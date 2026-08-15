"""只使用 LLM Structured Output 的第一版 SRE Intent Router。"""

from app.intent.models import IntentDecision, SREIntent
from app.llm import LLM, LLMMessage
from app.llm.structured_output import (
    StructuredOutputError,
    schema_retry_message,
    template_refill_message,
    validate_structured_output,
)


class IntentRouter:
    """在工具和诊断 Runtime 之前完成安全的请求分类。"""

    def __init__(self, llm: LLM, structured_output_retries: int = 3) -> None:
        self.llm = llm
        self.structured_output_retries = max(2, min(3, structured_output_retries))
        self.last_failed_raw_output: str = ""

    async def classify(self, message: str) -> IntentDecision:
        """分类用户消息；所有结构修复失败时安全降级为要求澄清。"""
        messages = [
            LLMMessage(
                "system",
                "你是 SRE 请求意图路由器，不是诊断 Agent，禁止调用或建议调用任何工具。"
                "只能返回一个 JSON 对象，字段严格为 intent、target、symptom。"
                "intent 只能是 SPECIFIC_INCIDENT、GENERAL_DIAGNOSIS、NEED_CLARIFICATION、OUT_OF_SCOPE。"
                "具体服务故障选 SPECIFIC_INCIDENT；系统整体巡检选 GENERAL_DIAGNOSIS；"
                "缺少可执行的现象或范围选 NEED_CLARIFICATION；非运维/故障排查选 OUT_OF_SCOPE。"
                "target 尽量使用规范服务名，未知为 null；symptom 使用简短 snake_case，未知为 null。"
                "不要输出解释或 Markdown。/no_think",
            ),
            LLMMessage("user", message),
        ]
        first_output = ""
        for attempt in range(self.structured_output_retries + 1):
            response = await self.llm.complete(messages)
            if not first_output:
                first_output = response.content
            messages.append(LLMMessage("assistant", response.content or "{}"))
            try:
                decision = validate_structured_output(response.content, IntentDecision)
                self.last_failed_raw_output = ""
                return decision
            except StructuredOutputError as exc:
                if attempt < self.structured_output_retries:
                    messages.append(LLMMessage("user", schema_retry_message(exc)))

        template = IntentDecision(
            intent=SREIntent.NEED_CLARIFICATION,
            target=None,
            symptom=None,
        ).model_dump(mode="json")
        messages.append(LLMMessage("user", template_refill_message(template, first_output)))
        refill = await self.llm.complete(messages)
        try:
            decision = validate_structured_output(refill.content, IntentDecision)
            self.last_failed_raw_output = ""
            return decision
        except StructuredOutputError:
            # 分类不可信时绝不放行工具；保留原始输出供服务日志/测试诊断。
            self.last_failed_raw_output = first_output
            return IntentDecision(intent=SREIntent.NEED_CLARIFICATION)
