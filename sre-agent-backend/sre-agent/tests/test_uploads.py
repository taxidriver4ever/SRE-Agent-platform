"""Docker MinIO 预签名直传、会话绑定和 Evidence 回查集成测试。"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.main import create_app


def test_presigned_upload_persists_only_oss_key_and_can_become_evidence(monkeypatch):
    """覆盖前端真实顺序：建会话→签名→PUT→complete→Evidence 回查。"""
    database_path = Path(".data") / f"test-upload-{uuid4().hex}.sqlite3"
    monkeypatch.setenv("APPLICATION_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SRE_INITIAL_USERNAME", "upload-tester")
    monkeypatch.setenv("SRE_INITIAL_PASSWORD", "upload-test-pass")
    raw_log = b"order-service timeout trace_id=abc123\n"
    try:
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "upload-tester", "password": "upload-test-pass"},
            )
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            conversation = client.post(
                "/api/conversations", json={"title": "上传日志"}, headers=headers
            ).json()

            presign = client.post(
                "/api/uploads/presign",
                headers=headers,
                json={
                    "conversation_id": conversation["id"],
                    "filename": "incident.log",
                    "content_type": "text/plain",
                    "size": len(raw_log),
                    "kind": "file",
                },
            )
            assert presign.status_code == 200
            signed = presign.json()
            # 使用独立 HTTP 客户端模拟浏览器直接 PUT；文件字节不会经过 FastAPI。
            put = httpx.put(
                signed["upload_url"], content=raw_log, headers={"Content-Type": "text/plain"}
            )
            assert put.status_code == 200

            completed = client.post(
                "/api/uploads/complete",
                headers=headers,
                json={
                    "conversation_id": conversation["id"],
                    "oss_key": signed["oss_key"],
                    "expected_size": len(raw_log),
                },
            )
            assert completed.status_code == 200
            detail = client.get(
                f"/api/conversations/{conversation['id']}", headers=headers
            ).json()
            assert detail["attachments"][0]["oss_key"] == signed["oss_key"]

            evidence_id, _ = client.app.state.evidence_store.register_uploaded(
                "run-upload-test", signed["oss_key"]
            )
            evidence = client.get(
                f"/api/agent/evidence/run-upload-test/{evidence_id}", headers=headers
            ).json()
            assert evidence["result"]["text"] == raw_log.decode("utf-8")

            # 表结构断言防止未来误把文件正文、URL 或 metadata JSON 加回 SQLite。
            with closing(client.app.state.conversation_service.database.connect()) as connection:
                columns = [
                    row[1]
                    for row in connection.execute("PRAGMA table_info(conversation_attachments)")
                ]
            assert columns == ["id", "conversation_id", "user_id", "oss_key", "created_at"]
    finally:
        database_path.unlink(missing_ok=True)
