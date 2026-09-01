"""Diagnosis Session 应用服务与状态机。"""

from __future__ import annotations

from app.conversation import ConversationService
from app.diagnosis.models import DiagnosisSession, DiagnosisStatus
from app.diagnosis.repository import DiagnosisRepository
from app.diagnosis.schemas import DiagnosisCreateRequest


class DiagnosisService:
    """协调 Conversation 与 Diagnosis 聚合，保持合法状态迁移。"""

    _TRANSITIONS = {
        DiagnosisStatus.PENDING: {DiagnosisStatus.INVESTIGATING, DiagnosisStatus.CANCELLED, DiagnosisStatus.FAILED},
        DiagnosisStatus.INVESTIGATING: {DiagnosisStatus.COMPLETED, DiagnosisStatus.FAILED, DiagnosisStatus.CANCELLED},
        DiagnosisStatus.COMPLETED: set(),
        DiagnosisStatus.FAILED: set(),
        DiagnosisStatus.CANCELLED: set(),
    }

    def __init__(self, repository: DiagnosisRepository, conversations: ConversationService) -> None:
        self.repository = repository
        self.conversations = conversations

    def create(self, user_id: str, request: DiagnosisCreateRequest) -> DiagnosisSession:
        conversation = self.conversations.create(user_id, f"Incident · {request.question[:80]}")
        session = self.repository.create(
            user_id, str(conversation["id"]), request.question,
            request.trigger_type.value, request.initial_target,
        )
        self.repository.append_event(session.id, "diagnosis.created", {
            "diagnosis_id": session.id,
            "trigger_type": session.trigger_type.value,
            "initial_target": session.initial_target.model_dump(mode="json") if session.initial_target else None,
            "status": session.status.value,
        })
        return session

    def get_detail(self, user_id: str, diagnosis_id: str) -> DiagnosisSession | None:
        session = self.repository.get(user_id, diagnosis_id)
        if session is None:
            return None
        session.steps = self.repository.list_steps(user_id, diagnosis_id)
        session.evidence = self.repository.list_evidence(user_id, diagnosis_id)
        session.graph = self.repository.get_graph(user_id, diagnosis_id)
        session.root_cause = self.repository.get_root_cause(user_id, diagnosis_id)
        return session

    def transition(self, user_id: str, diagnosis_id: str, target: DiagnosisStatus) -> DiagnosisSession:
        session = self.repository.get(user_id, diagnosis_id)
        if session is None:
            raise KeyError("diagnosis not found")
        if target is session.status:
            return session
        if target not in self._TRANSITIONS[session.status]:
            raise ValueError(f"invalid diagnosis transition: {session.status.value} -> {target.value}")
        self.repository.update_session(
            diagnosis_id, status=target,
            started=target is DiagnosisStatus.INVESTIGATING,
            finished=target in {DiagnosisStatus.COMPLETED, DiagnosisStatus.FAILED, DiagnosisStatus.CANCELLED},
        )
        return self.repository.get(user_id, diagnosis_id)  # type: ignore[return-value]

    def fail(self, user_id: str, diagnosis_id: str, error: Exception) -> None:
        session = self.repository.get(user_id, diagnosis_id)
        if session is None or session.status in {DiagnosisStatus.COMPLETED, DiagnosisStatus.CANCELLED}:
            return
        self.repository.update_session(
            diagnosis_id, status=DiagnosisStatus.FAILED,
            error_message=self._error_text(error), finished=True,
        )
        self.repository.append_event(diagnosis_id, "diagnosis.failed", {
            "diagnosis_id": diagnosis_id,
            "status": DiagnosisStatus.FAILED.value,
            "message": self._error_text(error),
        })

    @staticmethod
    def _error_text(error: Exception) -> str:
        detail = str(error).strip()
        return (f"{error.__class__.__name__}: {detail}" if detail else error.__class__.__name__)[:4000]
