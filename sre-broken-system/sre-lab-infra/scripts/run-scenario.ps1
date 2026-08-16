param([Parameter(Mandatory=$true)][ValidatePattern('^SRE-00[1-9]$|^SRE-010$')][string]$Scenario)

$ErrorActionPreference = "Stop"
$infraRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path $infraRoot -Parent
$orderBad = (git -C (Join-Path $workspaceRoot "order-service") rev-parse bad).Trim()
$inventoryBad = (git -C (Join-Path $workspaceRoot "inventory-service") rev-parse bad).Trim()

& (Join-Path $PSScriptRoot "reset-lab.ps1") | Out-Null

function Set-AllPodFaults {
    param([string]$Service,[int]$RemotePort,[string]$FaultPath)
    $pods=(kubectl -n sre-lab get pods -l "app=$Service" -o jsonpath='{.items[*].metadata.name}').Split(' ',[System.StringSplitOptions]::RemoveEmptyEntries)
    $localPort=18200
    foreach($pod in $pods){
        $forward=Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$pod","${localPort}:$RemotePort" -WindowStyle Hidden -PassThru
        try{Start-Sleep -Milliseconds 700;Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort$FaultPath" -TimeoutSec 10|Out-Null}finally{if(-not $forward.HasExited){Stop-Process -Id $forward.Id}}
        $localPort++
    }
}

function Deploy-OrderBad {
    kubectl -n sre-lab set image deployment/order-service "order-service=sre-lab/order-service:$orderBad" | Out-Null
    kubectl -n sre-lab set env deployment/order-service "SERVICE_VERSION=$orderBad" "OTEL_RESOURCE_ATTRIBUTES=service.version=$orderBad,deployment.environment=sre-lab" | Out-Null
    kubectl -n sre-lab annotate deployment/order-service "sre.agent/git-sha=$orderBad" "sre.agent/previous-git-sha=dbf1473f182ecf4d157e57dd4486701fc2de53b6" --overwrite | Out-Null
    kubectl -n sre-lab rollout status deployment/order-service --timeout=240s | Out-Null
}

switch($Scenario){
    'SRE-001'{Deploy-OrderBad;Set-AllPodFaults order-service 8080 '/debug/fault/slow_sql';1..12|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=20' -TimeoutSec 30|Out-Null}}
    'SRE-002'{Deploy-OrderBad;Set-AllPodFaults order-service 8080 '/debug/fault/pool_exhaustion';$jobs=1..18|ForEach-Object{Start-Job{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=20' -TimeoutSec 30}};$jobs|Wait-Job|Receive-Job -ErrorAction SilentlyContinue|Out-Null;$jobs|Remove-Job}
    'SRE-003'{Set-AllPodFaults inventory-service 8081 '/debug/fault?mode=dependency_timeout';Write-Host 'Inventory reservations now exceed the order-service downstream timeout.'}
    'SRE-004'{Set-AllPodFaults user-service 8082 '/debug/fault?mode=cpu_saturation';Write-Host 'Both user-service replicas now execute genuine CPU-heavy work.'}
    'SRE-005'{Set-AllPodFaults payment-service 8083 '/debug/fault?mode=memory_leak';Write-Host 'Payment Pods retain 6 MiB/s until Kubernetes memory limits cause OOMKilled.'}
    'SRE-006'{kubectl -n sre-lab set image deployment/inventory-service "inventory-service=sre-lab/inventory-service:$inventoryBad"|Out-Null;kubectl -n sre-lab rollout status deployment/inventory-service --timeout=180s|Out-Null;Set-AllPodFaults recommendation-service 8085 '/debug/fault?mode=quadratic_ranking';Write-Host 'BAD inventory replicas retry the CPU-heavy recommendation dependency without backoff.'}
    'SRE-007'{Deploy-OrderBad;Write-Host "All order replicas now run BAD SHA $orderBad."}
    'SRE-008'{$pod=(kubectl -n sre-lab get pods -l 'app=order-service,track=stable' -o jsonpath='{.items[0].metadata.name}');$forward=Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$pod",'18299:8080' -WindowStyle Hidden -PassThru;try{Start-Sleep -Seconds 1;Invoke-RestMethod -Method Post 'http://127.0.0.1:18299/debug/fault/single_pod_slow'|Out-Null}finally{if(-not $forward.HasExited){Stop-Process -Id $forward.Id}};1..24|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=customer-10@slow.example.com&limit=5' -TimeoutSec 30|Out-Null};Write-Host "Only $pod is degraded; two sibling Pods remain normal."}
    'SRE-009'{kubectl apply -f (Join-Path $infraRoot 'k8s\scenarios\order-canary-bad.yaml')|Out-Null;kubectl -n sre-lab scale deployment/order-service --replicas=2|Out-Null;kubectl -n sre-lab scale deployment/order-service-canary --replicas=1|Out-Null;kubectl -n sre-lab rollout status deployment/order-service-canary --timeout=240s|Out-Null;1..24|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=5' -TimeoutSec 30|Out-Null};Write-Host 'Service now balances across two GOOD Pods and one BAD canary Pod.'}
    'SRE-010'{kubectl -n sre-lab patch deployment order-service --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/broken-health"}]'|Out-Null;Write-Host 'Invalid liveness path will cause restart/CrashLoop evidence; reset-lab.ps1 restores it.'}
}
Write-Host "$Scenario activated. Definition and expected evidence: scenarios/catalog.yaml"
