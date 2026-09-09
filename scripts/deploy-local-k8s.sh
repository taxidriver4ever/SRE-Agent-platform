#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-sre-lab}"
NAMESPACE="sre"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io/taxidriver4ever}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-180s}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG must be an immutable tag such as sha-<40-hex-git-sha>}"
GIT_SHA="${GIT_SHA:?GIT_SHA must be the complete 40-character Git commit SHA}"
DEPLOYMENT_STARTED=false

if [[ ! "${GIT_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: GIT_SHA must contain exactly 40 lowercase hexadecimal characters." >&2
  exit 1
fi

if [[ "${IMAGE_TAG}" != "sha-${GIT_SHA}" ]]; then
  echo "ERROR: IMAGE_TAG must equal 'sha-${GIT_SHA}' so Pod images remain traceable to the exact commit." >&2
  exit 1
fi

for command_name in kubectl sed mktemp; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command '${command_name}' was not found." >&2
    exit 1
  fi
done

if ! kubectl config get-contexts "${KUBE_CONTEXT}" >/dev/null 2>&1; then
  echo "ERROR: kubectl context '${KUBE_CONTEXT}' does not exist." >&2
  exit 1
fi

if ! kubectl --context "${KUBE_CONTEXT}" get namespace sre-lab >/dev/null 2>&1; then
  echo "ERROR: namespace 'sre-lab' does not exist. Run scripts/setup-local-k8s.sh first." >&2
  exit 1
fi

for secret_name in sre-agent-secrets sre-gateway-secrets; do
  if ! kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" get secret "${secret_name}" >/dev/null 2>&1; then
    echo "ERROR: Secret '${secret_name}' does not exist in namespace '${NAMESPACE}'." >&2
    echo "Create it from local values as documented in README.md; never apply the example placeholders." >&2
    exit 1
  fi
done

render_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "${render_dir}"
}
trap cleanup EXIT

rollback_on_error() {
  local original_status=$?
  trap - ERR
  set +e
  if [[ "${DEPLOYMENT_STARTED}" == "true" ]]; then
    echo "Deployment validation failed. Rolling back application Deployments..." >&2
    for deployment_name in sre-agent sre-gateway sre-agent-frontend; do
      if kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" get deployment "${deployment_name}" >/dev/null 2>&1; then
        kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" rollout undo "deployment/${deployment_name}"
      fi
    done
    for deployment_name in sre-agent sre-gateway sre-agent-frontend; do
      if kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" get deployment "${deployment_name}" >/dev/null 2>&1; then
        kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" rollout status \
          "deployment/${deployment_name}" --timeout="${ROLLOUT_TIMEOUT}"
      fi
    done
    echo "Rollback attempt finished. The original deployment failure is preserved." >&2
  fi
  exit "${original_status}"
}
trap rollback_on_error ERR

render_deployment() {
  local source_file="$1"
  local target_file="$2"
  local short_sha="${GIT_SHA:0:12}"
  sed \
    -e "s/IMAGE_TAG_PLACEHOLDER/${IMAGE_TAG}/g" \
    -e "s/GIT_SHA_PLACEHOLDER/${GIT_SHA}/g" \
    -e "s/VERSION_PLACEHOLDER/${short_sha}/g" \
    "${source_file}" > "${target_file}"
}

kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/namespace.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/configmap.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/rbac.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/agent-service.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/gateway-service.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${REPOSITORY_ROOT}/deploy/k8s/frontend-service.yaml"

render_deployment "${REPOSITORY_ROOT}/deploy/k8s/agent-deployment.yaml" "${render_dir}/agent-deployment.yaml"
render_deployment "${REPOSITORY_ROOT}/deploy/k8s/gateway-deployment.yaml" "${render_dir}/gateway-deployment.yaml"
render_deployment "${REPOSITORY_ROOT}/deploy/k8s/frontend-deployment.yaml" "${render_dir}/frontend-deployment.yaml"

kubectl --context "${KUBE_CONTEXT}" apply -f "${render_dir}/agent-deployment.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${render_dir}/gateway-deployment.yaml"
kubectl --context "${KUBE_CONTEXT}" apply -f "${render_dir}/frontend-deployment.yaml"
DEPLOYMENT_STARTED=true

kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" set image deployment/sre-agent \
  sre-agent="${IMAGE_REGISTRY}/sre-agent:${IMAGE_TAG}"
kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" set image deployment/sre-gateway \
  sre-gateway="${IMAGE_REGISTRY}/sre-gateway:${IMAGE_TAG}"
kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" set image deployment/sre-agent-frontend \
  sre-agent-frontend="${IMAGE_REGISTRY}/sre-agent-frontend:${IMAGE_TAG}"

for deployment_name in sre-agent sre-gateway sre-agent-frontend; do
  kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" rollout status \
    "deployment/${deployment_name}" --timeout="${ROLLOUT_TIMEOUT}"
done

kubectl --context "${KUBE_CONTEXT}" get --raw \
  "/api/v1/namespaces/${NAMESPACE}/services/http:sre-agent:8001/proxy/health" >/dev/null
kubectl --context "${KUBE_CONTEXT}" get --raw \
  "/api/v1/namespaces/${NAMESPACE}/services/http:sre-gateway:8000/proxy/health" >/dev/null
kubectl --context "${KUBE_CONTEXT}" get --raw \
  "/api/v1/namespaces/${NAMESPACE}/services/http:sre-agent-frontend:80/proxy/healthz" >/dev/null

echo "Deployment succeeded. Running images:"
kubectl --context "${KUBE_CONTEXT}" -n "${NAMESPACE}" get pods \
  -l 'app.kubernetes.io/part-of=sre-agent-platform' \
  -o custom-columns='POD:.metadata.name,IMAGE:.spec.containers[*].image,COMMIT:.metadata.annotations.sre\.agent/git-commit'

trap - ERR
