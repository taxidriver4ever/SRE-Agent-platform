from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_regression import decide
from app.validation.models import ValidationExecution, EnvironmentSpec, TestCaseResult as CaseResult


def execution(side,status):
    env=EnvironmentSpec(runner_image='test',cpu_limit=1,memory_mb=256,pids_limit=64,
        project_type='PYTHON',runtime_version='3.12',dependency_mode='fixed',network_policy='none',
        test_suite_hash='same',adapter_version='1',environment_fingerprint='same')
    return ValidationExecution(id=side,validation_id='test',side=side,commit_sha='a'*40,idempotency_key=side,
        status='COMPLETED',environment=env,tests=[CaseResult(suite='frozen',name='case',status=status)])


@pytest.mark.parametrize('base,candidate,classification,blocked',[
    ('PASSED','FAILED','NEW_REGRESSION',True),
    ('FAILED','PASSED','POSSIBLE_FIX',False),
    ('PASSED','PASSED','UNCHANGED_PASS',False),
    ('PASSED','SKIPPED','UNCHANGED_PASS',True),
])
def test_real_comparator_gate(base,candidate,classification,blocked):
    result=decide(execution('BASE',base),execution('CANDIDATE',candidate))
    assert result['blocked']==blocked
    assert result['results'][0]['classification']==classification


def test_missing_cases_fail_closed():
    candidate=execution('CANDIDATE','PASSED');candidate.tests=[]
    assert decide(execution('BASE','PASSED'),candidate)['blocked']


def test_duplicate_case_identity_fails_closed():
    candidate=execution('CANDIDATE','PASSED')
    candidate.tests.append(candidate.tests[0].model_copy())
    assert decide(execution('BASE','PASSED'),candidate)['blocked']
