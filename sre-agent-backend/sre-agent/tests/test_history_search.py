"""Real MySQL projection/recovery plus bounded ES protocol and workflow contracts."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.diagnosis.models import DiagnosisRootCause, RootCauseResource
from app.history.elasticsearch import ElasticsearchHistoryIndex
from app.history.models import HistoryItem, HistoryQuery
from app.history.repository import HistoryRepository
from app.history.service import HistoryContextBuilder, HistoryRetrievalService
from app.history.worker import HistorySyncWorker
from app.workflow.models import DiagnosisState
from tests.test_durable_diagnosis import _session


def completed_case():
    db, diagnoses, session = _session()
    diagnoses.upsert_root_cause(session.id, DiagnosisRootCause(
        title="pool exhausted", description="mysql connection pool exhausted",
        root_resource=RootCauseResource(type="DATABASE", name="mysql"), confidence=.9))
    diagnoses.update_session(session.id, status="COMPLETED", summary="MySQL pool exhaustion caused timeout",
                             affected_services=["payment-service"], finished=True)
    with db.connect() as conn:
        conn.execute("UPDATE diagnosis_sessions SET checkpoint_json = ?, question = ? WHERE id = ?",
            (json.dumps({"service": "payment-service", "synthesis": {"status": "confirmed"}}),
             "payment-service mysql timeout", session.id))
        conn.commit()
    return db, diagnoses, session, HistoryRepository(db)


class FakeIndex:
    def __init__(self):
        self.items = {}
        self.fail = False

    async def index(self, item):
        if self.fail:
            raise httpx.ConnectError("private upstream detail")
        self.items[item.history_id] = item

    async def search(self, user_id, project_id, query):
        if self.fail:
            raise httpx.ConnectError("private upstream detail")
        return list(self.items)[:query.limit]


@pytest.mark.asyncio
async def test_mysql_commit_index_retry_idempotency_and_rebuild():
    db, diagnoses, session, source = completed_case()
    index = FakeIndex()
    index.fail = True
    worker = HistorySyncWorker(source, index)
    assert (await worker.run_once())["captured"] == 1
    assert diagnoses.get(session.user_id, session.id).status.value == "COMPLETED"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM diagnosis_history_items WHERE task_id = ?", (session.id,)).fetchone()
    assert row["index_status"] == "pending" and row["retry_count"] == 1
    assert row["last_index_error"] == "ConnectError"
    index.fail = False
    source.rebuild()
    assert (await worker.run_once())["indexed"] == 1
    assert (await worker.run_once())["captured"] == 0
    source.rebuild()
    await worker.run_once()
    assert len(index.items) == 1
    assert index.items[session.id].category == "database"
    assert index.items[session.id].conclusion_status == "insufficient_evidence"


def test_history_conclusion_uses_evidence_gate():
    db, _, session, source = completed_case()
    state = DiagnosisState(query="mysql timeout", service="payment-service", synthesis={
        "status": "confirmed", "root_cause": "pool exhausted", "confidence": .9,
        "evidence_ids": [],
    })
    with db.connect() as conn:
        conn.execute("UPDATE diagnosis_sessions SET checkpoint_json = ? WHERE id = ?",
                     (state.model_dump_json(), session.id))
        conn.commit()
    assert source.capture_completed() == 1
    item = HistoryItem.model_validate_json(source.pending()[0]["document_json"])
    assert item.conclusion_status == "insufficient_evidence"


@pytest.mark.asyncio
async def test_retrieval_scopes_filters_deletion_and_budget(caplog):
    db, _, session, source = completed_case()
    index = FakeIndex()
    await HistorySyncWorker(source, index).run_once()
    service = HistoryRetrievalService(source, index)
    query = HistoryQuery(service="payment-service", text="mysql timeout", category="database")
    result = json.loads(await service.retrieve(session.user_id, session.project_id, query))
    assert len(result) == 1 and result[0]["supports_conclusion"] is False
    assert result[0]["structured_data"]["root_cause"] == "mysql connection pool exhausted"
    for user, project, q in [
        ("other", session.project_id, query), (session.user_id, "other", query),
        (session.user_id, session.project_id, query.model_copy(update={"service": "other"})),
        (session.user_id, session.project_id, query.model_copy(update={"category": "network"})),
        (session.user_id, session.project_id, query.model_copy(update={"start_time": datetime.now(timezone.utc)+timedelta(days=1)})),
        (session.user_id, session.project_id, query.model_copy(update={"exclude_task_id": session.id})),
    ]:
        assert await service.retrieve(user, project, q) == "[]"
    with db.connect() as conn:
        conn.execute("DELETE FROM diagnosis_sessions WHERE id = ?", (session.id,))
        conn.commit()
    assert await service.retrieve(session.user_id, session.project_id, query) == "[]"
    index.fail = True
    assert await service.retrieve(session.user_id, session.project_id, query) == "[]"
    assert "History Retrieval skipped" in caplog.text and "private upstream" not in caplog.text


@pytest.mark.asyncio
async def test_es_fixed_bm25_query_and_idempotent_document_path():
    seen = []
    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/_search"):
            body = json.loads(request.content)
            assert body["size"] == 3 and body["_source"] is False
            clauses = body["query"]["bool"]
            assert {"term": {"user_id": "owner"}} in clauses["filter"]
            assert {"term": {"project_id": "project"}} in clauses["filter"]
            assert {"term": {"service": "payment-service"}} in clauses["filter"]
            assert {"term": {"category": "database"}} in clauses["filter"]
            assert any("range" in row for row in clauses["filter"])
            assert clauses["must"][0]["multi_match"]["fields"] == ["symptom^3", "root_cause^2", "summary"]
            return httpx.Response(200, json={"hits": {"hits": [{"_id": "history-1"}]}})
        if "/_doc/" in request.url.path:
            assert request.url.path.endswith("/_doc/history-1")
            assert "sensitive" not in request.content.decode()
            return httpx.Response(201, json={"result": "created"})
        return httpx.Response(400, json={"error": {"type": "resource_already_exists_exception"}})
    index = ElasticsearchHistoryIndex("http://es", "history-test", transport=httpx.MockTransport(handle))
    item = HistoryItem(history_id="history-1", task_id="task", user_id="owner", project_id="project",
        service="payment-service", timestamp=datetime.now(timezone.utc), symptom="timeout", root_cause="pool",
        summary="password=sensitive")
    await index.index(item)
    await index.index(item)
    result = await index.search("owner", "project", HistoryQuery(service="payment-service", text="mysql timeout",
        category="database", limit=3, start_time=datetime.now(timezone.utc)-timedelta(days=30)))
    assert result == ["history-1"]
    assert len([r for r in seen if "/_doc/" in r.url.path]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "partial", "oversize", "auth", "bad_json"])
async def test_es_failures_degrade(failure):
    def handle(request):
        if failure == "timeout": raise httpx.ReadTimeout("hidden")
        if failure == "partial": return httpx.Response(200, json={"timed_out": True})
        if failure == "oversize": return httpx.Response(200, content=b"x"*1_000_001)
        if failure == "auth": return httpx.Response(403, json={"error": "hidden"})
        return httpx.Response(200, text="broken")
    index = ElasticsearchHistoryIndex("http://es", "history-test", transport=httpx.MockTransport(handle))
    service = HistoryRetrievalService(None, index)
    assert await service.retrieve("owner", "project", HistoryQuery(service="payment-service")) == "[]"


@pytest.mark.asyncio
async def test_history_does_not_become_current_evidence_and_failure_continues():
    from app.workflow.diagnosis import DiagnosisWorkflow
    from app.core.config import get_settings
    class Missing:
        async def for_incident(self, *args): raise httpx.ConnectError("unavailable")
    workflow = DiagnosisWorkflow(None, get_settings().service_catalog_path, history_retrieval=Missing())
    state = DiagnosisState(query="mysql timeout", service="payment-service", user_id="owner")
    await workflow._history_context(state)
    assert state.history_retrieved and state.long_term_history == "[]" and state.evidence == []
    class EmptyTools:
        async def execute(self, name, arguments): return {"data": {"empty": True}}
    workflow.tools = EmptyTools()
    report = await workflow.run("payment-service mysql timeout", user_id="owner", target="payment-service")
    assert report.workflow_phases[-1].value == "END"
    assert report.status == "insufficient_evidence"


def test_context_truncates_whole_cases_and_redacts():
    item = HistoryItem(history_id="a", task_id="a", user_id="u", project_id="p", service="demo",
        timestamp=datetime.now(timezone.utc), symptom="timeout", root_cause="pool", summary="token=private")
    context = HistoryContextBuilder.build([item]*10, 1600)
    assert len(context) <= 1600 and len(json.loads(context)) < 10
    assert "private" not in context


@pytest.mark.asyncio
async def test_event_delivery_cursor_is_monotonic_and_payload_bounded():
    _, diagnoses, session, source = completed_case()
    diagnoses.append_event(session.id, "diagnosis.completed", {"password": "private"}, event_key="history-test")
    events = source.pending_events()
    assert len(events) == 1 and "private" not in json.dumps(events)
    source.acknowledge_event(int(events[0]["event_id"]))
    source.acknowledge_event(0)
    assert source.pending_events() == []


@pytest.mark.asyncio
async def test_event_late_commit_with_lower_id_is_not_lost():
    db, diagnoses, session, source = completed_case()
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as late:
        late.execute("INSERT INTO diagnosis_events(diagnosis_id,event_type,data_json,created_at) VALUES (?, 'late', '{}', ?)", (session.id, now))
        diagnoses.append_event(session.id, "early", {})
        events = source.pending_events()
        assert len(events) == 1 and events[0]["event_type"] == "early"
        source.acknowledge_event(int(events[0]["event_id"]))
        late.commit()
    events = source.pending_events()
    assert len(events) == 1 and events[0]["event_type"] == "late"


def test_sanitize_quoted_json_credentials():
    from app.history.sanitize import sanitize
    assert "private" not in sanitize('"token": "private" password=private Authorization: Bearer private')


@pytest.mark.asyncio
async def test_unavailable_es_does_not_fail_mysql_durable_diagnosis(caplog):
    from app.core.config import get_settings
    from app.conversation import ConversationService
    from app.diagnosis.execution import DurableWorkflowRuntime
    from app.diagnosis.orchestrator import DiagnosisOrchestrator
    from app.diagnosis.service import DiagnosisService
    from app.diagnosis.schemas import DiagnosisCreateRequest
    from app.workflow.diagnosis import DiagnosisWorkflow
    db, repository, session = _session()
    class EmptyTools:
        async def execute(self, name, arguments): return {"data": {"empty": True}}
    history = HistoryRetrievalService(HistoryRepository(db),
        ElasticsearchHistoryIndex("http://127.0.0.1:1", "unavailable-history", timeout=.1), timeout=.2)
    workflow = DiagnosisWorkflow(EmptyTools(), get_settings().service_catalog_path, history_retrieval=history)
    service = DiagnosisService(repository, ConversationService(db))
    orchestrator = DiagnosisOrchestrator(workflow, service, repository)
    claimed = repository.claim(session.id, "history-failure-test", lease_ttl_seconds=60,
        max_attempts=3, recovery_reason="test")
    runtime = DurableWorkflowRuntime(repository, claimed, "history-failure-test", 60)
    await orchestrator.run(session.user_id, session.id,
        DiagnosisCreateRequest(trigger_type="QUESTION", question="order-service timeout"), runtime=runtime)
    completed = repository.get(session.user_id, session.id)
    assert completed.status.value == "COMPLETED" and completed.checkpoint_json
    assert "History Retrieval skipped" in caplog.text


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("HISTORY_TEST_ES_URL"), reason="opt-in real Elasticsearch integration")
async def test_real_mysql_to_es_bm25_filters_and_rebuild():
    url = os.environ["HISTORY_TEST_ES_URL"]
    assert url.startswith(("http://127.0.0.1:", "http://localhost:"))
    _, _, session, source = completed_case()
    index = ElasticsearchHistoryIndex(url, "sre-history-test-"+uuid4().hex, timeout=5)
    try:
        await HistorySyncWorker(source, index).run_once()
        await index.request("POST", index.index_name+"/_refresh")
        service = HistoryRetrievalService(source, index, timeout=5)
        query = HistoryQuery(service="payment-service", text="mysql timeout", category="database")
        assert len(json.loads(await service.retrieve(session.user_id, session.project_id, query))) == 1
        assert await service.retrieve(session.user_id, session.project_id, query.model_copy(update={"service": "other"})) == "[]"
        await index.request("DELETE", index.index_name)
        source.rebuild()
        await HistorySyncWorker(source, index).run_once()
        await index.request("POST", index.index_name+"/_refresh")
        assert len(json.loads(await service.retrieve(session.user_id, session.project_id, query))) == 1
    finally:
        async with httpx.AsyncClient() as client:
            await client.delete(url+"/"+index.index_name)
