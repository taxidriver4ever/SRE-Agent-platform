import os
os.environ["OTEL_SDK_DISABLED"]="true"
from fastapi.testclient import TestClient
from app.main import app
from app.core.faults import faults

def test_health_and_fault_contract(monkeypatch):
    faults.set("normal")
    with TestClient(app) as client:
        r=client.get("/health",headers={"x-request-id":"contract-123"})
        assert r.status_code==200 and r.headers["x-request-id"]=="contract-123"
        assert r.json()["status"]=="ok"
        assert client.post("/internal/faults",json={"fault":"unknown"}).status_code==400
        assert client.post("/internal/faults",json={"fault":"normal","duration_seconds":301}).status_code==422
    assert not faults.set("normal",parameters={"error_rate":2})
    monkeypatch.setattr("app.core.faults.monotonic",lambda:10)
    assert faults.set("normal",1)
    monkeypatch.setattr("app.core.faults.monotonic",lambda:12)
    assert faults.get()=="normal"

def test_user_status_preferences_contract(monkeypatch):
    from app.api import routes
    from app.schema.user import UserProfile
    from datetime import datetime,timezone
    from app.observability.telemetry import request_id_context
    def profile(self,uid):
        assert request_id_context.get()=="contract-123"
        return UserProfile(id=uid,email="lab@example.com",display_name="Lab",membership_level="GOLD",status="ACTIVE",created_at=datetime.now(timezone.utc))
    monkeypatch.setattr(type(routes.service),"profile",profile)
    with TestClient(app) as client:
        assert client.get("/users/7/status",headers={"x-request-id":"contract-123"}).json()["status"]=="ACTIVE"
        assert client.get("/users/7/preferences",headers={"x-request-id":"contract-123"}).json()["category"]=="category-7"
    assert request_id_context.get() is None
