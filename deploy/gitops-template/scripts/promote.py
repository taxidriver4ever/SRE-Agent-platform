"""Promote the exact scanned images from one environment to the next in Git."""
import argparse
from pathlib import Path
import re
import yaml

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--from-env',choices=['dev','staging'],required=True)
p.add_argument('--to-env',choices=['staging','prod'],required=True)
a=p.parse_args()
if (a.from_env,a.to_env) not in [('dev','staging'),('staging','prod')]:
    p.error('promote dev -> staging -> prod only')
root=Path(__file__).resolve().parents[1]
source=yaml.safe_load((root/f'environments/{a.from_env}/values.yaml').read_text())
path=root/f'environments/{a.to_env}/values.yaml'
target=yaml.safe_load(path.read_text())
if len({source[key]['image'].get('tag') for key in ['agent','gateway','frontend']}) != 1:
    raise ValueError('all components must belong to the same source release')
for key in ['agent','gateway','frontend']:
    image=source[key]['image']
    if not re.fullmatch(r'[0-9a-f]{40}',image.get('tag','')) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image.get('digest','')):
        raise ValueError('promotion requires a published SHA and recorded digest from CI')
    target[key]['image']=image.copy()
path.write_text(yaml.safe_dump(target,sort_keys=False),encoding='utf-8')
print(f'Updated {path}; review, commit and push (or open a deployment-repository PR).')
