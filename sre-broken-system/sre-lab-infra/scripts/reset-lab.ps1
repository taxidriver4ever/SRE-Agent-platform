$ErrorActionPreference = "Stop"
$infraRoot = Split-Path $PSScriptRoot -Parent
$serviceRoot = Join-Path $infraRoot "k8s\services"
$canary = Join-Path $infraRoot "k8s\scenarios\order-canary-bad.yaml"

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

# Reapplying canonical manifests reverses image, probe, replica and annotation mutations.
Get-ChildItem $serviceRoot -Recurse -Filter deployment.yaml | ForEach-Object { kubectl apply -f $_.FullName | Out-Null }
kubectl apply -f $canary | Out-Null
kubectl -n sre-lab scale deployment/order-service-canary --replicas=0 | Out-Null

function Set-PodFaultNormal {
    param([string]$Service, [int]$RemotePort, [string]$FaultPath)
    $podList = kubectl -n sre-lab get pods -l "app=$Service" -o json | ConvertFrom-Json
    $pods = $podList.items | Where-Object {
        -not $_.metadata.deletionTimestamp -and $_.status.phase -eq 'Running' -and
        ($_.status.containerStatuses | Where-Object ready).Count -gt 0
    } | ForEach-Object { $_.metadata.name }
    foreach ($pod in $pods) {
        $localPort = Get-FreeTcpPort
        $forward = Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$pod","${localPort}:$RemotePort" -WindowStyle Hidden -PassThru
        try {
            $reset = $false
            foreach ($attempt in 1..20) {
                if ($forward.HasExited) { break }
                try {
                    Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort$FaultPath" -TimeoutSec 3 | Out-Null
                    $reset = $true
                    break
                } catch {
                    Start-Sleep -Milliseconds 500
                }
            }
            if (-not $reset) { throw "reset endpoint not ready after 10 seconds" }
        } catch {
            Write-Warning "Could not reset $Service/${pod}: $($_.Exception.Message)"
        } finally {
            if (-not $forward.HasExited) { Stop-Process -Id $forward.Id }
        }
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
