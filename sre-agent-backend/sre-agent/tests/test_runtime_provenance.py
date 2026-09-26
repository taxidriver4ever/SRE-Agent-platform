from unittest.mock import Mock
import pytest
from app.workflow.models import DiagnosisState
from app.workflow.runtime_extractor import extract_git_sha, extract_pod_runtime


@pytest.mark.parametrize('image,annotations', [
    ('ghcr.io/owner/service:' + 'a' * 40, {}),
    ('ghcr.io/owner/service:' + 'a' * 40 + '@sha256:' + 'b' * 64, {}),
    ('ghcr.io/owner/service@sha256:' + 'b' * 64, {'sre-agent/source-sha': 'a' * 40}),
])
def test_immutable_images_resolve_source_commit(image, annotations):
    state = DiagnosisState(task_id='test', query='order-service')
    extract_pod_runtime(state, {'data': {'items': [{'metadata': {'name': 'order-service-1', 'annotations': annotations},
                                                  'spec': {'containers': [{'image': image}]}}]}})
    assert state.runtime_commit == 'a' * 40


def test_selected_canary_supplies_source_metadata_not_last_stable_pod():
    def pod(name, sha, path):
        return {'metadata': {'name': name, 'annotations': {'sre.agent/git-sha': sha,
            'sre.agent/repository': 'order-service', 'sre.agent/repository-url': 'https://github.com/taxidriver4ever/SRE-Agent-platform.git',
            'sre.agent/source-path': path}}, 'spec': {'containers': [{'image': 'ghcr.io/owner/service@sha256:' + 'f' * 64}]}}
    state = DiagnosisState(task_id='test', query='order-service')
    registry = Mock()
    registry.bind.return_value = 'https://github.com/taxidriver4ever/SRE-Agent-platform.git'
    extract_pod_runtime(state, {'data': {'items': [pod('canary', 'a' * 40, 'selected.java'),
        pod('stable-1', 'b' * 40, 'old.java'), pod('stable-2', 'b' * 40, 'old.java')]}}, registry)
    assert state.mixed_versions
    assert state.runtime_commit == 'a' * 40
    assert state.pod_name == 'canary'
    assert state.source_code_location == 'selected.java'
    registry.bind.assert_called_once()


def test_non_sha_annotations_are_rejected():
    assert extract_git_sha({'data': {'annotations': {'sre.agent/git-sha': 'z' * 40}}}) is None


def test_regression_compares_previous_deployed_version():
    from datetime import datetime, timezone
    from app.workflow.models import ToolCallRecord
    from app.workflow.planning.decision_rules import evidence_driven_decision
    state = DiagnosisState(query='deployment regression', repository='order-service', runtime_commit='a' * 40)
    payload = {'data': {'items': [{'metadata': {'name': 'order-1', 'annotations': {
        'sre.agent/git-sha': 'a' * 40, 'sre.agent/previous-git-sha': 'b' * 40}},
        'spec': {'containers': [{'image': 'ghcr.io/owner/service@sha256:' + 'c' * 64}]}}]}}
    extract_pod_runtime(state, payload)
    state.timeline.append(ToolCallRecord(tool_name='get_commit',
        arguments={'repository': 'order-service', 'commit': 'a' * 40}, timestamp=datetime.now(timezone.utc), duration_ms=1))
    decision = evidence_driven_decision(state)
    assert decision.tool_name == 'get_commit_diff'
    assert decision.arguments['base'] == 'b' * 40


def test_remote_diff_explicitly_fetches_older_base(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.mcp_servers.git.tools import GitReadBackend
    (tmp_path / '.git').mkdir()
    registry = Mock()
    registry.resolve = AsyncMock(return_value=tmp_path)
    registry.remote_url.return_value = 'https://github.com/example/repo.git'
    command = AsyncMock(return_value='diff')
    monkeypatch.setattr('app.mcp_servers.git.tools.run_fixed_command', command)
    backend = GitReadBackend('get_commit_diff', str(tmp_path), 1, 1000, registry=registry)
    asyncio.run(backend.execute({'repository': 'order-service', 'head': 'a' * 40, 'base': 'b' * 40}))
    assert registry.resolve.await_args_list[0].args == ('order-service', 'a' * 40)
    assert registry.resolve.await_args_list[1].args == ('order-service', 'b' * 40)
    assert command.await_args.args[1][-4:] == ['--unified=40', 'b' * 40, 'a' * 40, '--']
