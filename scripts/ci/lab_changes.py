"""Select Lab images against their last desired release, or a PR merge base."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import yaml
from update_gitops import LAB_COMPONENTS

SHARED = ['.github/workflows/ci.yml', 'scripts/ci/', 'deploy/gitops-template/',
          'compose.yaml', 'sre-broken-system/sre-lab-infra/']


def select(root: Path, head: str, base: str, desired: Path | None = None) -> list[str]:
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        raise ValueError('head must be a full SHA')
    data = yaml.safe_load(desired.read_text(encoding='utf-8')) if desired and desired.exists() else None
    selected = []
    for name in LAB_COMPONENTS:
        prior = (data or {}).get('services', {}).get(name, {}).get('image', {}).get('tag', '') if desired else base
        if not re.fullmatch(r'[0-9a-f]{40}', prior) or subprocess.run(
                ['git', '-C', str(root), 'cat-file', '-e', f'{prior}^{{commit}}'],
                capture_output=True).returncode:
            print(f'{name}: no available released baseline; explicitly build', file=sys.stderr)
            selected.append(name)
            continue
        paths = [f'sre-broken-system/{name}/', *SHARED]
        result = subprocess.run(['git', '-C', str(root), 'diff', '--quiet', prior, head, '--', *paths])
        if result.returncode not in (0, 1):
            raise RuntimeError('git diff failed')
        if result.returncode:
            selected.append(name)
    return selected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--head', required=True)
    parser.add_argument('--base', default='')
    parser.add_argument('--desired', type=Path)
    args = parser.parse_args()
    print(json.dumps({'service': select(Path.cwd(), args.head, args.base, args.desired)}))
