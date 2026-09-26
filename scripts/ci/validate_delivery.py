"""Static CI boundary checks plus real Helm renders for all three environments."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[2]


def check_workflows(root=ROOT):
    paths = list((root / '.github/workflows').glob('*.y*ml'))
    for path in paths:
        text = path.read_text(encoding='utf-8')
        assert not re.search(r'\bkubectl\b|\bhelm\s+(upgrade|install)\b|self-hosted|KUBECONFIG', text), path
        document = yaml.safe_load(text)
        assert 'pull_request_target' not in document.get('on', document.get(True, {})), path
        for job in document['jobs'].values():
            for step in job.get('steps', []):
                if 'run' in step:
                    for script in re.findall(r'(?:bash|python)\s+(scripts/[\w./-]+)', step['run']):
                        content = (root / script).read_text(encoding='utf-8')
                        assert not re.search(r'\bkubectl\b|helm\s+(upgrade|install)', content), script


def validate_render(documents, environment):
    deployments = [d for d in documents if d['kind'] == 'Deployment']
    assert len(deployments) == 3
    for d in deployments:
        assert d['metadata']['namespace'] == f'sre-{environment}'
        spec = d['spec']
        assert spec['strategy']['rollingUpdate'] == {'maxSurge': 1, 'maxUnavailable': 0}
        assert spec['replicas'] >= (2 if environment == 'prod' else 1)
        pod = spec['template']['spec']
        c = pod['containers'][0]
        assert re.search(r':[0-9a-f]{40}(?:@sha256:[0-9a-f]{64})?$', c['image'])
        assert all(c.get(p) for p in ['startupProbe','readinessProbe','livenessProbe','resources'])
        assert pod['securityContext']['fsGroup'] > 0
        assert c['securityContext']['readOnlyRootFilesystem']
    assert not any(d['kind'] == 'Secret' for d in documents)
    for d in documents:
        if d['kind'] == 'Role':
            assert d['metadata']['name'].startswith(f'sre-{environment}-')
            for rule in d['rules']:
                assert set(rule['verbs']) <= {'get','list','watch'}
                assert 'secrets' not in rule['resources']
    assert len([d for d in documents if d['kind']=='PodDisruptionBudget']) == (3 if environment=='prod' else 0)


def main():
    check_workflows()
    helm = os.environ.get('HELM_EXE', 'helm')
    template = ROOT / 'deploy/gitops-template'
    chart = template / 'charts/sre-platform'
    with tempfile.TemporaryDirectory() as tmp:
        override = Path(tmp)/'images.yaml'
        override.write_text(yaml.safe_dump({k:{'image':{'repository':f'ghcr.io/test/{k}','tag':'a'*40}} for k in ['agent','gateway','frontend']}))
        for env in ['dev','staging','prod']:
            args = [str(chart), '-f', str(template/f'environments/{env}/values.yaml'), '-f', str(override)]
            subprocess.run([helm,'lint',*args,'--strict'], check=True)
            rendered = subprocess.check_output([helm,'template','sre-platform',*args,'--namespace',f'sre-{env}'], text=True)
            docs = [d for d in yaml.safe_load_all(rendered) if d]
            validate_render(docs, env)
            print(f'{env}: {len(docs)} resources validated')
        invalid = subprocess.run([helm,'template','bad',str(chart)], capture_output=True)
        assert invalid.returncode != 0, 'unset/mutable release must fail closed'
        base_args = [helm, 'template', 'sre-platform', str(chart), '-f', str(override)]
        for change in ['agent.image.tag=latest', 'agent.config.MYSQL_PASSWORD=unsafe',
                       'agent.env[0].name=JWT_SECRET,agent.env[0].value=unsafe']:
            invalid = subprocess.run([*base_args, '--set', change], capture_output=True)
            assert invalid.returncode != 0, change
        extended = subprocess.check_output([*base_args, '--set',
            'ingress.enabled=true,agent.service.port=9001,frontend.service.port=8088,agent.autoscaling.enabled=true'], text=True)
        extra = [d for d in yaml.safe_load_all(extended) if d]
        assert any(d['kind']=='Ingress' for d in extra)
        assert any(d['kind']=='HorizontalPodAutoscaler' for d in extra)
        nginx = next(d for d in extra if d['metadata']['name']=='sre-frontend-nginx')
        assert 'sre-agent:9001' in nginx['data']['default.conf']
    print('CI boundary and all Helm environment contracts passed')
    from validate_lab import validate
    validate()


if __name__ == '__main__':
    main()
