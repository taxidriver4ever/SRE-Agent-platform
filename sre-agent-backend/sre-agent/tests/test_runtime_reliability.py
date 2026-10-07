"""Fencing and runtime integration regressions, using the isolated MySQL DB."""

import asyncio

import pytest
from pydantic import BaseModel

from app.diagnosis.execution import DiagnosisExecutionManager, DurableWorkflowRuntime
from app.diagnosis.repository import DiagnosisOwnershipLost
from app.llm.base import LLMMessage, LLMResponse
from app.llm.structured import generate_structured
from app.workflow.diagnosis import DiagnosisWorkflow
from app.workflow.runtime import NoopWorkflowRuntime
from app.workflow.models import WorkflowPhase
from tests.test_durable_diagnosis import _session, _state, _expired, _Sandbox


@pytest.mark.asyncio
async def test_old_worker_cannot_project_after_takeover():
    database, repository, session = _session()
    claimed = repository.claim(session.id, "a", lease_ttl_seconds=60, max_attempts=3, recovery_reason="test")
    old = DurableWorkflowRuntime(repository, claimed, "a", 60)
    with database.connect() as connection:
        connection.execute("UPDATE diagnosis_sessions SET lease_expires_at = ? WHERE id = ?", (_expired(), session.id))
        connection.commit()
    assert repository.claim(session.id, "b", lease_ttl_seconds=60, max_attempts=3, recovery_reason="takeover")
    with pytest.raises(DiagnosisOwnershipLost):
        await old.persist_projection(lambda: repository.append_event(session.id, "stale.write", {}))
    assert not any(event.type == "stale.write" for event in repository.list_events(session.user_id, session.id, 0))


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_steps_and_events():
    _, repository, session = _session()
    claimed = repository.claim(session.id, "a", lease_ttl_seconds=60, max_attempts=3, recovery_reason="test")
    runtime = DurableWorkflowRuntime(repository, claimed, "a", 60)
    def write():
        repository.append_step(session.id, step_type="REPORT", summary="partial", idempotency_key="partial")
        repository.append_event(session.id, "partial.event", {}, event_key="partial")
        raise ValueError("projection failed")
    with pytest.raises(ValueError, match="projection failed"):
        await runtime.persist_projection(write)
    assert repository.get_step_by_key(session.id, "partial") is None
    assert repository.list_events(session.user_id, session.id, 0) == []


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_renewed_by_stale_worker():
    database, repository, session = _session()
    claimed = repository.claim(session.id, "a", lease_ttl_seconds=60, max_attempts=3, recovery_reason="test")
    with database.connect() as connection:
        connection.execute("UPDATE diagnosis_sessions SET lease_expires_at = ? WHERE id = ?", (_expired(), session.id))
        connection.commit()
    with pytest.raises(DiagnosisOwnershipLost):
        repository.heartbeat(session.id, "a", claimed.state_version, 60)
    assert not repository.fail_owned_session(session.id, "a", "stale failure")
    assert repository.get_unscoped(session.id).status.value == "INVESTIGATING"


@pytest.mark.asyncio
async def test_concurrent_baseline_does_not_swallow_ownership_loss():
    _, _, session = _session()
    class Runtime(NoopWorkflowRuntime):
        async def begin_tool(self, *args):
            raise DiagnosisOwnershipLost("taken over")
    workflow = object.__new__(DiagnosisWorkflow)
    workflow.max_steps = 12
    with pytest.raises(DiagnosisOwnershipLost):
        await workflow._call_concurrently(_state(session), None, [("query_logs", {}, "logs")], runtime=Runtime())


@pytest.mark.asyncio
async def test_concurrent_ownership_loss_cancels_sibling_without_waiting_for_tool_timeout():
    from app.workflow.models import DiagnosisState
    entered, cancelled = asyncio.Event(), asyncio.Event()
    workflow = object.__new__(DiagnosisWorkflow)
    workflow.max_steps = 12
    async def call(state, name, *args, **kwargs):
        if name == "lost":
            await entered.wait()
            raise DiagnosisOwnershipLost("worker-b took over")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    workflow._call = call
    with pytest.raises(DiagnosisOwnershipLost):
        await asyncio.wait_for(workflow._call_concurrently(
            DiagnosisState(query="timeout"), None,
            [("slow", {}, "logs"), ("lost", {}, "metrics")],
        ), 1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_inflight_model():
    _, repository, session = _session()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class Transport:
        async def complete(self, messages):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    class Orchestrator:
        async def run(self, *args, **kwargs):
            await Transport().complete([LLMMessage("user", "diagnose")])
    manager = DiagnosisExecutionManager(Orchestrator(), repository, _Sandbox())
    async def lost(runtime):
        await entered.wait()
        raise DiagnosisOwnershipLost("taken over")
    manager._heartbeat = lost
    await asyncio.wait_for(manager._execute(session.id, "test"), 3)
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs, expected, retries", [
    (["prefix {'answer': 'fixed',}"], "fixed", 0),
    (["bad", '{"answer":"recovered"}'], "recovered", 1),
    (["{}", '{"answer":"schema repaired"}'], "schema repaired", 1),
    (["bad", "bad", "bad", '{"answer":"refilled"}'], "refilled", 3),
    (["bad", "bad", "bad", "bad"], None, 3),
])
async def test_bounded_structured_calls_preserve_usage_and_fallback(outputs, expected, retries):
    class Output(BaseModel):
        answer: str
    class Transport:
        calls = []
        async def complete(self, messages):
            self.calls.append(list(messages))
            return LLMResponse(outputs[len(self.calls)-1], "fixture", prompt_tokens=3, completion_tokens=2)
    transport = Transport()
    result = await generate_structured(
        transport, [LLMMessage("user", "diagnose")], Output,
        retries=2, template={"answer": ""},
    )
    assert (result.value.answer if result.value else None) == expected
    assert result.retry_count == retries
    assert len(transport.calls) == len(outputs)
    assert result.prompt_tokens == 3 * len(outputs)
    assert result.completion_tokens == 2 * len(outputs)
    if len(outputs) == 4:
        assert '"type": "structured_output_template_refill"' in transport.calls[-1][-1].content


