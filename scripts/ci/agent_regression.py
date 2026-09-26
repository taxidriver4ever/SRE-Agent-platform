"""Run one frozen, deterministic Agent suite against two actual code revisions.

No models, cluster credentials or production data are needed. Live scenario evals
remain a separate staging acceptance operation, not a fabricated CI pass.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'sre-agent-backend/sre-agent'))
from app.validation.comparator import ValidationComparator
from app.validation.models import EnvironmentSpec, ValidationExecution
from app.validation.runner import normalize_junit


def decide(base, candidate):
    results, confidence = ValidationComparator().compare(base, candidate)
    left = {x.identity for x in base.tests}
    right = {x.identity for x in candidate.tests}
    incomplete = (not left or left != right or len(left) != len(base.tests)
                  or len(right) != len(candidate.tests)
                  or any(x.status.value in {'ERROR', 'SKIPPED'} for x in [*base.tests, *candidate.tests]))
    blocked = incomplete or confidence.value != 'HIGH' or any(r.classification.value in {
        'NEW_REGRESSION', 'AI_CONFIRMED_REGRESSION', 'BUILD_REGRESSION', 'COMPARISON_INCONCLUSIVE'
    } for r in results)
    return {'blocked': blocked, 'incomplete': incomplete, 'confidence': confidence.value,
            'results': [r.model_dump(mode='json') for r in results]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', required=True, type=Path)
    p.add_argument('--candidate', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    suite = output / 'suite'
    suite.mkdir()
    # Reuse existing diagnosis rule contracts, without the repository's DB fixture.
    source = ROOT / 'sre-agent-backend/sre-agent/tests/test_planning_rules.py'
    shutil.copy2(source, suite / 'test_agent_rules.py')
    shutil.copy2(ROOT / 'scripts/ci/test_agent_gate.py', suite / 'test_agent_gate.py')
    suite_hash = hashlib.sha256(b''.join(p.read_bytes() for p in sorted(suite.glob('*.py')))).hexdigest()
    deps = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], text=True)
    fingerprint = hashlib.sha256((sys.version + deps).encode()).hexdigest()
    environment = EnvironmentSpec(runner_image='github-hosted/python3.12', cpu_limit=2,
        memory_mb=4096, pids_limit=256, project_type='PYTHON', runtime_version=sys.version,
        dependency_mode='same resolved interpreter', network_policy='no external tools in suite',
        test_suite_hash=suite_hash, adapter_version='agent-ci-v1', environment_fingerprint=fingerprint)
    executions = []
    for side, checkout in [('BASE', args.base), ('CANDIDATE', args.candidate)]:
        checkout = checkout.resolve()
        sha = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
        report = output / f'{side.lower()}.xml'
        env = os.environ.copy()
        env['PYTHONPATH'] = str(checkout / 'sre-agent-backend/sre-agent')
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        command = [sys.executable, '-m', 'pytest', str(suite), '--noconftest', '-q',
                   '-o', f'cache_dir={output / "pytest-cache"}', f'--junitxml={report}']
        try:
            run = subprocess.run(command, cwd=suite, env=env, capture_output=True, text=True, timeout=180)
            (output / f'{side.lower()}.log').write_text(run.stdout + run.stderr, encoding='utf-8')
            status = 'COMPLETED' if run.returncode in (0, 1) else 'FAILED'
            code = run.returncode
        except subprocess.TimeoutExpired:
            status, code = 'TIMEOUT', -1
        tests = normalize_junit(output, (report.name,))
        executions.append(ValidationExecution(id=side, validation_id='ci', side=side,
            commit_sha=sha, idempotency_key=f'{sha}:{suite_hash}', status=status,
            test_status='PASSED' if code == 0 else 'FAILED', exit_code=code,
            environment=environment, tests=tests))
    result = decide(*executions)
    result.update(base_sha=executions[0].commit_sha, candidate_sha=executions[1].commit_sha,
                  suite_hash=suite_hash, environment_fingerprint=fingerprint)
    (output / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'blocked': result['blocked'], 'cases': len(result['results']),
                      'classifications': sorted({r['classification'] for r in result['results']})}))
    return 1 if result['blocked'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
