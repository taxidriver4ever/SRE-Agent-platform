#!/usr/bin/env bash
# Local infrastructure bootstrap, never invoked from application CI.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-sre-lab}"
for tool in docker kind kubectl; do command -v "$tool" >/dev/null; done
docker info >/dev/null
if ! kind get clusters | grep -Fxq "$CLUSTER_NAME"; then
  kind create cluster --name "$CLUSTER_NAME" --config "$ROOT/sre-broken-system/sre-lab-infra/k8s/kind-config.yaml"
fi
kubectl --context "kind-$CLUSTER_NAME" cluster-info
echo 'Kind ready. Export/configure the separate GitOps repository, then run its scripts/bootstrap-kind.sh.'
echo 'Application resources are no longer applied by this script. See docs/gitops-delivery.md.'
