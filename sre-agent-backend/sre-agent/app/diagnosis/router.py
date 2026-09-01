"""Incident/Diagnosis Session 的统一 REST 与 SSE API。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.router import get_sandbox_manager, get_tool_policy, require_project
from app.auth import CurrentUser, require_user
from app.conversation_memory import conversation_memory_scope
from app.diagnosis.models import (
    DiagnosisEvidence, DiagnosisRootCause, DiagnosisSession, DiagnosisStatus,
    IncidentGraph, InvestigationStep,
)
from app.diagnosis.orchestrator import DiagnosisOrchestrator
from app.diagnosis.repository import DiagnosisRepository
from app.diagnosis.schemas import (
    DiagnosisCreatedResponse, DiagnosisCreateRequest, DiagnosisListResponse, QuickDiagnosisRequest,
)
from app.diagnosis.service import DiagnosisService
from app.sandbox import DockerSandboxManager
from app.security import ToolPolicy, task_security_scope

router = APIRouter(prefix="/api/diagnoses", tags=["diagnoses"])
logger = logging.getLogger(__name__)


def get_diagnosis_repository(request: Request) -> DiagnosisRepository:
    return request.app.state.diagnosis_repository


def get_diagnosis_service(request: Request) -> DiagnosisService:
    return request.app.state.diagnosis_service


def get_diagnosis_orchestrator(request: Request) -> DiagnosisOrchestrator:
    return request.app.state.diagnosis_orchestrator


@router.post("/quick/stream")
async def quick_diagnosis_stream(
    body: QuickDiagnosisRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    orchestrator: Annotated[DiagnosisOrchestrator, Depends(get_diagnosis_orchestrator)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    sandbox: Annotated[DockerSandboxManager, Depends(get_sandbox_manager)],
) -> StreamingResponse:
    """执行无会话、无历史、无上下文记忆的一次性只读快速诊断。"""
    require_project(policy, body.project_id)
    create_request = DiagnosisCreateRequest(
        trigger_type=body.target.type.value,
        question=body.question,
        initial_target=body.target,
        project_id=body.project_id,
    )
    target, system_scan = orchestrator.resolve_target(create_request)
    if target not in orchestrator.workflow.catalog.services:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="target is not in service catalog")

    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    task_id = uuid4().hex

    async def publish(event: dict[str, object]) -> None:
        # 工作流的 final 会在 build_quick_result 后由本端点统一发送。
        if event.get("type") != "final":
            await queue.put(event)

    async def execute() -> None:
        try:
            async with sandbox.task_workspace(task_id) as workspace:
                with task_security_scope(user["id"], body.project_id, task_id, str(workspace)):
                    report = await orchestrator.workflow.run(
                        body.question,
                        publish,
                        user_id=None,
                        conversation_id=None,
                        target=target,
                        symptom=body.question,
                        system_scan=system_scan,
                    )
            await queue.put({"type": "final", "result": orchestrator.build_quick_result(report, create_request)})
        except Exception as exc:
            detail = str(exc).strip()
            message = f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__
            logger.exception("quick diagnosis failed: target=%s", body.target.name)
            await queue.put({"type": "error", "message": message})
        finally:
            await queue.put({"type": "done"})

    async def stream() -> AsyncIterator[str]:
        task = asyncio.create_task(execute())
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "done":
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("", response_model=DiagnosisCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_diagnosis(
    body: DiagnosisCreateRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[DiagnosisService, Depends(get_diagnosis_service)],
    orchestrator: Annotated[DiagnosisOrchestrator, Depends(get_diagnosis_orchestrator)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    sandbox: Annotated[DockerSandboxManager, Depends(get_sandbox_manager)],
) -> DiagnosisCreatedResponse:
    """QUESTION、SERVICE 和 POD 三种入口统一创建独立 Diagnosis Session。"""
    require_project(policy, body.project_id)
    session = service.create(user["id"], body)
    task_id = uuid4().hex

    async def execute() -> None:
        try:
            async with sandbox.task_workspace(task_id) as workspace:
                with task_security_scope(user["id"], body.project_id, task_id, str(workspace)):
                    with conversation_memory_scope(user["id"], session.conversation_id):
                        await orchestrator.run(user["id"], session.id, body)
        except asyncio.CancelledError:
            current = service.repository.get(user["id"], session.id)
            if current and current.status in {DiagnosisStatus.PENDING, DiagnosisStatus.INVESTIGATING}:
                service.repository.update_session(
                    session.id, status=DiagnosisStatus.CANCELLED, error_message="诊断任务已取消", finished=True,
                )
                service.repository.append_event(session.id, "diagnosis.cancelled", {
                    "diagnosis_id": session.id, "status": DiagnosisStatus.CANCELLED.value,
                })
            raise
        except Exception as exc:
            logger.exception("diagnosis session failed: diagnosis_id=%s", session.id)
            service.fail(user["id"], session.id, exc)

    task = asyncio.create_task(execute(), name=f"diagnosis-{session.id}")
    tasks: set[asyncio.Task[None]] = request.app.state.diagnosis_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return DiagnosisCreatedResponse(
        id=session.id, status=session.status,
        events_url=f"/api/diagnoses/{session.id}/events",
        detail_url=f"/api/diagnoses/{session.id}",
    )


@router.get("", response_model=DiagnosisListResponse)
async def list_diagnoses(
    user: Annotated[CurrentUser, Depends(require_user)],
    repository: Annotated[DiagnosisRepository, Depends(get_diagnosis_repository)],
    limit: int = Query(default=50, ge=1, le=100),
) -> DiagnosisListResponse:
    """返回当前用户的 Incident 历史，供侧栏与历史页面使用。"""
    return DiagnosisListResponse(items=repository.list_for_user(user["id"], limit))


@router.get("/{diagnosis_id}", response_model=DiagnosisSession)
async def get_diagnosis(
    diagnosis_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[DiagnosisService, Depends(get_diagnosis_service)],
) -> DiagnosisSession:
    session = service.get_detail(user["id"], diagnosis_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="diagnosis not found")
    return session


@router.get("/{diagnosis_id}/steps", response_model=list[InvestigationStep])
async def get_steps(
    diagnosis_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    repository: Annotated[DiagnosisRepository, Depends(get_diagnosis_repository)],
) -> list[InvestigationStep]:
    return _owned(lambda: repository.list_steps(user["id"], diagnosis_id))


@router.get("/{diagnosis_id}/evidence", response_model=list[DiagnosisEvidence])
async def get_evidence(
    diagnosis_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    repository: Annotated[DiagnosisRepository, Depends(get_diagnosis_repository)],
) -> list[DiagnosisEvidence]:
    return _owned(lambda: repository.list_evidence(user["id"], diagnosis_id))


@router.get("/{diagnosis_id}/graph", response_model=IncidentGraph)
async def get_graph(
    diagnosis_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    repository: Annotated[DiagnosisRepository, Depends(get_diagnosis_repository)],
) -> IncidentGraph:
    return _owned(lambda: repository.get_graph(user["id"], diagnosis_id))


@router.get("/{diagnosis_id}/root-cause", response_model=DiagnosisRootCause | None)
async def get_root_cause(
    diagnosis_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    repository: Annotated[DiagnosisRepository, Depends(get_diagnosis_repository)],
) -> DiagnosisRootCause | None:
    return _owned(lambda: repository.get_root_cause(user["id"], diagnosis_id))


@router.get("/{diagnosis_id}/events")
async def diagnosis_events(
    diagnosis_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    repository: Annotated[DiagnosisRepository, Depends(get_diagnosis_repository)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """回放持久事件并持续推送；Last-Event-ID 支持浏览器断线续传。"""
    if repository.get(user["id"], diagnosis_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="diagnosis not found")
    try:
        header_cursor = int(last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid Last-Event-ID") from exc
    cursor = max(after, header_cursor)

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        idle_rounds = 0
        while True:
            events = repository.list_events(user["id"], diagnosis_id, cursor)
            if events:
                idle_rounds = 0
                for event in events:
                    cursor = event.id
                    payload = {
                        "id": event.id, "type": event.type,
                        "created_at": event.created_at, "data": event.data, **event.data,
                    }
                    yield f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                idle_rounds += 1
                session = repository.get(user["id"], diagnosis_id)
                terminal = session and session.status in {
                    DiagnosisStatus.COMPLETED, DiagnosisStatus.FAILED, DiagnosisStatus.CANCELLED,
                }
                if terminal and idle_rounds >= 2:
                    break
                if idle_rounds % 20 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _owned(callback):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="diagnosis not found") from exc
