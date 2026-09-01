"""Intent 到诊断、整体巡检或普通回复的唯一分流层。"""

from app.conversation import ConversationService
from app.intent.models import IntentDecision, IntentReply, SREIntent
from app.intent.router import IntentRouter
from app.workflow import DiagnosisReport, DiagnosisWorkflow
from app.workflow.diagnosis import EventCallback


class IntentWorkflowRouter:
    """保证合法运维意图确认前不会进入 DiagnosisWorkflow。"""

    def __init__(
        self,
        intent_router: IntentRouter,
        diagnosis_workflow: DiagnosisWorkflow,
        conversation_service: ConversationService | None = None,
    ) -> None:
        self.intent_router = intent_router
        self.diagnosis_workflow = diagnosis_workflow
        self.conversation_service = conversation_service

    async def dispatch(
        self,
        message: str,
        on_event: EventCallback | None = None,
        *,
        conversation_id: str | None = None,
        user_id: str | None = None,
        selected_services: list[str] | None = None,
    ) -> DiagnosisReport | IntentReply:
        selected = list(dict.fromkeys(selected_services or []))
        classification_message = message
        if selected:
            classification_message = f"{message}\n用户选择的初始服务范围：{', '.join(selected)}"
        decision = await self.intent_router.classify(classification_message)
        if on_event:
            await on_event({"type": "intent", **decision.model_dump(mode="json")})

        if decision.intent in {SREIntent.SPECIFIC_INCIDENT, SREIntent.GENERAL_DIAGNOSIS}:
            return await self.diagnosis_workflow.run(
                message,
                on_event,
                conversation_id=conversation_id,
                user_id=user_id,
                target=decision.target or (selected[0] if selected else None),
                symptom=decision.symptom,
                system_scan=decision.intent is SREIntent.GENERAL_DIAGNOSIS,
                selected_services=selected,
            )

        reply = self._reply(decision, conversation_id)
        self._persist_non_diagnosis(message, reply, user_id, conversation_id)
        if on_event:
            await on_event(reply.model_dump(mode="json"))
        return reply

    @staticmethod
    def _reply(decision: IntentDecision, conversation_id: str | None) -> IntentReply:
        if decision.intent is SREIntent.OUT_OF_SCOPE:
            message = "当前仅支持运维、系统巡检和故障排查问题。请描述服务异常、指标变化、日志报错或运行环境。"
        else:
            message = "请补充要排查的服务或系统范围、具体现象、发生时间，以及是否伴随错误率、延迟或重启变化。"
        return IntentReply(
            intent=decision.intent,
            message=message,
            conversation_id=conversation_id,
            target=decision.target,
            symptom=decision.symptom,
        )

    def _persist_non_diagnosis(
        self,
        message: str,
        reply: IntentReply,
        user_id: str | None,
        conversation_id: str | None,
    ) -> None:
        if not self.conversation_service or not user_id or not conversation_id:
            return
        self.conversation_service.append(
            user_id, conversation_id, "user", {"message": message}, message_type="user"
        )
        self.conversation_service.append(
            user_id,
            conversation_id,
            "assistant",
            {"message": reply.message, "intent": reply.intent.value},
            message_type="assistant",
        )