@pytest.mark.asyncio
async def test_planner_and_synthesis_use_runtime_fallback_and_token_accounting(monkeypatch):
    from app.workflow.planning import planner as planning
    from app.workflow.models import DiagnosisState
    monkeypatch.setattr(planning, "evidence_driven_decision", lambda state: None)
    monkeypatch.setattr(planning, "deterministic_synthesis", lambda state: None)
    outputs = iter([
        '{"action":"insufficient_evidence","reason":"no safe next step"}',
        '{"status":"insufficient_evidence","root_cause":"need evidence","confidence":0.0}',
    ])
    class Transport:
        async def complete(self, messages):
            assert messages[0].role == "system"
            assert messages[1].role == "user"
            return LLMResponse(next(outputs), "fixture", prompt_tokens=5, completion_tokens=3)
    planner = planning.EvidencePlanner(Transport())
    state = DiagnosisState(query="timeout", service="order-service")
    assert (await planner.decide(state, [])).action == "insufficient_evidence"
    assert (await planner.synthesize(state)).status == "insufficient_evidence"
    assert planner.prompt_tokens == 10
    assert planner.completion_tokens == 6


@pytest.mark.asyncio
async def test_structured_transport_failure_is_not_retried():
    from app.llm.gateway import GatewayRequestError
    class Output(BaseModel):
        answer: str
    class Transport:
        calls = 0
        async def complete(self, messages):
            self.calls += 1
            raise GatewayRequestError("offline")
    llm = Transport()
    with pytest.raises(GatewayRequestError, match="offline"):
        await generate_structured(llm, [], Output, retries=3, template={"answer": ""})
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_structured_cancellation_propagates_without_refill():
    class Output(BaseModel):
        answer: str
    entered = asyncio.Event()
    class Transport:
        calls = 0
        async def complete(self, messages):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()
    llm = Transport()
    task = asyncio.create_task(generate_structured(llm, [], Output, retries=3, template={"answer": ""}))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_nonempty, original", [(True, "bad"), (False, "")])
async def test_structured_empty_response_and_original_output_contract(first_nonempty, original):
    class Output(BaseModel):
        answer: str
    history = [LLMMessage("user", "  {literal} 中文  ")]
    calls = []
    class Transport:
        async def complete(self, messages):
            calls.append(list(messages))
            return LLMResponse("" if len(calls) == 1 else "bad", "test")
    result = await generate_structured(Transport(), history, Output, retries=1,
                                       template={"answer": ""}, empty_response="", first_nonempty=first_nonempty)
    assert result.value is None and result.retry_count == 2
    assert result.original_output == original
    assert calls[1][1] == LLMMessage("assistant", "")
    assert history == [LLMMessage("user", "  {literal} 中文  ")]


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_compaction_commits_only_valid_output_after_refill(valid):
    from types import SimpleNamespace
    from app.conversation_memory.models import CompactionOutput, ShortContextState
    from app.conversation_memory.service import ConversationCompactionService
    snapshot = SimpleNamespace(summary="", state=ShortContextState(), pending_messages=[{
        "id": "message-1", "role": "user", "message_type": "text", "content": " {原文} ",
        "run_id": None, "tool_name": None,
    }])
    commits, calls = [], []
    class Repository:
        def active_snapshot(self, user_id, conversation_id):
            return snapshot
        def commit(self, *args):
            commits.append(args)
    output = CompactionOutput(conversation_summary="summary", context_state=ShortContextState(), memory_items=[])
    class Transport:
        async def complete(self, messages):
            calls.append(list(messages))
            return LLMResponse(output.model_dump_json() if valid and len(calls) == 4 else "bad", "test")
    service = ConversationCompactionService(Repository(), Transport(), structured_output_retries=2)
    result = await service.maybe_compact("user", "conversation", force=True)
    assert len(calls) == 4
    assert calls[0][0].role == "system" and calls[0][1].role == "user"
    assert '"type": "structured_output_template_refill"' in calls[-1][-1].content
    if valid:
        assert result == output
        assert len(commits) == 1 and commits[0][:4] == ("user", "conversation", output, "message-1")
        assert not service.last_failed_raw_output
    else:
        assert result is None and not commits
        assert service.last_failed_raw_output == {"conversation": "bad"}
