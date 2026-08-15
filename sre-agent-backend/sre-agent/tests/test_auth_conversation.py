"""密码登录、Token 撤销和 Conversation 持久化回归测试。"""

from fastapi.testclient import TestClient

from app.main import create_app
from tests.mysql_support import mysql_test_database


def test_login_restore_conversation_cache_and_logout(monkeypatch):
    """覆盖前端实际启动顺序：登录→me→列表缓存→历史详情→注销。"""
    monkeypatch.setenv("SRE_INITIAL_USERNAME", "tester")
    monkeypatch.setenv("SRE_INITIAL_PASSWORD", "simple-pass-123")
    mysql_test_database()
    monkeypatch.setenv("APPLICATION_MYSQL_DATABASE", "sre_agent_test")
    with TestClient(create_app()) as client:
        wrong = client.post(
            "/api/auth/login", json={"username": "tester", "password": "wrong-password"}
        )
        assert wrong.status_code == 401

        login = client.post(
            "/api/auth/login", json={"username": "tester", "password": "simple-pass-123"}
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert login.status_code == 200
        assert client.get("/api/auth/me", headers=headers).json()["username"] == "tester"

        created = client.post("/api/conversations", json={"title": "订单延迟"}, headers=headers)
        conversation_id = created.json()["id"]
        user_id = login.json()["user"]["id"]
        client.app.state.conversation_service.append(
            user_id, conversation_id, "user", {"message": "为什么订单慢"}
        )

        cached = client.get("/api/conversations", headers=headers).json()
        detail = client.get(f"/api/conversations/{conversation_id}", headers=headers).json()
        assert cached[0]["message_count"] == 1
        assert detail["messages"][0]["content"]["message"] == "为什么订单慢"

        logout = client.post("/api/auth/logout", headers=headers)
        assert logout.status_code == 204
        assert client.get("/api/auth/me", headers=headers).status_code == 401
