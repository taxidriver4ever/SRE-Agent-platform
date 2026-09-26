"""Update only one environment's immutable release, never contact Kubernetes."""
import argparse
import json
from pathlib import Path
import re
import yaml

COMPONENTS = {'agent': 'sre-agent', 'gateway': 'sre-gateway', 'frontend': 'sre-agent-frontend'}


def update(root: Path, environment: str, sha: str, registry: str, digests: dict | None = None) -> Path:
    if environment not in {'dev', 'staging', 'prod'}:
        raise ValueError('invalid environment')
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('a complete lowercase Git SHA is required')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9_.-]+', registry):
        raise ValueError('registry must be ghcr.io/<lowercase-owner>')
    path = root.resolve() / 'environments' / environment / 'values.yaml'
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('environment path escapes repository')
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    for key, image in COMPONENTS.items():
        digest = (digests or {}).get(image, '')
        if digests is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError(f'missing/invalid scanned digest for {image}')
        component = data.setdefault(key, {})
        component['image'] = {'repository': f'{registry}/{image}', 'tag': sha, 'digest': digest}
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
    a = p.parse_args()
    digests = None
    if a.digests:
        digests = {}
        for path in a.digests.glob('*.json'):
            document = json.loads(path.read_text())
            if document['source_sha'] != a.sha:
                raise ValueError('artifact source SHA mismatch')
            digests[document['image']] = document['digest']
    print(update(a.repo, a.environment, a.sha, a.registry, digests))
