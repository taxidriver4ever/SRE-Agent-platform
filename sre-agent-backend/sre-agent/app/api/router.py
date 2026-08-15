"""Agent HTTP 路由、SSE 诊断流及领域异常到 HTTP 状态码的转换。"""

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.agent import AgentMaxIterationsError, ToolAgent
from app.agent.schemas import AgentResult
from app.api.schemas import AgentRunRequest, DiagnosisChatRequest
from app.auth import CurrentUser, require_user
from app.conversation.router import get_conversation_service
from app.conversation.service import ConversationService
from app.conversation_memory import conversation_memory_scope
from app.llm import GatewayConfigurationError, GatewayRequestError
from app.workflow import DiagnosisReport, DiagnosisWorkflow

# 所有 Agent 能力统一挂在版本化前缀下，后续协议演进可保留 v1 兼容性。
router = APIRouter(prefix="/v1/agent", tags=["agent"])
# 新的诊断聊天接口保持任务指定的 /api/agent/chat 路径，旧 v1 ReAct API 继续兼容。
chat_router = APIRouter(prefix="/api/agent", tags=["sre-diagnosis"])


def get_agent(request: Request) -> ToolAgent:
    """从 FastAPI 应用状态中取得生命周期内共享的 Agent 实例。

    作为依赖函数声明后，测试可以通过 FastAPI dependency_overrides 替换它。
    """
    return request.app.state.agent


def get_workflow(request: Request) -> DiagnosisWorkflow:
    """取得应用生命周期内共享的硬性 SRE 工作流。"""
    return request.app.state.diagnosis_workflow


@router.post("/run", response_model=AgentResult)
async def run_agent(
    body: AgentRunRequest,
    agent: Annotated[ToolAgent, Depends(get_agent)],
    _user: Annotated[CurrentUser, Depends(require_user)],
) -> AgentResult:
    """执行一个用户问题并返回答案、工具轨迹及 Token 统计。

    HTTP 层不参与推理，只负责输入验证、调用 ToolAgent 和异常语义转换。
    """
    try:
        return await agent.run(body.query)
    except GatewayConfigurationError as exc:
        # 缺少 Gateway Token 属于服务端未配置，而非用户请求格式错误。
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except GatewayRequestError as exc:
        # Agent 作为网关的下游服务，将上游调用失败统一表示为 502。
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except AgentMaxIterationsError as exc:
        # 达到推理轮数上限表示本次任务未能在限定时间/预算内完成。
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc


@chat_router.post("/chat", response_model=DiagnosisReport)
async def diagnose(
    body: DiagnosisChatRequest,
    workflow: Annotated[DiagnosisWorkflow, Depends(get_workflow)],
    user: Annotated[CurrentUser, Depends(require_user)],
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
) -> DiagnosisReport:
    """同步返回完整的结构化诊断报告，适合脚本、评测和 API 测试。"""
    try:
        conversation_id = conversations.ensure(user["id"], body.conversation_id, body.message)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="conversation not found") from exc
    with conversation_memory_scope(user["id"], conversation_id):
        report = await workflow.run(
            body.message,
            conversation_id=conversation_id,
            user_id=user["id"],
        )
    report.conversation_id = conversation_id
    return report


@chat_router.post("/chat/stream")
async def diagnose_stream(
    body: DiagnosisChatRequest,
    workflow: Annotated[DiagnosisWorkflow, Depends(get_workflow)],
    user: Annotated[CurrentUser, Depends(require_user)],
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
) -> StreamingResponse:
    """以 SSE 逐步发送 phase、tool、final 事件，供问答页面实时展示。"""
    try:
        conversation_id = conversations.ensure(user["id"], body.conversation_id, body.message)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="conversation not found") from exc
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def publish(event: dict[str, object]) -> None:
        """工作流先完成持久化，再把事件入队发送。"""
        if event.get("type") == "final" and isinstance(event.get("report"), dict):
            report = event["report"]
            report["conversation_id"] = conversation_id
        await queue.put(event)

    async def execute() -> None:
        """后台执行工作流，并把未预期错误转换为可读 SSE 事件。"""
        try:
            with conversation_memory_scope(user["id"], conversation_id):
                await workflow.run(
                    body.message,
                    publish,
                    conversation_id=conversation_id,
                    user_id=user["id"],
                )
        except Exception as exc:  # HTTP 流已开始，只能通过事件报告错误。
            conversations.append(
                user["id"], conversation_id, "assistant", {"error": str(exc)[:1000]}
            )
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put({"type": "done"})

    async def event_stream():
        """按标准 SSE data 帧编码 JSON，直到收到内部 done 哨兵。"""
        task = asyncio.create_task(execute())
        try:
            # 浏览器立即获得服务器确认的 ID，后续请求可追加到同一持久会话。
            yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
            while True:
                event = await queue.get()
                if event.get("type") == "done":
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 浏览器中断连接时取消后台诊断，避免无消费者任务继续占用工具预算。
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@chat_router.get("/evidence/{run_id}/{evidence_id}")
async def get_evidence(
    run_id: str,
    evidence_id: str,
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
    user: Annotated[CurrentUser, Depends(require_user)],
) -> dict[str, object]:
    """只回读当前用户会话中的原始 Tool Result。"""
    item = conversations.get_tool_result(user["id"], run_id, evidence_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="evidence not found or expired")
    return item
