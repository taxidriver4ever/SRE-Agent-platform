param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("SRE-001", "SRE-002", "SRE-003", "SRE-004", "SRE-005", "SRE-006", "SRE-007")]
    [string]$Scenario
)

$ErrorActionPreference = "Continue"
$order = "http://127.0.0.1:18080"
$inventory = "http://127.0.0.1:18081"
$user = "http://127.0.0.1:18082"
$payment = "http://127.0.0.1:18083"

function Invoke-ConcurrentGet([string]$Url, [int]$Count, [int]$Throttle = 12) {
    <# PowerShell 7 并行请求用于稳定制造池等待、CPU 压力或错误率。 #>
    1..$Count | ForEach-Object -Parallel {
        try {
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 $using:Url | Out-Null
        } catch {
            # 场景预期错误不终止负载；HTTP 状态和异常会由 Metrics/Logs 记录。
        }
    } -ThrottleLimit $Throttle
}

# 每次场景先恢复在线模式，避免前一个实验污染当前证据。
& (Join-Path $PSScriptRoot "reset-lab.ps1") | Out-Null

switch ($Scenario) {
    "SRE-001" {
        Invoke-RestMethod -Method Post "$order/debug/fault/slow_sql" | Out-Null
        1..8 | ForEach-Object { Invoke-WebRequest -UseBasicParsing "$order/orders/search?email=slow.example.com" | Out-Null }
    }
    "SRE-002" {
        Invoke-RestMethod -Method Post "$order/debug/fault/pool_exhaustion" | Out-Null
        Invoke-ConcurrentGet "$order/orders/search?email=slow.example.com" 24 24
    }
    "SRE-003" {
        Invoke-RestMethod -Method Post "$inventory/debug/fault?mode=dependency_timeout" | Out-Null
        Invoke-RestMethod -Method Post "$order/debug/fault/dependency_timeout" | Out-Null
        Invoke-ConcurrentGet "$order/orders/42" 8 8
    }
    "SRE-004" {
        Invoke-RestMethod -Method Post "$user/debug/fault?mode=cpu_saturation" | Out-Null
        Invoke-ConcurrentGet "$user/users/42" 12 6
    }
    "SRE-005" {
        Invoke-RestMethod -Method Post "$payment/debug/fault?mode=memory_leak" | Out-Null
        Write-Host "Memory leak enabled. Watch: kubectl get pods -n sre-lab -w"
    }
    "SRE-006" {
        Invoke-RestMethod -Method Post "$inventory/debug/fault?mode=dependency_timeout" | Out-Null
        Invoke-RestMethod -Method Post "$order/debug/fault/retry_storm" | Out-Null
        Invoke-ConcurrentGet "$order/orders/42" 6 6
    }
    "SRE-007" {
        Invoke-RestMethod -Method Post "$order/debug/fault/regression" | Out-Null
        1..8 | ForEach-Object { Invoke-WebRequest -UseBasicParsing "$order/orders/search?email=slow.example.com" | Out-Null }
    }
}

Write-Host "$Scenario triggered. Scenario definition: scenarios/catalog.yaml"
