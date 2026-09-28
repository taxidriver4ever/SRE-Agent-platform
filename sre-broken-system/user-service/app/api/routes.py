"""FastAPI routes for profiles, memberships, listing and controlled fault injection."""

import asyncio
import time
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from app.core.faults import faults
from app.core.cpu_work import count_primes
from app.observability.telemetry import BLOCKING, log_event
from app.repository.user_repository import UserRepository
from app.schema.user import MembershipSummary, UserProfile
from app.service.user_service import UserNotFoundError, UserService


router = APIRouter()
service = UserService(UserRepository())


@router.get("/users/{user_id}", response_model=UserProfile)
async def profile(user_id: int, request: Request) -> UserProfile:
    """Read a profile; normal DB I/O runs in the thread pool to protect the event loop."""
    mode = faults.get()
    if mode == "cpu_saturation":
        BLOCKING.labels("user-service", request.app.state.version, request.app.state.pod_name).set(1)
        count_primes(550_000)
    elif mode == "event_loop_blocking":
        # This blocking call is intentionally wrong and isolated behind an explicit scenario switch.
        time.sleep(2)
    elif mode in {"database_latency", "high_latency"}:
        await asyncio.sleep(faults.parameters["delay_ms"] / 1000)
    elif mode == "random_error":
        import random
        if random.random() < faults.parameters["error_rate"]: raise HTTPException(503,"simulated user error")
    try:
        result = await run_in_threadpool(service.profile, user_id)
        log_event("INFO", "user profile read", request.state.trace_id, user_id=user_id, fault_mode=mode)
        return result
    except UserNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    finally:
        BLOCKING.labels("user-service", request.app.state.version, request.app.state.pod_name).set(0)


@router.get("/users/{user_id}/membership", response_model=MembershipSummary)
async def membership(user_id: int) -> MembershipSummary:
    """Return membership and discount without exposing the ORM model."""
    try:
        return await run_in_threadpool(service.membership, user_id)
    except UserNotFoundError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/users", response_model=list[UserProfile])
async def list_users(after_id: int = 0, limit: int = Query(20, ge=1, le=100)) -> list[UserProfile]:
    """Provide cursor-based administration listing with a hard result limit."""
    return await run_in_threadpool(service.list_users, after_id, limit)


@router.api_route("/debug/fault", methods=["GET", "POST"])
async def fault(mode: str | None = None) -> dict[str, str]:
    """Read or change this Pod's fault mode using a strict whitelist."""
    if mode is not None and not faults.set(mode):
        raise HTTPException(400, "unsupported fault mode")
    return {"service": "user-service", "fault_mode": faults.get()}

from pydantic import BaseModel, Field
from app.core.faults import faults
class FaultInput(BaseModel):
    fault: str
    duration_seconds: float = Field(120, ge=1, le=300)
    parameters: dict = Field(default_factory=dict)
@router.get("/internal/faults")
def get_faults(): return faults.snapshot()
@router.post("/internal/faults")
def set_faults(body: FaultInput):
    if not faults.set(body.fault, body.duration_seconds, body.parameters): raise HTTPException(400,"invalid fault parameters")
    return faults.snapshot()
@router.delete("/internal/faults/{name}")
def clear_fault(name: str):
    if faults.get()==name: faults.set("normal")
    return faults.snapshot()

@router.get("/users/{user_id}/status")
async def user_status(user_id: int, request: Request):
    user = await profile(user_id,request)
    return {"user_id":user.id,"status":user.status,"risk_status":"blocked" if user.status.upper() in {"BLOCKED","SUSPENDED"} else "clear"}
@router.get("/users/{user_id}/preferences")
async def preferences(user_id: int, request: Request):
    user = await profile(user_id,request)
    return {"user_id":user.id,"category":f"category-{user.id % 40}","membership_level":user.membership_level}
