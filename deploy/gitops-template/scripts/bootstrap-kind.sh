#!/usr/bin/env bash
# Operator bootstrap only. Never called from application CI.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-sre-lab}"
ARGOCD_VERSION="${ARGOCD_VERSION:-v3.5.3}"
[[ "$KUBE_CONTEXT" == kind-* ]] || { echo 'This helper is restricted to Kind contexts.' >&2; exit 1; }
[[ "$ARGOCD_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
if grep -R 'REPLACE_OWNER' "$ROOT/argocd" >/dev/null; then
  echo 'Export/configure the deployment repository before bootstrap.' >&2; exit 1
fi
kubectl --context "$KUBE_CONTEXT" cluster-info
for ns in argocd sre-dev sre-staging sre-prod sre-lab; do
  kubectl --context "$KUBE_CONTEXT" create namespace "$ns" --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
done
# Cluster controller installation is an operator action, not a release action.
kubectl --context "$KUBE_CONTEXT" -n argocd apply --server-side -f "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/install.yaml"
kubectl --context "$KUBE_CONTEXT" -n argocd rollout status deployment/argocd-server --timeout=300s
kubectl --context "$KUBE_CONTEXT" apply -f "$ROOT/argocd/project.yaml"
echo 'Argo CD installed. Provision external databases, namespace Secrets and repository read access before registering Applications.'
echo "Then run: kubectl --context $KUBE_CONTEXT apply -f $ROOT/argocd/dev-application.yaml"
echo 'Register staging/prod separately when their dependencies and approved release are ready.'
