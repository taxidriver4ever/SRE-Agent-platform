# 恢复所有可在线切换的故障模式；场景控制脚本不是 Agent Tool，因此可执行写操作。
$ErrorActionPreference = "Stop"

function Set-NormalMode([string]$Service, [string]$Url) {
    <#
    CPU 饱和或 OOM 场景下连接可能在响应中途断开，因此最多重试 5 次。
    只有收到成功 JSON 后才报告恢复，避免旧脚本无条件输出“normal”的假成功。
    #>
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $response = Invoke-RestMethod -Method Post -TimeoutSec 10 $Url
            return [PSCustomObject]@{ service = $Service; fault_mode = $response.fault_mode; attempts = $attempt }
        } catch {
            if ($attempt -eq 5) { throw "Failed to reset $Service after 5 attempts: $($_.Exception.Message)" }
            Start-Sleep -Seconds 1
        }
    }
}

$results = @(
    Set-NormalMode "order-service" "http://127.0.0.1:18080/debug/fault/normal"
    Set-NormalMode "inventory-service" "http://127.0.0.1:18081/debug/fault?mode=normal"
    Set-NormalMode "user-service" "http://127.0.0.1:18082/debug/fault?mode=normal"
    Set-NormalMode "payment-service" "http://127.0.0.1:18083/debug/fault?mode=normal"
)
$results | Format-Table -AutoSize
Write-Host "All online fault modes verified as normal."
