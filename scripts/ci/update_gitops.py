"""Update only one environment's immutable release, never contact Kubernetes."""
import argparse
import json
from pathlib import Path
import re
import yaml

COMPONENTS = {'agent': 'sre-agent', 'gateway': 'sre-gateway', 'frontend': 'sre-agent-frontend'}
PLATFORM_COMPONENTS = COMPONENTS
LAB_COMPONENTS = {name: f'sre-lab-{name}' for name in (
    'order-service', 'inventory-service', 'user-service', 'payment-service',
    'notification-service', 'recommendation-service')}


def update(root: Path, environment: str, sha: str, registry: str, digests: dict | None = None,
           component_group: str = 'platform') -> Path:
    if environment not in {'dev', 'staging', 'prod'}:
        raise ValueError('invalid environment')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('a complete lowercase Git SHA is required')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9_.-]+', registry):
        raise ValueError('registry must be ghcr.io/<lowercase-owner>')
    if component_group not in {'platform', 'lab'}:
        raise ValueError('invalid component group')
    components = PLATFORM_COMPONENTS if component_group == 'platform' else LAB_COMPONENTS
    if component_group == 'lab' and (environment != 'dev' or not digests):
        raise ValueError('Lab releases require scanned digests and currently support dev only')
    if digests is not None and set(digests) - set(components.values()):
        raise ValueError('unknown image or artifact from another component group')
    filename = 'values.yaml' if component_group == 'platform' else 'lab-values.yaml'
    path = root.resolve() / 'environments' / environment / filename
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('environment path escapes repository')
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    target = data if component_group == 'platform' else data['services']
    for key, image in components.items():
        if component_group == 'lab' and image not in digests:
            continue
        digest = (digests or {}).get(image, '')
        if digests is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError(f'missing/invalid scanned digest for {image}')
        component = target.setdefault(key, {})
        previous = component.get('image', {}).get('tag', '')
        if component_group == 'lab' and previous and previous != sha:
            if not re.fullmatch(r'[0-9a-f]{40}', previous):
                raise ValueError('invalid previous source SHA')
            component['previousSourceSha'] = previous
        component['image'] = {'repository': f'{registry}/{image}', 'tag': sha, 'digest': digest}
    if component_group == 'lab':
        # A partial update may not activate an incomplete first release.
        for key in components:
            image = target.get(key, {}).get('image', {})
            if not (re.fullmatch(r'ghcr\.io/[a-z0-9_.-]+/sre-lab-' + re.escape(key), image.get('repository', ''))
                    and re.fullmatch(r'[0-9a-f]{40}', image.get('tag', ''))
                    and re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('digest', ''))):
                raise ValueError(f'incomplete immutable Lab release: {key}; first release needs all six images')
        data['releaseReady'] = True
    # Write only after every component has passed validation.
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if path.read_text(encoding='utf-8') != text:
        path.write_text(text, encoding='utf-8')
    return path


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', required=True, type=Path)
    p.add_argument('--environment', choices=['dev', 'staging', 'prod'], default='dev')
    p.add_argument('--sha', required=True)
    p.add_argument('--registry', required=True)
    p.add_argument('--digests', type=Path)
    p.add_argument('--component-group', choices=['platform', 'lab'], default='platform')
    a = p.parse_args()
    digests = None
    if a.digests:
        digests = {}
        for path in a.digests.glob('*.json'):
            document = json.loads(path.read_text(encoding='utf-8'))
            if document['source_sha'] != a.sha:
                raise ValueError('artifact source SHA mismatch')
            if document['image'] in digests:
                raise ValueError('duplicate image artifact')
            digests[document['image']] = document['digest']
    print(update(a.repo, a.environment, a.sha, a.registry, digests, a.component_group))
