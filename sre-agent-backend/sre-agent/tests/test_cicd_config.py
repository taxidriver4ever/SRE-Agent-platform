"""CI/CD configuration contract tests.

These tests intentionally inspect declarative files.  They make security and
traceability constraints reviewable in pull requests instead of relying only on
someone remembering the deployment rules.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.config import get_settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPOSITORY_ROOT / ".github" / "workflows"
K8S_DIR = REPOSITORY_ROOT / "deploy" / "k8s"


def _load_yaml(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _load_yaml_documents(path: Path) -> list[dict]:
    return [
        item
        for item in yaml.load_all(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        if item
    ]


def test_ci_has_three_independent_github_hosted_jobs() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    triggers = workflow["on"]

    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]
    assert set(workflow["jobs"]) == {"agent-test", "gateway-test", "frontend-build"}
    assert all(job["runs-on"] == "ubuntu-latest" for job in workflow["jobs"].values())

    workflow_text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "python -m pytest" in workflow_text
    assert "npm run build" in workflow_text
    assert "self-hosted" not in workflow_text


def test_agent_ci_uses_linux_workspace_paths_for_repository_catalog() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    environment = workflow["jobs"]["agent-test"]["env"]

    assert environment["SRE_REPOSITORY_PATH"] == "${{ github.workspace }}/sre-broken-system"
    assert environment["SERVICE_CATALOG_PATH"] == (
        "${{ github.workspace }}/sre-broken-system/sre-lab-infra/service-catalog.yaml"
    )


def test_default_repository_paths_are_anchored_to_checkout(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SRE_REPOSITORY_PATH", raising=False)
    monkeypatch.delenv("SERVICE_CATALOG_PATH", raising=False)
    monkeypatch.delenv("SRE_REPOSITORY_CACHE_PATH", raising=False)

    settings = get_settings()

    assert Path(settings.repository_path) == REPOSITORY_ROOT / "sre-broken-system"
    assert Path(settings.service_catalog_path) == (
        REPOSITORY_ROOT / "sre-broken-system" / "sre-lab-infra" / "service-catalog.yaml"
    )
    assert Path(settings.repository_cache_path) == (
        REPOSITORY_ROOT / ".cache" / "sre-agent-repositories"
    )


def test_cd_is_gated_by_successful_trusted_main_ci() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "cd.yml")
    triggers = workflow["on"]

    assert set(triggers) == {"workflow_run"}
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["branches"] == ["main"]

    build_job = workflow["jobs"]["build-images"]
    deploy_job = workflow["jobs"]["deploy-local"]
    required_condition_fragments = (
        "conclusion == 'success'",
        "event == 'push'",
        "head_branch == 'main'",
        "head_repository.full_name == github.repository",
    )
    assert all(fragment in build_job["if"] for fragment in required_condition_fragments)
    assert all(fragment in deploy_job["if"] for fragment in required_condition_fragments)
    assert deploy_job["needs"] == ["build-images"]
    assert set(deploy_job["runs-on"]) == {"self-hosted", "linux", "x64", "sre-local-deploy"}


def test_cd_builds_all_images_with_full_sha_tag() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "cd.yml")
    build_job = workflow["jobs"]["build-images"]
    image_names = {item["image"] for item in build_job["strategy"]["matrix"]["include"]}
    assert image_names == {"sre-agent", "sre-gateway", "sre-agent-frontend"}

    workflow_text = (WORKFLOW_DIR / "cd.yml").read_text(encoding="utf-8")
    assert "docker/login-action@v4" in workflow_text
    assert "docker/setup-buildx-action@v4" in workflow_text
    assert "docker/build-push-action@v7" in workflow_text
    assert ":sha-${{ github.event.workflow_run.head_sha }}" in workflow_text
    assert "bash scripts/deploy-local-k8s.sh" in workflow_text


def test_kubernetes_deployments_have_rollout_and_probe_contracts() -> None:
    expected_replicas = {
        "sre-agent": "2",
        "sre-gateway": "2",
        "sre-agent-frontend": "1",
    }
    deployment_files = sorted(K8S_DIR.glob("*-deployment.yaml"))
    assert len(deployment_files) == 3

    for path in deployment_files:
        deployment = _load_yaml(path)
        name = deployment["metadata"]["name"]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        strategy = deployment["spec"]["strategy"]

        assert deployment["kind"] == "Deployment"
        assert deployment["metadata"]["namespace"] == "sre"
        assert deployment["spec"]["replicas"] == expected_replicas[name]
        assert strategy["type"] == "RollingUpdate"
        assert strategy["rollingUpdate"] == {"maxUnavailable": "0", "maxSurge": "1"}
        assert "readinessProbe" in container
        assert "livenessProbe" in container
        assert container["image"].endswith(":IMAGE_TAG_PLACEHOLDER")
        assert deployment["spec"]["template"]["metadata"]["annotations"][
            "sre.agent/git-commit"
        ] == "GIT_SHA_PLACEHOLDER"


def test_secret_example_contains_placeholders_only() -> None:
    documents = _load_yaml_documents(K8S_DIR / "secret.example.yaml")
    allowed_values = {"CHANGE_ME", "YOUR_GATEWAY_API_KEY", "YOUR_VLLM_API_KEY"}

    assert {document["metadata"]["name"] for document in documents} == {
        "sre-agent-secrets",
        "sre-gateway-secrets",
    }
    for document in documents:
        assert document["kind"] == "Secret"
        assert set(document["stringData"].values()) <= allowed_values


def test_dockerfiles_are_production_oriented_and_exclude_dotenv() -> None:
    dockerfiles = {
        "agent": REPOSITORY_ROOT / "sre-agent-backend" / "sre-agent" / "Dockerfile",
        "gateway": REPOSITORY_ROOT / "sre-agent-backend" / "sre-gateway" / "Dockerfile",
        "frontend": REPOSITORY_ROOT / "sre-agent-frontend" / "Dockerfile",
    }
    for name in ("agent", "gateway"):
        content = dockerfiles[name].read_text(encoding="utf-8")
        assert "python:3.12-slim" in content
        assert "HEALTHCHECK" in content
        assert "USER 10001:10001" in content
        assert "--reload" not in content
        assert "COPY .env" not in content

    frontend = dockerfiles["frontend"].read_text(encoding="utf-8")
    assert frontend.count("FROM ") == 2
    assert "npm ci" in frontend
    assert "nginx" in frontend.lower()
    assert "HEALTHCHECK" in frontend

    for dockerignore in (
        REPOSITORY_ROOT / ".dockerignore",
        REPOSITORY_ROOT / "sre-agent-backend" / "sre-gateway" / ".dockerignore",
        REPOSITORY_ROOT / "sre-agent-frontend" / ".dockerignore",
    ):
        ignore_text = dockerignore.read_text(encoding="utf-8")
        assert ".env" in ignore_text


def test_deploy_script_rolls_back_but_preserves_failure() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "deploy-local-k8s.sh").read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in script
    assert 'IMAGE_TAG}" != "sha-${GIT_SHA}' in script
    assert "kubectl" in script and "set image" in script
    assert "rollout status" in script
    assert "get --raw" in script
    assert "rollout undo" in script
    assert 'exit "${original_status}"' in script


def test_local_setup_reuses_the_existing_kind_cluster() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "setup-local-k8s.sh").read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in script
    assert 'CLUSTER_NAME="${CLUSTER_NAME:-sre-lab}"' in script
    assert "kind create cluster --config" in script
    assert 'LAB_NAMESPACE="sre-lab"' in script
    assert "k3d" not in script.lower()
    assert "secret.example.yaml unchanged" in script
