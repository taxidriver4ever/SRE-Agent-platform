"""全项目测试补充：认证隔离、Diagnosis 创建契约与 SSE 断线续传。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import get_tool_policy
from app.auth import require_user
from app.core.config import get_settings
from app.diagnosis.models import DiagnosisEvent, DiagnosisSession
from app.diagnosis.router import (
    get_diagnosis_execution_manager,
    get_diagnosis_repository,
    get_diagnosis_service,
    router as diagnosis_router,
)
from app.main import create_app


def _login(
    client: TestClient,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, str]:
    if username is None or password is None:
        settings = get_settings()
        username = settings.initial_username
        password = settings.initial_password
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.parametrize(
    "authorization",
    ["Basic abc", "Bearer", "Bearer definitely-invalid-token"],
)
def test_protected_api_rejects_malformed_or_invalid_authorization(authorization: str) -> None:
    """HTTP 层必须统一拒绝错误认证方案、空 Bearer 和未知 Token。"""
    with TestClient(create_app()) as client:
        response = client.get("/api/conversations", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_expired_token_is_rejected() -> None:
    """数据库中过期的 Token 即使摘要仍存在，也不能继续访问受保护资源。"""
    with TestClient(create_app()) as client:
        headers = _login(client)
        token = headers["Authorization"].removeprefix("Bearer ")
        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        auth_service = client.app.state.auth_service
        with auth_service.database.connect() as connection:
            connection.execute(
                "UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?",
                (expired_at, auth_service._token_hash(token)),
            )
            connection.commit()

        response = client.get("/api/auth/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid or expired token"


def test_conversation_is_not_visible_to_another_user() -> None:
    """通过猜测 conversation_id 读取其他用户会话时必须返回 404。"""
    with TestClient(create_app()) as client:
        owner_headers = _login(client)
        created = client.post(
            "/api/conversations", json={"title": "owner-only"}, headers=owner_headers
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]

        client.app.state.auth_service.ensure_user("other-user", "other-pass-123")
        other_headers = _login(client, "other-user", "other-pass-123")

        detail = client.get(f"/api/conversations/{conversation_id}", headers=other_headers)
        listing = client.get("/api/conversations", headers=other_headers)

    assert detail.status_code == 404
    assert listing.status_code == 200
    assert all(item["id"] != conversation_id for item in listing.json())


class _AllowProjectPolicy:
    @staticmethod
    def project(project_id: str) -> dict[str, str]:
        assert project_id == "sre-lab"
        return {"id": project_id}


class _CreateService:
    def __init__(self) -> None:
        self.requests = []

    def create(self, user_id, request):
        self.requests.append((user_id, request))
        return DiagnosisSession(
            id="d" * 32,
            user_id=user_id,
            conversation_id="c" * 32,
            question=request.question,
            trigger_type=request.trigger_type,
            initial_target=request.initial_target,
            status="PENDING",
            created_at="2026-09-08T00:00:00+00:00",
            updated_at="2026-09-08T00:00:00+00:00",
        )


class _ExecutionManager:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, diagnosis_id: str) -> None:
        self.submitted.append(diagnosis_id)


@pytest.mark.parametrize(
    ("trigger_type", "initial_target"),
    [
        ("QUESTION", None),
        ("SERVICE", {"type": "SERVICE", "name": "order-service", "namespace": "sre-lab"}),
        ("POD", {"type": "POD", "name": "order-service-abc", "namespace": "sre-lab"}),
    ],
)
def test_formal_diagnosis_create_returns_pending_and_submits_background_execution(
    trigger_type: str, initial_target: dict[str, str] | None
) -> None:
    """三种入口都应快速返回 202/PENDING，并把执行交给持久化后台管理器。"""
    service = _CreateService()
    manager = _ExecutionManager()
    application = FastAPI()
    application.include_router(diagnosis_router)
    application.dependency_overrides[require_user] = lambda: {"id": "user-1", "username": "tester"}
    application.dependency_overrides[get_tool_policy] = lambda: _AllowProjectPolicy()
    application.dependency_overrides[get_diagnosis_service] = lambda: service
    application.dependency_overrides[get_diagnosis_execution_manager] = lambda: manager

    response = TestClient(application).post(
        "/api/diagnoses",
        json={
            "trigger_type": trigger_type,
            "question": "检查当前异常",
            "initial_target": initial_target,
            "project_id": "sre-lab",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "id": "d" * 32,
        "status": "PENDING",
        "events_url": f"/api/diagnoses/{'d' * 32}/events",
        "detail_url": f"/api/diagnoses/{'d' * 32}",
    }
    assert manager.submitted == ["d" * 32]
    assert service.requests[0][1].trigger_type.value == trigger_type


class _SseRepository:
    def __init__(self) -> None:
        self.events = [
            DiagnosisEvent(
                id=index,
                diagnosis_id="d" * 32,
                type="phase",
                data={"phase": f"PHASE_{index}"},
                created_at=f"2026-09-08T00:00:0{index}+00:00",
            )
            for index in (1, 2, 3)
        ]

    @staticmethod
    def get(user_id: str, diagnosis_id: str) -> DiagnosisSession | None:
        if user_id != "user-1" or diagnosis_id != "d" * 32:
            return None
        return DiagnosisSession(
            id=diagnosis_id,
            conversation_id="c" * 32,
            question="检查当前异常",
            trigger_type="QUESTION",
            status="COMPLETED",
            created_at="2026-09-08T00:00:00+00:00",
            updated_at="2026-09-08T00:00:03+00:00",
        )

    def list_events(self, user_id: str, diagnosis_id: str, after_id: int):
        assert user_id == "user-1"
        assert diagnosis_id == "d" * 32
        return [event for event in self.events if event.id > after_id]


def _sse_application(repository: _SseRepository) -> FastAPI:
    application = FastAPI()
    application.include_router(diagnosis_router)
    application.dependency_overrides[require_user] = lambda: {"id": "user-1", "username": "tester"}
    application.dependency_overrides[get_diagnosis_repository] = lambda: repository
    return application


def test_sse_last_event_id_replays_only_missing_events_in_order() -> None:
    """断线后携带 Last-Event-ID 时只能回放游标之后的事件。"""
    application = _sse_application(_SseRepository())
    with TestClient(application) as client:
        response = client.get(
            f"/api/diagnoses/{'d' * 32}/events",
            headers={"Last-Event-ID": "2"},
        )

    assert response.status_code == 200
    assert "id: 3\n" in response.text
    assert '"phase": "PHASE_3"' in response.text
    assert "id: 1\n" not in response.text
    assert "id: 2\n" not in response.text


def test_sse_rejects_invalid_last_event_id() -> None:
    """不可解析的 SSE 游标必须返回 400，不能静默重放完整 Timeline。"""
    application = _sse_application(_SseRepository())
    response = TestClient(application).get(
        f"/api/diagnoses/{'d' * 32}/events",
        headers={"Last-Event-ID": "not-an-integer"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid Last-Event-ID"
