from pathlib import Path
import sys
import shutil
import subprocess
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'scripts/ci'))
from update_gitops import update
from validate_delivery import check_workflows


def test_ci_cannot_deploy_cluster():
    check_workflows()


def test_update_is_scoped_idempotent_and_preserves_config(tmp_path):
    shutil.copytree(ROOT/'deploy/gitops-template',tmp_path/'repo')
    root=tmp_path/'repo'
    prod=(root/'environments/prod/values.yaml').read_bytes()
    target=update(root,'dev','a'*40,'ghcr.io/example', {n:'sha256:'+'b'*64 for n in ['sre-agent','sre-gateway','sre-agent-frontend']})
    first=target.read_bytes()
    update(root,'dev','a'*40,'ghcr.io/example', {n:'sha256:'+'b'*64 for n in ['sre-agent','sre-gateway','sre-agent-frontend']})
    assert first==target.read_bytes()
    assert prod==(root/'environments/prod/values.yaml').read_bytes()
    values=yaml.safe_load(first)
    assert values['agent']['config']['APPLICATION_MYSQL_DATABASE']=='sre_agent_dev'
    assert all(values[k]['image']['tag']=='a'*40 for k in ['agent','gateway','frontend'])


@pytest.mark.parametrize('env,sha,registry,digests',[
    ('../prod','a'*40,'ghcr.io/example',None),
    ('dev','latest','ghcr.io/example',None),
    ('dev','a'*40,'ghcr.io/example; touch x',None),
    ('dev','a'*40,'ghcrXio/example',None),
    ('dev','a'*40,'ghcr.io/example',{}),
])
def test_invalid_update_never_writes(tmp_path,env,sha,registry,digests):
    shutil.copytree(ROOT/'deploy/gitops-template',tmp_path/'repo')
    target=tmp_path/'repo/environments/dev/values.yaml'
    before=target.read_bytes()
    with pytest.raises(ValueError): update(tmp_path/'repo',env,sha,registry,digests)
    assert target.read_bytes()==before


def test_promotion_preserves_environment_and_requires_digest(tmp_path):
    root=tmp_path/'repo'
    shutil.copytree(ROOT/'deploy/gitops-template',root)
    target=root/'environments/staging/values.yaml'
    before=target.read_bytes()
    command=[sys.executable,str(root/'scripts/promote.py'),'--from-env','dev','--to-env','staging']
    assert subprocess.run(command,capture_output=True).returncode != 0
    assert target.read_bytes()==before
    update(root,'dev','a'*40,'ghcr.io/example',{n:'sha256:'+'b'*64 for n in ['sre-agent','sre-gateway','sre-agent-frontend']})
    assert subprocess.run(command,capture_output=True).returncode == 0
    values=yaml.safe_load(target.read_text())
    assert values['agent']['config']['APPLICATION_MYSQL_DATABASE']=='sre_agent_staging'
    assert values['agent']['image']['tag']=='a'*40
    assert values['smoke']['enabled'] is True


def test_export_never_overwrites_existing_repository(tmp_path):
    existing=tmp_path/'existing';existing.mkdir()
    marker=existing/'keep';marker.write_text('keep')
    run=subprocess.run([sys.executable,str(ROOT/'scripts/export_gitops.py'),'--destination',str(existing),
        '--repository','example/deploy','--sha','a'*40,'--registry','ghcr.io/example'],capture_output=True)
    assert run.returncode != 0
    assert marker.read_text()=='keep'
    assert list(existing.iterdir())==[marker]
