"""Create an independent deployment repository directory from bootstrap templates."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent / 'ci'))
from update_gitops import update


def export(destination: Path, repository: str, sha: str, registry: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository must be owner/name')
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or not re.fullmatch(r'ghcr\.io/[a-z0-9_.-]+', registry):
        raise ValueError('full SHA and lowercase GHCR registry are required')
    if destination.exists():
        raise ValueError('destination must not exist; existing desired state is never overwritten')
    source = Path(__file__).resolve().parents[1] / 'deploy/gitops-template'
    if destination.resolve().is_relative_to(source):
        raise ValueError('destination must be outside the bootstrap template')
    shutil.copytree(source, destination)
    url = f'https://github.com/{repository}.git'
    for path in (destination / 'argocd').glob('*.yaml'):
        path.write_text(path.read_text().replace('https://github.com/REPLACE_OWNER/sre-agent-deploy.git', url))
    for environment in ['dev', 'staging', 'prod']:
        update(destination, environment, sha, registry)
    subprocess.run(['git', 'init', '-b', 'main', str(destination)], check=True)
    subprocess.run(['git', '-C', str(destination), 'remote', 'add', 'origin', url], check=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination', required=True, type=Path)
    p.add_argument('--repository', required=True)
    p.add_argument('--sha', required=True)
    p.add_argument('--registry', required=True)
    a = p.parse_args()
    export(a.destination, a.repository, a.sha, a.registry)
