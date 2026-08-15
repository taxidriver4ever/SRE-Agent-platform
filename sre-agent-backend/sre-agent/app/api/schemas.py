"""Agent HTTP API 的输入模型。"""

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    """启动一次无状态 Agent 运行的请求。"""

    query: str = Field(
        min_length=1,
        max_length=20_000,
        description="需要 Agent 分析并处理的用户问题",
        examples=["计算 (12 + 8) * 3"],
    )


class DiagnosisChatRequest(BaseModel):
    """启动一次 SRE 证据诊断的聊天请求。"""

    message: str = Field(
        min_length=1,
        max_length=20_000,
        description="自然语言故障问题；工作流会自行识别服务与症状",
        examples=["为什么订单模块最近很慢？"],
    )
    # 首次提问可省略，由服务器创建会话；后续提问必须回传 SSE conversation 事件中的 ID。
    conversation_id: str | None = Field(default=None, min_length=32, max_length=64)
