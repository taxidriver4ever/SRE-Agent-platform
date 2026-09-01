param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^SRE-00[1-9]$|^SRE-010$')]
    [string]$Scenario
)

$ErrorActionPreference = "Stop"
$infraRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path $infraRoot -Parent
$orderBad = (git -C (Join-Path $workspaceRoot "order-service") rev-parse bad).Trim()
$inventoryBad = (git -C (Join-Path $workspaceRoot "inventory-service") rev-parse bad).Trim()

& (Join-Path $PSScriptRoot "reset-lab.ps1") | Out-Null

# 每个 Case 使用新的 Prometheus/Loki/Tempo 空间，避免上一个场景在最近 30 分钟
# 窗口内留下的指标、日志和 Trace 污染本次 Evidence。
foreach ($deployment in @('prometheus', 'loki', 'tempo')) {
    kubectl -n sre-lab rollout restart "deployment/$deployment" | Out-Null
    kubectl -n sre-lab rollout status "deployment/$deployment" --timeout=180s | Out-Null
}
# mysql.slow_log 使用 PVC，重启 Pod 不会隔离 Case。关闭慢日志后清空表再开启，
# 确保数据库证据也只来自当前场景。
kubectl -n sre-lab exec deployment/mysql -- sh -lc 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SET GLOBAL slow_query_log=OFF; TRUNCATE TABLE mysql.slow_log; SET GLOBAL slow_query_log=ON;"' | Out-Null

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Test-LocalTcpPort {
    param([int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(500) -and $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Invoke-ServiceLoad {
    param(
        [string]$Service,
        [int]$RemotePort,
        [string]$Path,
        [int]$Count = 8,
        [ValidateSet('GET','POST')][string]$Method = 'GET',
        [object]$Body = $null
    )
    $localPort = Get-FreeTcpPort
    $forward = Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"service/$Service","${localPort}:$RemotePort" -WindowStyle Hidden -PassThru
    try {
        foreach ($attempt in 1..20) {
            if (Test-LocalTcpPort $localPort) { break }
            if ($attempt -eq 20) { throw "service port-forward not ready for $Service" }
            Start-Sleep -Milliseconds 500
        }
        foreach ($index in 1..$Count) {
            try {
                if ($Method -eq 'POST') {
                    Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort$Path" -ContentType 'application/json' -Body ($Body | ConvertTo-Json -Depth 5) -TimeoutSec 15 | Out-Null
                } else {
                    Invoke-RestMethod "http://127.0.0.1:$localPort$Path" -TimeoutSec 30 | Out-Null
                }
            } catch {
                # 故障场景预期可能返回 5xx/timeout；请求本身已经形成所需观测数据。
            }
        }
    } finally {
        if (-not $forward.HasExited) { Stop-Process -Id $forward.Id }
    }
}

function Invoke-OrderCreationLoad {
    param([int]$Count = 8)
    $body = @{
        userId = 1
        customerEmail = 'eval-load@example.com'
        items = @(@{productId=1; sku='SKU-1'; quantity=1; unitPrice=10.00})
    }
    Invoke-ServiceLoad order-service 8080 '/orders' $Count 'POST' $body
}

function Set-AllPodFaults {
    param([string]$Service,[int]$RemotePort,[string]$FaultPath)
    $podList = kubectl -n sre-lab get pods -l "app=$Service" -o json | ConvertFrom-Json
    $pods = $podList.items | Where-Object {
        -not $_.metadata.deletionTimestamp -and $_.status.phase -eq 'Running' -and
        ($_.status.containerStatuses | Where-Object ready).Count -gt 0
    } | ForEach-Object { $_.metadata.name }
    if (-not $pods) { throw "no ready Pods found for $Service" }
    foreach($pod in $pods){
        $localPort=Get-FreeTcpPort
        $forward=Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$pod","${localPort}:$RemotePort" -WindowStyle Hidden -PassThru
        try {
            $activated = $false
            foreach ($attempt in 1..20) {
                if ($forward.HasExited) { throw "port-forward exited for $Service/$pod" }
                try {
                    Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort$FaultPath" -TimeoutSec 3 | Out-Null
                    $activated = $true
                    break
                } catch {
                    Start-Sleep -Milliseconds 500
                }
            }
            if (-not $activated) { throw "fault endpoint not ready for $Service/$pod after 10 seconds" }
        } finally {
            if (-not $forward.HasExited) { Stop-Process -Id $forward.Id }
        }
    }
}

function Deploy-OrderBad {
    kubectl -n sre-lab set image deployment/order-service "order-service=sre-lab/order-service:$orderBad" | Out-Null
    kubectl -n sre-lab set env deployment/order-service "SERVICE_VERSION=$orderBad" "OTEL_RESOURCE_ATTRIBUTES=service.version=$orderBad,deployment.environment=sre-lab" | Out-Null
    kubectl -n sre-lab annotate deployment/order-service "sre.agent/git-sha=$orderBad" "sre.agent/previous-git-sha=dbf1473f182ecf4d157e57dd4486701fc2de53b6" --overwrite | Out-Null
    kubectl -n sre-lab rollout status deployment/order-service --timeout=240s | Out-Null
}

function Deploy-InventoryBad {
    kubectl -n sre-lab set image deployment/inventory-service "inventory-service=sre-lab/inventory-service:$inventoryBad" | Out-Null
    kubectl -n sre-lab rollout status deployment/inventory-service --timeout=180s | Out-Null
}

switch($Scenario){
    'SRE-001'{Deploy-OrderBad;Set-AllPodFaults order-service 8080 '/debug/fault/slow_sql';1..12|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=20' -TimeoutSec 30|Out-Null}}
    'SRE-002'{Deploy-OrderBad;Set-AllPodFaults order-service 8080 '/debug/fault/pool_exhaustion';$jobs=1..18|ForEach-Object{Start-Job{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=20' -TimeoutSec 30}};$jobs|Wait-Job|Receive-Job -ErrorAction SilentlyContinue|Out-Null;$jobs|Remove-Job}
    'SRE-003'{Deploy-InventoryBad;Set-AllPodFaults inventory-service 8081 '/debug/fault?mode=dependency_timeout';Invoke-OrderCreationLoad 8;Invoke-ServiceLoad inventory-service 8081 '/inventory/SKU-1' 4;Start-Sleep -Seconds 5;Write-Host 'Inventory reservations now exceed the order-service downstream timeout.'}
    'SRE-004'{Set-AllPodFaults user-service 8082 '/debug/fault?mode=cpu_saturation';Invoke-ServiceLoad user-service 8082 '/users/1' 10;Write-Host 'Both user-service replicas now execute genuine CPU-heavy work.'}
    'SRE-005'{Set-AllPodFaults payment-service 8083 '/debug/fault?mode=memory_leak';Start-Sleep -Seconds 22;Write-Host 'Payment Pods retained memory long enough to produce first-cycle limit/restart evidence.'}
    'SRE-006'{Deploy-InventoryBad;Set-AllPodFaults recommendation-service 8085 '/debug/fault?mode=quadratic_ranking';Invoke-OrderCreationLoad 4;Invoke-ServiceLoad inventory-service 8081 '/inventory/SKU-1' 2;Start-Sleep -Seconds 3;Write-Host 'BAD inventory replicas retry the CPU-heavy recommendation dependency without backoff.'}
    'SRE-007'{Deploy-OrderBad;1..6|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=20' -TimeoutSec 30|Out-Null};Write-Host "All order replicas now run BAD SHA $orderBad."}
    'SRE-008'{
        $podList=kubectl -n sre-lab get pods -l 'app=order-service,track=stable' -o json|ConvertFrom-Json
        $pod=($podList.items|Where-Object{-not $_.metadata.deletionTimestamp -and $_.status.phase -eq 'Running'}|Select-Object -First 1).metadata.name
        $localPort=Get-FreeTcpPort
        $forward=Start-Process -FilePath kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$pod","${localPort}:8080" -WindowStyle Hidden -PassThru
        try {
            $activated=$false
            foreach($attempt in 1..20){
                if($forward.HasExited){throw "port-forward exited for order-service/$pod"}
                try{Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort/debug/fault/single_pod_slow" -TimeoutSec 3|Out-Null;$activated=$true;break}catch{Start-Sleep -Milliseconds 500}
            }
            if(-not $activated){throw "fault endpoint not ready for order-service/$pod after 10 seconds"}
        }finally{if(-not $forward.HasExited){Stop-Process -Id $forward.Id}}
        1..12|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=customer-10@slow.example.com&limit=5' -TimeoutSec 30|Out-Null}
        Write-Host "Only $pod is degraded; two sibling Pods remain normal."
    }
    'SRE-009'{kubectl apply -f (Join-Path $infraRoot 'k8s\scenarios\order-canary-bad.yaml')|Out-Null;kubectl -n sre-lab scale deployment/order-service --replicas=2|Out-Null;kubectl -n sre-lab scale deployment/order-service-canary --replicas=1|Out-Null;kubectl -n sre-lab rollout status deployment/order-service-canary --timeout=240s|Out-Null;1..12|ForEach-Object{Invoke-RestMethod 'http://127.0.0.1:18080/orders/search?email=slow.example.com&limit=5' -TimeoutSec 30|Out-Null};Write-Host 'Service now balances across two GOOD Pods and one BAD canary Pod.'}
    'SRE-010'{kubectl -n sre-lab patch deployment order-service --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/broken-health"}]'|Out-Null;Start-Sleep -Seconds 45;Write-Host 'Invalid liveness path produced restart/CrashLoop evidence; reset-lab.ps1 restores it.'}
}
Write-Host "$Scenario activated. Definition and expected evidence: scenarios/catalog.yaml"
