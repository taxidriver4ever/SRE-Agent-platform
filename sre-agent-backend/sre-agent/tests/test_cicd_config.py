"""Contracts for the current CI / Helm / Argo CD delivery architecture."""
from pathlib import Path
import yaml
from app.core.config import get_settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPOSITORY_ROOT / ".github/workflows"
GITOPS_DIR = REPOSITORY_ROOT / "deploy/gitops-template"
QUALITY_JOBS = {"agent-test", "gateway-test", "frontend-build", "agent-regression",
                "delivery-contracts", "dependency-scan"}


def _load_yaml(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_ci_retains_independent_quality_gates_on_pinned_hosted_runners():
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    assert "pull_request" in workflow["on"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request_target" not in workflow["on"]
    assert QUALITY_JOBS <= set(workflow["jobs"])
    assert all(job["runs-on"] == "ubuntu-24.04" for job in workflow["jobs"].values())
    assert all(not workflow["jobs"][name].get("needs") for name in QUALITY_JOBS)
    assert workflow["permissions"] == {"contents": "read"}


def test_publication_requires_all_quality_gates_and_trusted_main_push():
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    assert not (WORKFLOW_DIR / "cd.yml").exists()
    build = workflow["jobs"]["build-images"]
    deploy = workflow["jobs"]["update-gitops"]
    assert set(build["needs"]) == QUALITY_JOBS
    assert deploy["needs"] == ["build-images"]
    for job in (build, deploy):
        assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert build["permissions"] == {"contents": "read", "packages": "write"}
    assert deploy["permissions"] == {"contents": "read"}
    assert deploy["concurrency"]["cancel-in-progress"] == "false"
    commands = "\n".join(step.get("run", "") for step in deploy["steps"])
    assert "scripts/ci/update_gitops.py" in commands and '--environment dev' in commands
    assert '"$newest" != "$GITHUB_SHA"' in commands
    assert "--force" not in commands
    text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "kubectl" not in text and "self-hosted" not in text


def test_all_sha_images_are_scanned_before_push_and_digests_are_recorded():
    job = _load_yaml(WORKFLOW_DIR / "ci.yml")["jobs"]["build-images"]
    assert {x["image"] for x in job["strategy"]["matrix"]["include"]} == {
        "sre-agent", "sre-gateway", "sre-agent-frontend"}
    steps = job["steps"]
    build = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
    scan = next(s for s in steps if s.get("uses", "").startswith("aquasecurity/trivy-action@"))
    push = next(s for s in steps if s.get("run", "").startswith('docker push'))
    assert build["with"]["push"] == "false" and build["with"]["load"] == "true"
    assert scan["with"]["severity"] == "CRITICAL"
    assert scan["with"]["exit-code"] == "1" and scan["with"]["ignore-unfixed"] == "false"
    assert steps.index(build) < steps.index(scan) < steps.index(push)
    commands = "\n".join(s.get("run", "") for s in steps)
    for fragment in (':$GITHUB_SHA', 'docker manifest inspect', 'org.opencontainers.image.revision', '.RepoDigests'):
        assert fragment in commands
    assert not any(s.get("continue-on-error") == "true" for s in steps)


def test_helm_environments_preserve_probes_rollout_and_prod_availability():
    # Full rendering is exercised separately by the delivery-contracts job.
    values = _load_yaml(GITOPS_DIR / "charts/sre-platform/values.yaml")
    for component in ("agent", "gateway", "frontend"):
        config = values[component]
        assert config["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {
            "maxUnavailable": "0", "maxSurge": "1"}}
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            assert config[probe]["httpGet"]["path"] in {"/health", "/healthz"}
        assert config["resources"]["requests"] and config["resources"]["limits"]
    for environment in ("dev", "staging", "prod"):
        overlay = _load_yaml(GITOPS_DIR / f"environments/{environment}/values.yaml")
        app = _load_yaml(GITOPS_DIR / f"argocd/{environment}-application.yaml")["spec"]
        assert app["destination"]["namespace"] == f"sre-{environment}"
        assert app["source"]["path"] == "charts/sre-platform"
        assert app["syncPolicy"]["automated"] == {"prune": "true", "selfHeal": "true", "allowEmpty": "false"}
        if environment == "prod":
            for component in ("agent", "gateway", "frontend"):
                assert int(overlay[component]["replicaCount"]) >= 2
                assert overlay[component]["pdb"]["enabled"] == "true"


def test_helm_references_external_secrets_instead_of_committing_credentials():
    values = _load_yaml(GITOPS_DIR / "charts/sre-platform/values.yaml")
    assert values["agent"]["existingSecret"] == "sre-agent-secrets"
    assert values["gateway"]["existingSecret"] == "sre-gateway-secrets"
    for component in ("agent", "gateway"):
        assert not any("PASSWORD" in key or "API_KEY" in key for key in values[component]["config"])
    templates = GITOPS_DIR / "charts/sre-platform/templates"
    assert not any("kind: Secret" in p.read_text(encoding="utf-8") for p in templates.glob("*.yaml"))
    assert "secretRef:" in (templates / "workloads.yaml").read_text(encoding="utf-8")


def test_legacy_deploy_entrypoint_fails_closed_and_directs_to_git_revert():
    script = (REPOSITORY_ROOT / "scripts/deploy-local-k8s.sh").read_text(encoding="utf-8")
    assert "exit 1" in script and "git revert" in script
    assert "kubectl" not in script and "rollout undo" not in script


def test_local_setup_prepares_kind_without_releasing_application_resources():
    script = (REPOSITORY_ROOT / "scripts/setup-local-k8s.sh").read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in script
    assert 'CLUSTER_NAME="${CLUSTER_NAME:-sre-lab}"' in script
    assert 'kind get clusters' in script
    assert 'kind create cluster --name "$CLUSTER_NAME" --config' in script
    assert "sre-lab-infra/k8s/kind-config.yaml" in script
    assert "apply -f" not in script and "set image" not in script
    assert "k3d" not in script.lower()


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
