"""FastAPI 生命周期、健康检查和 HTTP 错误映射测试。"""

from fastapi.testclient import TestClient

from app.main import create_app


def login_headers(client: TestClient) -> dict[str, str]:
    """使用本地初始账号登录，返回受保护 API 所需 Authorization Header。"""
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_health_endpoint():
    """应用进入 lifespan 后应提供不依赖网关的存活检查。"""
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_endpoint_reports_missing_gateway_key(monkeypatch):
    """缺少 Gateway Token 时，Agent 接口应返回可理解的 503。"""
    # 清理父进程可能存在的配置，保证测试结果不依赖开发机环境变量。
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        response = client.post("/v1/agent/run", json={"query": "hello"}, headers=login_headers(client))
    assert response.status_code == 503
    assert response.json() == {"detail": "GATEWAY_API_KEY is not configured"}


def test_evidence_endpoint_returns_full_stored_result():
    """最终报告中的 evidence_id 必须能回查完整原文，而不只是压缩摘要。"""
    with TestClient(create_app()) as client:
        headers = login_headers(client)
        user = client.get("/api/auth/me", headers=headers).json()
        conversation = client.post(
            "/api/conversations", json={"title": "evidence"}, headers=headers
        ).json()
        evidence_id = client.app.state.conversation_service.append(
            user["id"], conversation["id"], "assistant",
            {
                "tool_name": "query_logs",
                "arguments": {"service": "order-service"},
                "result": {"logs": "full raw log"},
                "source_references": [],
            },
            message_type="tool_result", run_id="run-test", tool_name="query_logs",
        )
        response = client.get(f"/api/agent/evidence/run-test/{evidence_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["result"] == {"logs": "full raw log"}


def test_protected_endpoint_rejects_missing_token():
    """诊断与证据接口不能只依赖前端隐藏，服务端必须强制 Bearer Token。"""
    with TestClient(create_app()) as client:
        response = client.get("/api/conversations")
    assert response.status_code == 401
