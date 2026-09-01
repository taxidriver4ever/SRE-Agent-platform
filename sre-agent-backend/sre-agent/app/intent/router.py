"""SRE Intent Router：明确服务故障走确定性快路径，其余由 LLM 分类。"""

import re

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

    def __init__(
        self,
        llm: LLM,
        structured_output_retries: int = 3,
        service_names: list[str] | tuple[str, ...] | None = None,
        service_aliases: dict[str, str] | None = None,
    ) -> None:
        self.llm = llm
        self.structured_output_retries = max(2, min(3, structured_output_retries))
        self.service_names = tuple(sorted(set(service_names or ()), key=len, reverse=True))
        self.service_aliases = dict(service_aliases or {})
        self.last_failed_raw_output: str = ""

    async def classify(self, message: str) -> IntentDecision:
        """分类用户消息；所有结构修复失败时安全降级为要求澄清。"""
        fast_decision = self._specific_incident_fast_path(message)
        if fast_decision is not None:
            return fast_decision
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

    def _specific_incident_fast_path(self, message: str) -> IntentDecision | None:
        """对“规范服务名 + 明确故障现象”直接路由，避免浪费一次本地模型调用。

        这里只识别输入中真实出现的 ``*-service``，不维护评测 Case 或故障答案；
        模糊中文服务名、整体巡检和非运维请求仍交给结构化 LLM Router。
        """
        text = message.lower()
        target_match = re.search(r"(?<![a-z0-9-])([a-z][a-z0-9-]*-service)(?![a-z0-9-])", text)
        target = target_match.group(1) if target_match is not None else None
        if target is None:
            for alias, service_name in sorted(
                self.service_aliases.items(), key=lambda item: len(item[0]), reverse=True
            ):
                lowered_alias = alias.lower()
                if lowered_alias.isascii() and lowered_alias.replace("-", "").isalnum():
                    matched = re.search(
                        rf"(?<![a-z0-9-]){re.escape(lowered_alias)}(?![a-z0-9-])", text
                    ) is not None
                else:
                    matched = lowered_alias in text
                if matched:
                    target = service_name
                    break
        if target is None:
            for service_name in self.service_names:
                short_name = service_name.removesuffix("-service")
                if re.search(rf"(?<![a-z0-9-]){re.escape(short_name)}(?![a-z0-9-])", text):
                    target = service_name
                    break
        if target is None:
            return None
        symptom_groups = (
            ("pod_restart", ("重启", "restart", "oom", "内存")),
            ("dependency_timeout", ("超时", "timeout", "重试", "retry", "依赖")),
            ("latency", ("延迟", "耗时", "慢", "latency", "slow")),
            ("5xx", ("5xx", "500", "错误", "失败", "error", "failure")),
        )
        symptom = next((name for name, words in symptom_groups if any(word in text for word in words)), None)
        if symptom is None:
            return None
        return IntentDecision(
            intent=SREIntent.SPECIFIC_INCIDENT,
            target=target,
            symptom=symptom,
        )
