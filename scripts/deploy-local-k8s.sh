#!/usr/bin/env bash
set -euo pipefail
echo 'Deprecated: application deployments are owned by the separate GitOps repository and Argo CD.' >&2
echo 'See docs/gitops-delivery.md. Change image SHA/digests in Git; rollback with git revert.' >&2
exit 1
