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
    elif mode == "database_latency":
        await asyncio.sleep(1.5)
    try:
        # BAD: synchronous SQLAlchemy I/O now runs directly on the event loop. Under concurrent
        # traffic one slow database connection stalls unrelated FastAPI requests on this worker.
        result = service.profile(user_id)
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
