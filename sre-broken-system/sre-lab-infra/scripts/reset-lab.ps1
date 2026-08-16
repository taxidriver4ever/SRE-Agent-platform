$ErrorActionPreference = "Stop"
$infraRoot = Split-Path $PSScriptRoot -Parent
$serviceRoot = Join-Path $infraRoot "k8s\services"
$canary = Join-Path $infraRoot "k8s\scenarios\order-canary-bad.yaml"

# Reapplying canonical manifests reverses image, probe, replica and annotation mutations.
Get-ChildItem $serviceRoot -Recurse -Filter deployment.yaml | ForEach-Object { kubectl apply -f $_.FullName | Out-Null }
kubectl apply -f $canary | Out-Null
kubectl -n sre-lab scale deployment/order-service-canary --replicas=0 | Out-Null

function Set-PodFaultNormal {
    param([string]$Service, [int]$RemotePort, [string]$FaultPath)
    $pods = (kubectl -n sre-lab get pods -l "app=$Service" -o jsonpath='{.items[*].metadata.name}').Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)
    $localPort = 18100
    foreach ($pod in $pods) {
        $forward = Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$pod","${localPort}:$RemotePort" -WindowStyle Hidden -PassThru
        try {
            Start-Sleep -Milliseconds 700
            Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort$FaultPath" -TimeoutSec 8 | Out-Null
        } catch {
            Write-Warning "Could not reset $Service/${pod}: $($_.Exception.Message)"
        } finally {
            if (-not $forward.HasExited) { Stop-Process -Id $forward.Id }
        }
        $localPort++
    }
}

kubectl -n sre-lab rollout status deployment/order-service --timeout=240s | Out-Null
Set-PodFaultNormal order-service 8080 '/debug/fault/normal'
Set-PodFaultNormal inventory-service 8081 '/debug/fault?mode=normal'
Set-PodFaultNormal user-service 8082 '/debug/fault?mode=normal'
Set-PodFaultNormal payment-service 8083 '/debug/fault?mode=normal'
Set-PodFaultNormal notification-service 8084 '/debug/fault?mode=normal'
Set-PodFaultNormal recommendation-service 8085 '/debug/fault?mode=normal'
Write-Host "Canonical GOOD versions, replicas, probes and all reachable Pod fault modes restored."
