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

def test_recommendation_calls_user_preferences(monkeypatch):
    import httpx
    calls=[]
    async def get(self,url,**kwargs):
        from app.observability.telemetry import request_id_context
        assert request_id_context.get()=="contract-123"
        calls.append((url,kwargs))
        return httpx.Response(200,json={"category":"category-7"},request=httpx.Request("GET",url))
    monkeypatch.setattr(httpx.AsyncClient,"get",get)
    with TestClient(app) as client:
        r=client.get("/recommendations/7",headers={"x-request-id":"contract-123"})
        assert r.status_code==200 and len(r.json())==10
    assert calls[0][0].endswith("/users/7/preferences")
    assert calls[0][1]["headers"]["x-request-id"]=="contract-123"
