"""Agent HTTP API 的输入模型。"""

from pydantic import BaseModel, Field, field_validator


class AgentRunRequest(BaseModel):
    """启动一次无状态 Agent 运行的请求。"""

    query: str = Field(
        min_length=1,
        max_length=20_000,
        description="需要 Agent 分析并处理的用户问题",
        examples=["计算 (12 + 8) * 3"],
    )
    project_id: str = Field(default="sre-lab", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")


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
    # project_id 只选择服务端白名单项目，不能携带 namespace、repo、path 或凭证。
    project_id: str = Field(default="sre-lab", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    # 仅作为调查起点提示，可为空、单选或多选；Agent 仍可沿证据扩展到其他服务。
    selected_services: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("selected_services")
    @classmethod
    def normalize_selected_services(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        for service in normalized:
            if len(service) > 120 or not service.replace("-", "").replace("_", "").isalnum():
                raise ValueError("selected service name is invalid")
        return normalized
