param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
# PowerShell 默认不会把 kubectl/docker 等原生命令的非零退出码当作异常。
# 开启该选项后，任一清单应用失败都会立即停止部署，避免输出“成功”的假阳性结果。
$PSNativeCommandUseErrorActionPreference = $true
$infraRoot = Split-Path $PSScriptRoot -Parent
$k8s = Join-Path $infraRoot "k8s"
$workspaceRoot = Split-Path $infraRoot -Parent
$kindCommand = Get-Command kind -ErrorAction SilentlyContinue
$kindExe = if ($env:KIND_EXE) { $env:KIND_EXE } elseif ($kindCommand) { $kindCommand.Source } else { Join-Path $workspaceRoot "tools\kind.exe" }
if (-not (Test-Path $kindExe)) { throw "kind executable not found; set KIND_EXE or place tools/kind.exe in workspace" }

if (-not (& $kindExe get clusters | Select-String -SimpleMatch "sre-lab")) {
    & $kindExe create cluster --name sre-lab --config (Join-Path $k8s "kind-config.yaml")
}
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot "build-images.ps1") -Service all -Version both }

kubectl apply -f (Join-Path $k8s "namespace\namespace.yaml")
# ConfigMaps are generated from source files so SQL and observability configuration have one canonical copy.
$mysqlSql = Join-Path $infraRoot "mysql\init\001-schema.sql"
$prometheusConfig = Join-Path $infraRoot "observability\prometheus.yml"
$lokiConfig = Join-Path $infraRoot "observability\loki.yaml"
$tempoConfig = Join-Path $infraRoot "observability\tempo.yaml"
$otelConfig = Join-Path $infraRoot "observability\otel-collector.yaml"
$alloyConfig = Join-Path $infraRoot "observability\alloy.alloy"
kubectl -n sre-lab create configmap mysql-init "--from-file=001-schema.sql=$mysqlSql" --dry-run=client -o yaml | kubectl apply -f -
# MySQL 清单只引用 Secret，不在 Git 中保存密码。全新 Kind 集群没有该对象时，
# 优先使用显式环境变量；本地未配置时生成随机值。已有 Secret 必须复用，否则
# 重部署持久卷时会让容器密码与数据目录中的既有 root 密码不一致。
$mysqlSecret = kubectl -n sre-lab get secret mysql-credentials --ignore-not-found -o name
if (-not $mysqlSecret) {
    $mysqlRootPassword = $env:SRE_LAB_MYSQL_ROOT_PASSWORD
    if (-not $mysqlRootPassword) {
        $passwordBytes = [byte[]]::new(32)
        [Security.Cryptography.RandomNumberGenerator]::Fill($passwordBytes)
        $mysqlRootPassword = [Convert]::ToBase64String($passwordBytes)
    }
    kubectl -n sre-lab create secret generic mysql-credentials "--from-literal=root-password=$mysqlRootPassword"
}
kubectl apply -f (Join-Path $k8s "database\mysql.yaml")
kubectl -n sre-lab rollout status deployment/mysql --timeout=240s

# ConfigMap 只会在“全新数据目录”的 MySQL 首次启动时由官方镜像自动执行。
# 对已经存在 PVC 的原地升级，下面把同一份幂等 SQL 主动送入当前 MySQL Pod：
# 这样既能补齐新服务所需的表和合成数据，又不需要删除用户已有的实验卷。
Get-Content -LiteralPath $mysqlSql -Raw |
    kubectl -n sre-lab exec -i deployment/mysql -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD"'
if ($LASTEXITCODE -ne 0) { throw "Failed to apply the idempotent MySQL schema and synthetic dataset" }

kubectl -n sre-lab create configmap prometheus-config "--from-file=prometheus.yml=$prometheusConfig" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sre-lab create configmap loki-config "--from-file=loki.yaml=$lokiConfig" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sre-lab create configmap tempo-config "--from-file=tempo.yaml=$tempoConfig" --dry-run=client -o yaml | kubectl apply -f -
# 名称必须与 stack.yaml 的 volume.configMap.name 完全一致；否则 Pod 会继续挂载旧占位配置。
kubectl -n sre-lab create configmap otel-collector-config "--from-file=otel-collector.yaml=$otelConfig" --dry-run=client -o yaml | kubectl apply -f -
# Alloy 启动参数读取 /etc/alloy/config.alloy，因此 ConfigMap 的 key 必须命名为 config.alloy。
kubectl -n sre-lab create configmap alloy-config "--from-file=config.alloy=$alloyConfig" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f (Join-Path $k8s "observability\stack.yaml")

# ConfigMap 挂载文件最终会更新，但 Tempo、OTel Collector 等进程不会自动重读全部配置。
# 显式滚动这些无状态观测工作负载，保证运行中的配置与仓库唯一配置源完全一致。
foreach ($deployment in @("prometheus", "loki", "tempo", "otel-collector")) {
    kubectl -n sre-lab rollout restart "deployment/$deployment"
    kubectl -n sre-lab rollout status "deployment/$deployment" --timeout=240s
}
kubectl -n sre-lab rollout restart daemonset/alloy
kubectl -n sre-lab rollout status daemonset/alloy --timeout=240s

Get-ChildItem (Join-Path $k8s "services") -Recurse -Filter deployment.yaml | ForEach-Object { kubectl apply -f $_.FullName }
kubectl apply -f (Join-Path $k8s "scenarios\order-canary-bad.yaml")
Get-ChildItem (Join-Path $k8s "services") -Directory | ForEach-Object { kubectl -n sre-lab rollout status "deployment/$($_.Name)" --timeout=240s }

kubectl get deployments,pods,services -n sre-lab -o wide
