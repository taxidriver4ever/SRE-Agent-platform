# Operator-side protection: legacy manifest writes fight Argo self-healing.
function Assert-LabNotGitOpsManaged {
    $raw = kubectl -n sre-lab get deployments -o json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot determine Lab ownership; no changes made.' }
    $deployments = $raw | ConvertFrom-Json
    foreach ($deployment in $deployments.items) {
        if ($deployment.metadata.annotations.'sre.agent/managed-by' -eq 'gitops') {
            throw 'Lab is managed by Argo CD. Change lab-values.yaml through Git; use set-runtime-fault.ps1 for HTTP faults or reset-lab.ps1 -RuntimeOnly. See docs/lab-gitops.md.'
        }
    }
}
