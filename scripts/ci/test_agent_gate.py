"""Frozen replay contracts copied by the Base/Candidate harness."""
from datetime import datetime, timezone
from app.workflow.models import DiagnosisState, Evidence, DiagnosisSynthesis
from app.workflow.evidence_gate import build_report


def test_unverified_llm_conclusion_is_not_confirmed():
    state = DiagnosisState(query='database timeout', synthesis=DiagnosisSynthesis(
        status='confirmed', root_cause='pool exhausted', confidence=.9, evidence_ids=['invented']))
    assert build_report(state).status == 'insufficient_evidence'


def test_current_direct_evidence_can_confirm_but_history_cannot():
    state = DiagnosisState(query='database timeout', synthesis=DiagnosisSynthesis(
        status='confirmed', root_cause='pool exhausted', confidence=.9, evidence_ids=['a', 'b']))
    state.evidence = [Evidence(source='MySQL', tool_name='query_slow_queries', title='pool', detail='pool',
        timestamp=datetime.now(timezone.utc), evidence_id=k, direct_evidence=True, supports_conclusion=True)
        for k in ['a', 'b']]
    assert build_report(state).status == 'confirmed'
    state.evidence[1].supports_conclusion = False
    assert build_report(state).status == 'insufficient_evidence'
