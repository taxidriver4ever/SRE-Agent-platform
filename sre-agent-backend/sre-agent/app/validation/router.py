"""Pre-Merge Validation REST、Branch 下拉与持久化 SSE API。"""

import asyncio
import json
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.router import get_tool_policy, require_project
from app.auth import CurrentUser, require_user
from app.security import ToolPolicy
from app.validation.models import (
    BranchListResponse, InterfaceTestGenerationRequest, InterfaceTestGenerationResponse,
    ManagedTestSuite, ManagedTestSuiteCreateRequest,
    ManagedTestSuiteListResponse, ManagedTestSuiteMetadataRequest,
    ManagedTestSuiteVersionCreateRequest, RepositoryInterfacesResponse, RepositoryTestFilesResponse,
    ValidationCreateRequest, ValidationCreatedResponse, ValidationListResponse,
)
from app.validation.service import ValidationRequestError, ValidationService

router = APIRouter(tags=["pre-merge-validation"])


def get_service(request: Request) -> ValidationService:
    return request.app.state.validation_service


@router.get("/api/repositories/{repository}/branches", response_model=BranchListResponse)
async def branches(repository: str, user: Annotated[CurrentUser, Depends(require_user)],
                   service: Annotated[ValidationService, Depends(get_service)]) -> BranchListResponse:
    try:
        return BranchListResponse(repository=repository, branches=await service.registry.list_branches(repository))
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/api/repositories/{repository}/test-files", response_model=RepositoryTestFilesResponse)
async def repository_test_files(repository: str, ref: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]) -> RepositoryTestFilesResponse:
    try:
        return await service.repository_test_files(repository, ref)
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/api/repositories/{repository}/interfaces", response_model=RepositoryInterfacesResponse)
async def repository_interfaces(repository: str, base_ref: str, candidate_ref: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]) -> RepositoryInterfacesResponse:
    try:
        return await service.repository_interfaces(repository,base_ref,candidate_ref)
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/api/validation-test-suites", response_model=ManagedTestSuite, status_code=201)
async def create_test_suite(body: ManagedTestSuiteCreateRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]) -> ManagedTestSuite:
    try:
        return service.create_test_suite(user["id"], body)
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/api/validation-test-suites", response_model=ManagedTestSuiteListResponse)
async def list_test_suites(user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)],
    repository: str | None = None, include_archived: bool = False,
    limit: int = Query(100, ge=1, le=100)) -> ManagedTestSuiteListResponse:
    return ManagedTestSuiteListResponse(items=service.repository.list_test_suites(
        user["id"],repository,include_archived,limit,
    ))


@router.get("/api/validation-test-suites/{suite_id}", response_model=ManagedTestSuite)
async def test_suite_detail(suite_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]) -> ManagedTestSuite:
    suite = service.repository.get_test_suite(user["id"], suite_id)
    if suite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="test suite not found")
    return suite


@router.post("/api/validation-test-suites/{suite_id}/versions",
             response_model=ManagedTestSuite, status_code=201)
async def add_test_suite_version(suite_id: str, body: ManagedTestSuiteVersionCreateRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]) -> ManagedTestSuite:
    try:
        return service.add_test_suite_version(user["id"], suite_id, body)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/api/validation-test-suites/{suite_id}/metadata",
             response_model=ManagedTestSuite)
async def update_test_suite_metadata(suite_id: str, body: ManagedTestSuiteMetadataRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]) -> ManagedTestSuite:
    try:
        return service.update_test_suite_metadata(user["id"], suite_id, body)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/api/validation-test-suites/{suite_id}/archive")
async def archive_test_suite(suite_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]):
    if not service.repository.archive_test_suite(user["id"], suite_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="test suite is missing or already archived")
    return {"id":suite_id,"archived":True}


@router.post("/api/interface-test-suites/generate",
             response_model=InterfaceTestGenerationResponse,status_code=202)
async def generate_interface_test_suite(body: InterfaceTestGenerationRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    project_id: str = Query(default="sre-lab")) -> InterfaceTestGenerationResponse:
    require_project(policy,project_id)
    try:
        return await service.generate_interface_test_suite(
            user["id"],project_id,body,idempotency_key,
        )
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/api/validations", response_model=ValidationCreatedResponse, status_code=202)
async def create_validation(body: ValidationCreateRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    project_id: str = Query(default="sre-lab")) -> ValidationCreatedResponse:
    require_project(policy, project_id)
    try:
        run = await service.create(user["id"], project_id, body, idempotency_key)
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return ValidationCreatedResponse(id=run.id, status=run.status,
        events_url=f"/api/validations/{run.id}/events", detail_url=f"/api/validations/{run.id}")


@router.get("/api/validations", response_model=ValidationListResponse)
async def list_validations(user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)], limit: int = Query(50, ge=1, le=100)):
    return ValidationListResponse(items=service.repository.list_for_user(user["id"], limit))


@router.get("/api/validations/{validation_id}")
async def validation_detail(validation_id: str, user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]):
    run = service.repository.detail(user["id"], validation_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="validation not found")
    return run


@router.get("/api/validations/{validation_id}/events")
async def validation_events(validation_id: str, request: Request,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)],
    after: int = Query(0, ge=0)) -> StreamingResponse:
    if service.repository.get(user["id"], validation_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="validation not found")

    async def stream() -> AsyncIterator[str]:
        cursor = after
        while not await request.is_disconnected():
            events = service.repository.list_events(user["id"], validation_id, cursor)
            for event in events:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
            run = service.repository.get(user["id"], validation_id)
            if run and run.status.value in {"COMPLETED", "FAILED", "CANCELLED"}:
                yield f"event: done\ndata: {json.dumps({'status': run.status.value})}\n\n"
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/validations/{validation_id}/diagnose", status_code=202)
async def diagnose_regression(validation_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)],
    policy: Annotated[ToolPolicy, Depends(get_tool_policy)],
    project_id: str = Query(default="sre-lab")):
    require_project(policy, project_id)
    try:
        diagnosis_id = await service.diagnose(user["id"], project_id, validation_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValidationRequestError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"diagnosis_id": diagnosis_id, "detail_url": f"/api/diagnoses/{diagnosis_id}"}


@router.post("/api/validations/{validation_id}/cancel")
async def cancel_validation(validation_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ValidationService, Depends(get_service)]):
    if service.repository.get(user["id"], validation_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="validation not found")
    if not service.manager.cancel(user["id"], validation_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="validation is already terminal")
    return {"id": validation_id, "status": "CANCELLED"}
