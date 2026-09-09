#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-sre-lab}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
NAMESPACE="sre"
LAB_NAMESPACE="sre-lab"
KIND_CONFIG="${REPOSITORY_ROOT}/sre-broken-system/sre-lab-infra/k8s/kind-config.yaml"

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command '${command_name}' was not found." >&2
    return 1
  fi
}

require_command docker
require_command kind
require_command kubectl

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running or the current WSL2 user cannot access it." >&2
  exit 1
fi

if ! kind get clusters | grep -Fxq "${CLUSTER_NAME}"; then
  if [[ ! -f "${KIND_CONFIG}" ]]; then
    echo "ERROR: existing kind configuration was not found: ${KIND_CONFIG}" >&2
    exit 1
  fi
  echo "Creating kind cluster '${CLUSTER_NAME}' from the existing sre-lab configuration..."
  kind create cluster --config "${KIND_CONFIG}"
else
  echo "Reusing existing kind cluster '${CLUSTER_NAME}'."
fi

if ! kubectl config get-contexts "${KUBE_CONTEXT}" >/dev/null 2>&1; then
  echo "ERROR: kubectl context '${KUBE_CONTEXT}' does not exist." >&2
  exit 1
fi

kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/namespace.yaml"
if ! kubectl --context "${KUBE_CONTEXT}" get namespace "${LAB_NAMESPACE}" >/dev/null 2>&1; then
  echo "Creating empty namespace '${LAB_NAMESPACE}' for the existing lab RBAC boundary."
  kubectl --context "${KUBE_CONTEXT}" create namespace "${LAB_NAMESPACE}"
fi
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/configmap.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/rbac.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/agent-service.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/gateway-service.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/frontend-service.yaml"

echo "Local Kubernetes foundation is ready in context '${KUBE_CONTEXT}', namespace '${NAMESPACE}'."

missing_secrets=()
for secret_name in sre-agent-secrets sre-gateway-secrets; do
  if ! kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" get secret "${secret_name}" >/dev/null 2>&1; then
    missing_secrets+=("${secret_name}")
  fi
done

if (( ${#missing_secrets[@]} > 0 )); then
  echo "Application Secrets are not created: ${missing_secrets[*]}"
  echo "Do not apply deploy/k8s/secret.example.yaml unchanged. Create local Secrets as documented in README.md."
fi

if [[ -n "${IMAGE_TAG:-}" || -n "${GIT_SHA:-}" ]]; then
  if [[ -z "${IMAGE_TAG:-}" || -z "${GIT_SHA:-}" ]]; then
    echo "ERROR: IMAGE_TAG and GIT_SHA must be supplied together." >&2
    exit 1
  fi
  KUBE_CONTEXT="${KUBE_CONTEXT}" \
    IMAGE_TAG="${IMAGE_TAG}" GIT_SHA="${GIT_SHA}" \
    bash "${SCRIPT_DIR}/deploy-local-k8s.sh"
else
  echo "No application image was deployed. Set IMAGE_TAG=sha-<40-hex-sha> and GIT_SHA=<40-hex-sha>, then run scripts/deploy-local-k8s.sh."
fi
