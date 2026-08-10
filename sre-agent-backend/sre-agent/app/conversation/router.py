"""会话列表、创建与历史详情 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import CurrentUser, require_user
from app.conversation.schemas import CreateConversationRequest, ConversationDetail, ConversationSummary
from app.conversation.service import ConversationService


router = APIRouter(prefix="/api/conversations", tags=["conversation"])


def get_conversation_service(request: Request) -> ConversationService:
    """从应用生命周期中获取共享 ConversationService。"""
    return request.app.state.conversation_service


@router.get("", response_model=list[ConversationSummary])
def list_conversations(
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> list[ConversationSummary]:
    """登录恢复后首先调用，仅加载当前用户最近 50 个会话摘要。"""
    return [ConversationSummary.model_validate(item) for item in service.list_for_user(user["id"])]


@router.post("", response_model=ConversationSummary, status_code=status.HTTP_201_CREATED)
def create_conversation(
    body: CreateConversationRequest,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationSummary:
    """显式创建一个空会话，适合前端“新建诊断”操作。"""
    return ConversationSummary.model_validate(service.create(user["id"], body.title))


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    user: Annotated[CurrentUser, Depends(require_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ConversationDetail:
    """读取当前用户的一段完整历史，用于从缓存列表切换会话。"""
    item = service.get(user["id"], conversation_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="conversation not found")
    return ConversationDetail.model_validate(item)
