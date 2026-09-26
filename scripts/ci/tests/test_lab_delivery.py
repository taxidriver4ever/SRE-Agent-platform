from pathlib import Path
import shutil
import subprocess
import sys
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
from update_gitops import update, LAB_COMPONENTS
from lab_changes import select


def test_lab_partial_release_preserves_other_images_and_platform(tmp_path):
    root = tmp_path / 'repo'
    shutil.copytree(ROOT / 'deploy/gitops-template', root)
    platform = (root / 'environments/dev/values.yaml').read_bytes()
    digests = {image: 'sha256:' + 'b' * 64 for image in LAB_COMPONENTS.values()}
    target = update(root, 'dev', 'a' * 40, 'ghcr.io/example', digests, 'lab')
    initial = yaml.safe_load(target.read_text())
    initial['services']['order-service']['config'] = {'DB_USERNAME': 'custom_user'}
    initial['databaseSecret'] = 'custom-lab-secret'
    target.write_text(yaml.safe_dump(initial))
    update(root, 'dev', 'c' * 40, 'ghcr.io/example', {'sre-lab-order-service': 'sha256:' + 'd' * 64}, 'lab')
    changed = yaml.safe_load(target.read_text())
    assert changed['services']['order-service']['previousSourceSha'] == 'a' * 40
    assert changed['services']['order-service']['image']['tag'] == 'c' * 40
    assert changed['services']['order-service']['config'] == {'DB_USERNAME': 'custom_user'}
    assert changed['databaseSecret'] == 'custom-lab-secret'
    for name in LAB_COMPONENTS:
        if name != 'order-service':
            assert changed['services'][name] == initial['services'][name]
    before = target.read_bytes()
    update(root, 'dev', 'c' * 40, 'ghcr.io/example', {'sre-lab-order-service': 'sha256:' + 'd' * 64}, 'lab')
    assert target.read_bytes() == before
    assert platform == (root / 'environments/dev/values.yaml').read_bytes()


@pytest.mark.parametrize('digests', [None, {}, {'sre-agent': 'sha256:' + 'a' * 64},
    {'sre-lab-order-service': 'sha256:' + 'a' * 64},
    {image: 'invalid' for image in LAB_COMPONENTS.values()}])
def test_invalid_or_incomplete_lab_release_never_writes(tmp_path, digests):
    root = tmp_path / 'repo'
    shutil.copytree(ROOT / 'deploy/gitops-template', root)
    target = root / 'environments/dev/lab-values.yaml'
    before = target.read_bytes()
    with pytest.raises(ValueError):
        update(root, 'dev', 'a' * 40, 'ghcr.io/example', digests, 'lab')
    assert before == target.read_bytes()


def test_paths_use_per_service_release_baseline(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init')
    git('config', 'user.name', 'test')
    git('config', 'user.email', 'test@example.invalid')
    def commit(path):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('change')
        git('add', '.')
        git('commit', '-m', path)
        return git('rev-parse', 'HEAD')
    base = commit('README.md')
    first = commit('sre-broken-system/order-service/new-file')
    assert select(tmp_path, first, base) == ['order-service']
    second = commit('sre-broken-system/user-service/new-file')
    desired = tmp_path / 'desired.yaml'
    desired.write_text(yaml.safe_dump({'services': {n: {'image': {'tag': base}} for n in LAB_COMPONENTS}}))
    # The previous failed run's order change must survive a later user-only push.
    assert select(tmp_path, second, first, desired) == ['order-service', 'user-service']
    desired.unlink()
    shared = commit('sre-broken-system/sre-lab-infra/shared-config')
    assert set(select(tmp_path, shared, second)) == set(LAB_COMPONENTS)
    assert set(select(tmp_path, shared, '', tmp_path / 'missing.yaml')) == set(LAB_COMPONENTS)


def test_lab_pipeline_is_separate_and_scans_before_publishing():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))['jobs']
    assert len(jobs['build-images']['strategy']['matrix']['include']) == 3
    lab = jobs['build-lab-images']
    assert lab['needs'] == ['detect-lab-changes', 'delivery-contracts']
    steps = lab['steps']
    build = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('docker/build-push'))
    runtime = next(i for i, s in enumerate(steps) if 'lab_runtime_check.py' in s.get('run', ''))
    scan = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('aquasecurity/trivy'))
    push = next(i for i, s in enumerate(steps) if s.get('run') == 'docker push "$IMAGE_REF"')
    assert build < runtime < scan < push
    assert steps[build]['with']['load'] and not steps[build]['with']['push']
    assert steps[scan]['with']['exit-code'] == '1'
    assert steps[scan]['with']['ignore-unfixed'] is False
    assert "github.event_name == 'push'" in steps[push]['if']
    download = next(s for s in jobs['update-lab-gitops']['steps'] if s.get('uses', '').startswith('actions/download-artifact'))
    assert download['with']['pattern'] == 'lab-digest-*'
    assert jobs['update-lab-gitops']['concurrency']['group'] != jobs['update-gitops']['concurrency']['group']


@pytest.mark.parametrize('problem', ['wrong-source', 'duplicate', 'wrong-group'])
def test_artifact_cli_rejects_untrusted_batch_without_writing(tmp_path, problem):
    root = tmp_path / 'repo'
    shutil.copytree(ROOT / 'deploy/gitops-template', root)
    target = root / 'environments/dev/lab-values.yaml'
    before = target.read_bytes()
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    import json
    for name, image in LAB_COMPONENTS.items():
        (artifacts / f'{name}.json').write_text(json.dumps({
            'source_sha': 'a' * 40, 'image': image, 'digest': 'sha256:' + 'b' * 64}))
    if problem == 'duplicate':
        shutil.copyfile(artifacts / 'order-service.json', artifacts / 'duplicate.json')
    else:
        path = artifacts / 'order-service.json'
        record = json.loads(path.read_text())
        record['source_sha' if problem == 'wrong-source' else 'image'] = 'c' * 40 if problem == 'wrong-source' else 'sre-agent'
        path.write_text(json.dumps(record))
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/update_gitops.py'), '--repo', str(root),
        '--component-group', 'lab', '--sha', 'a' * 40, '--registry', 'ghcr.io/example', '--digests', str(artifacts)], capture_output=True)
    assert result.returncode != 0
    assert target.read_bytes() == before
