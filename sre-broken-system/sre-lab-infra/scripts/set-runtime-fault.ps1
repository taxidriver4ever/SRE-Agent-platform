param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('order-service','inventory-service','user-service','payment-service','notification-service','recommendation-service')]
    [string]$Service,
    [Parameter(Mandatory=$true)] [ValidatePattern('^[a-z0-9_-]+$')] [string]$Mode
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$ports = @{'order-service'=8080; 'inventory-service'=8081; 'user-service'=8082; 'payment-service'=8083; 'notification-service'=8084; 'recommendation-service'=8085}
$raw = kubectl -n sre-lab get pods -l "app=$Service" -o json
if ($LASTEXITCODE -ne 0) { throw 'Pod discovery failed' }
$pods = ($raw | ConvertFrom-Json).items | Where-Object { -not $_.metadata.deletionTimestamp -and $_.status.phase -eq 'Running' }
if (-not $pods) { throw 'No running target Pods' }
foreach ($pod in $pods) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $localPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    $forward = Start-Process kubectl -ArgumentList '-n','sre-lab','port-forward',"pod/$($pod.metadata.name)","${localPort}:$($ports[$Service])" -WindowStyle Hidden -PassThru
    try {
        $path = if ($Service -eq 'order-service') { "/debug/fault/$Mode" } else { "/debug/fault?mode=$Mode" }
        $ready = $false
        foreach ($attempt in 1..20) {
            if ($forward.HasExited) { throw 'Port forward exited' }
            try { Invoke-WebRequest "http://127.0.0.1:$localPort/debug/fault" -TimeoutSec 2 | Out-Null; $ready = $true; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $ready) { throw 'Fault endpoint unavailable' }
        Invoke-RestMethod -Method Post "http://127.0.0.1:$localPort$path" -TimeoutSec 5
    } finally {
        if (-not $forward.HasExited) { Stop-Process -Id $forward.Id }
    }
}
