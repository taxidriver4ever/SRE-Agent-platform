"""Executable Helm contracts using synthetic immutable identities, never published images."""
from pathlib import Path
import os
import subprocess
import tempfile
import yaml
from update_gitops import LAB_COMPONENTS


def validate():
    root = Path(__file__).resolve().parents[2]
    chart = root / 'deploy/gitops-template/charts/sre-lab'
    helm = os.environ.get('HELM_EXE', 'helm')
    values = {'releaseReady': True, 'services': {name: {'image': {
        'repository': f'ghcr.io/test/{image}', 'tag': 'a' * 40, 'digest': 'sha256:' + 'b' * 64}}
        for name, image in LAB_COMPONENTS.items()}}
    with tempfile.TemporaryDirectory() as tmp:
        override = Path(tmp) / 'images.yaml'
        override.write_text(yaml.safe_dump(values))
        args = [str(chart), '-f', str(override)]
        subprocess.run([helm, 'lint', *args, '--strict'], check=True)
        def render(extra=()):
            return [d for d in yaml.safe_load_all(subprocess.check_output(
                [helm, 'template', 'sre-lab', *args, '-n', 'sre-lab', *extra], text=True)) if d]
        docs = render()
        deployments = [d for d in docs if d['kind'] == 'Deployment']
        services = [d for d in docs if d['kind'] == 'Service']
        assert len(deployments) == len(services) == 6
        assert not any(d['kind'] == 'Secret' for d in docs)
        for d in deployments:
            name = d['metadata']['name']
            assert d['metadata']['namespace'] == 'sre-lab'
            annotations = d['metadata']['annotations']
            assert annotations['sre-agent/source-sha'] == 'a' * 40
            assert (root / annotations['sre.agent/source-path']).is_file()
            assert annotations['sre.agent/repository-url'].endswith('/SRE-Agent-platform.git')
            pod = d['spec']['template']
            assert pod['metadata']['annotations']['sre.agent/git-sha'] == 'a' * 40
            c = pod['spec']['containers'][0]
            assert c['image'].endswith(':' + 'a' * 40 + '@sha256:' + 'b' * 64)
            assert all(c.get(key) for key in ['startupProbe', 'livenessProbe', 'readinessProbe', 'resources'])
            assert not pod['spec']['automountServiceAccountToken']
            legacy = next(yaml.safe_load_all((root / f'sre-broken-system/sre-lab-infra/k8s/services/{name}/deployment.yaml').read_text(encoding='utf-8')))
            assert legacy['spec']['selector'] == d['spec']['selector'], 'migration must preserve immutable selectors'
            env = {v['name']: v for v in c['env']}
            assert env['SERVICE_VERSION']['value'] == 'a' * 40
            if name == 'order-service':
                assert 'secretKeyRef' in env['DB_PASSWORD']['valueFrom']
                assert env['INVENTORY_BASE_URL']['value'] == 'http://inventory-service:8081'
            if name == 'user-service':
                assert 'secretKeyRef' in env['DATABASE_URL']['valueFrom']
        for change in ['services.order-service.image.tag=latest', 'services.user-service.image.digest=bad',
                       'services.order-service.config.DB_PASSWORD=unsafe', 'releaseReady=false']:
            assert subprocess.run([helm, 'template', 'bad', *args, '--set', change], capture_output=True).returncode != 0
        canary = render(['--set', 'orderCanary.enabled=true,orderCanary.image.repository=ghcr.io/test/sre-lab-order-service,'
                         'orderCanary.image.tag=' + 'c' * 40 + ',orderCanary.image.digest=sha256:' + 'd' * 64])
        assert len([d for d in canary if d['kind'] == 'Deployment']) == 7
        assert len([d for d in canary if d['kind'] == 'Service']) == 6
        candidate = next(d for d in canary if d['kind'] == 'Deployment' and d['metadata']['name'] == 'order-service-canary')
        assert candidate['spec']['template']['metadata']['annotations']['sre.agent/previous-git-sha'] == 'a' * 40
    print('Lab Helm: six services, immutable provenance, probes, DNS, secrets and optional canary passed')


if __name__ == '__main__':
    validate()
