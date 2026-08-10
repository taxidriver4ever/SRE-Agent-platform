"""受 Bearer Token 保护的 MinIO 预签名上传与下载路由。"""

from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import CurrentUser, require_user
from app.uploads.schemas import (
    CompleteUploadRequest,
    DownloadUrlResponse,
    PresignUploadRequest,
    PresignUploadResponse,
    UploadObjectResponse,
)
from app.uploads.service import UploadNotFoundError, UploadService, UploadValidationError


router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def get_upload_service(request: Request) -> UploadService:
    """从应用生命周期获取共享的上传服务。"""
    return request.app.state.upload_service


@router.post("/presign", response_model=PresignUploadResponse)
def create_presigned_upload(
    body: PresignUploadRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> PresignUploadResponse:
    """为当前会话的一个文件签发 15 分钟 PUT URL，文件不经过 FastAPI。"""
    try:
        item = service.create_presigned_upload(
            user_id=user["id"],
            conversation_id=body.conversation_id,
            filename=body.filename,
            content_type=body.content_type,
            size=body.size,
            kind=body.kind,
        )
        return PresignUploadResponse.model_validate(item)
    except UploadNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UploadValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/complete", response_model=UploadObjectResponse)
def complete_upload(
    body: CompleteUploadRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadObjectResponse:
    """PUT 成功后校验对象并绑定会话；此时数据库才写入 ``oss_key``。"""
    try:
        item = service.complete(
            user_id=user["id"],
            conversation_id=body.conversation_id,
            oss_key=body.oss_key,
            expected_size=body.expected_size,
        )
        return UploadObjectResponse.model_validate(item)
    except UploadNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UploadValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/download", response_model=DownloadUrlResponse)
def create_download_url(
    oss_key: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> DownloadUrlResponse:
    """按已编码 Query 参数中的 Key 生成一次短时效下载地址。"""
    try:
        return DownloadUrlResponse.model_validate(
            service.create_download_url(user_id=user["id"], oss_key=unquote(oss_key))
        )
    except UploadNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
