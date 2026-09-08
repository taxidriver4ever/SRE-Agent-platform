"""Authenticated Agent Runtime self-check API."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth import CurrentUser, require_user
from app.diagnosis.models import DiagnosisSelfCheckReport
from app.diagnosis.self_check import DiagnosisSelfCheckService

router = APIRouter(prefix="/api/system", tags=["system"])


def get_self_check_service(request: Request) -> DiagnosisSelfCheckService:
    return request.app.state.diagnosis_self_check


@router.get("/self-check", response_model=DiagnosisSelfCheckReport)
async def diagnosis_runtime_self_check(
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[DiagnosisSelfCheckService, Depends(get_self_check_service)],
    level: int = Query(default=3, ge=1, le=3),
) -> DiagnosisSelfCheckReport:
    """只扫描当前用户的 Diagnosis；不会泄露其他用户的 Session ID。"""
    return service.check(level=level, user_id=user["id"])
