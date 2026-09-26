# SRE Platform deployment repository

The repository now also contains an independent `charts/sre-lab` release,
`environments/dev/lab-values.yaml` and `argocd/sre-lab-dev.yaml`. Platform files
and its existing `sre-dev` Application remain unchanged. The Lab starts with
`releaseReady: false`; do not register its Application until CI has published
all six scanned images and initialized their full source SHA/digests.

Lab prerequisites in `sre-lab`: existing MySQL (`mysql:3306`, database `sre_lab`),
the existing observability stack, and Secret `sre-lab-database` with
`order-password` and `user-database-url`. The Lab chart never manages database
volumes or observability resources. Private GHCR requires `imagePullSecrets`.
Follow `docs/lab-gitops.md` in the application repository for migration, runtime
fault injection, canary experiments and Git revert. `scripts/promote.py` continues
to promote platform images only; Lab delivery is scoped to dev.

This directory is a **bootstrap template**, not active production desired state.
Export it using `scripts/export_gitops.py` in the application repository. From
then on, this independent repository owns charts, environment configuration,
approved image SHA/digests and Argo CD Applications. Do not sync Argo CD against
the application repository's template or export over an existing repository.

## Prerequisites

* A Kind cluster (the existing `kind-sre-lab` is supported), kubectl and Helm 3.
* External MySQL with **six separate databases**: `sre_agent_dev/staging/prod`
  and `sre_gateway_dev/staging/prod`. Provision least-privilege users per environment.
  Existing SQL initializers create tables, not databases. Retain existing data;
  any migration from the old single `sre_agent` database requires a backup and
  an explicit migration decision. Do not point all environments at the old DB.
* Existing lab observability/model endpoints, or environment-specific alternatives.
  Configure their addresses/users in environment values. Namespaces alone do not
  isolate external MySQL/ES/model data; production should use independent backing services.
* Namespace Secrets `sre-agent-secrets` and `sre-gateway-secrets`; chart never creates them.

Agent Secret keys: `APPLICATION_MYSQL_PASSWORD`, `MYSQL_PASSWORD`, `GATEWAY_API_KEY`,
`SRE_INITIAL_USERNAME`, `SRE_INITIAL_PASSWORD`; optional `ELASTICSEARCH_USERNAME`,
`ELASTICSEARCH_PASSWORD`. Gateway: `GATEWAY_MYSQL_PASSWORD`, provider keys such as
`VLLM_API_KEY`, `OPENAI_API_KEY`. Use the gateway's existing token-management process
to provision the Agent token. Do not invent an accepted token or commit credentials.

Create Secrets from private files outside Git (operator command, Bash/WSL):

```bash
kubectl --context kind-sre-lab -n sre-dev create secret generic sre-agent-secrets --from-env-file=/private/dev-agent.env
kubectl --context kind-sre-lab -n sre-dev create secret generic sre-gateway-secrets --from-env-file=/private/dev-gateway.env
```

Repeat with separate credentials in staging/prod. Rotate via your secret-management
process; a Secret change does not automatically restart Pods in this minimal setup.
External Secrets/Vault can later maintain these same names without changing workloads.
Private GHCR requires an `imagePullSecrets` entry and a read:packages pull Secret in
each namespace. Private Git requires Argo CD repository credentials with read access.

## Bootstrap and sync

Commit/push the exported repository first. Review `argocd/project.yaml` and environment
values, then run `bash scripts/bootstrap-kind.sh`. This installs pinned Argo CD only;
it never changes the application's image directly. After prerequisites are ready:

```bash
kubectl --context kind-sre-lab apply -f argocd/dev-application.yaml
kubectl --context kind-sre-lab -n argocd port-forward svc/argocd-server 8088:443
kubectl --context kind-sre-lab -n argocd get applications
```

Register staging/prod Applications separately. All three use auto-sync/prune/self-heal;
only dev is updated by application CI. Staging/prod change through reviewed commits/PRs.
Restrict branch write permissions accordingly. Three namespaces on one Kind node
simulate environments; two replicas/PDB do not provide node-level availability.

## Validate, promote and rollback

```bash
helm lint charts/sre-platform -f environments/dev/values.yaml --strict
helm template sre-platform charts/sre-platform -n sre-dev -f environments/dev/values.yaml
python -m pip install PyYAML==6.0.2
python scripts/promote.py --from-env dev --to-env staging
git diff -- environments/staging/values.yaml
git add environments/staging/values.yaml
git commit -m 'chore(deploy): promote validated release to staging'
git push origin main
```

Staging PostSync Job checks all health endpoints, frontend → login → authenticated
Diagnosis listing (real MySQL path), and the deployed Agent's Evidence Gate. Its
login uses the environment Secret, so keep its account credentials synchronized.
This is a lightweight acceptance test, not the ten injected fault scenarios; run
the application's existing `evals/run_evals.py` in an authorized lab for those.
After acceptance, promote staging → prod with the same script; no image rebuild.

Rollback the specific release commit in this repository:

```bash
git revert <deployment-commit>
git push origin main
```

Argo CD reapplies the previous desired state, including the recorded digest.
Check `Application.status.sync.revision`, `status.health`, Deployment rollout revision,
Pod image/imageID and `sre.agent/git-commit`. Automated sync does not automatically
revert a failing application. Readiness/maxUnavailable=0 keeps existing Ready replicas
where available, but cannot repair external state or reverse database schema changes.
Argo revision rollback requires disabling auto-sync first; reconcile the Git state
before reenabling it. Prefer Git revert to avoid self-heal undoing emergency changes.

HPA is optional and needs metrics-server. PDB limits voluntary disruptions only.
Startup checks wait up to five minutes; readiness gates traffic; liveness restarts
a stalled process. Existing `/health` endpoints are process/lifespan health, not
full dependency checks. History/ES failure must not make core diagnosis unready.

Chart defaults intentionally have empty tags and invalid owner placeholders. Helm
rejects incomplete image settings and mutable tags. Frontend nginx routing follows
the configured Agent Service port; its REST/SSE behavior is preserved.
