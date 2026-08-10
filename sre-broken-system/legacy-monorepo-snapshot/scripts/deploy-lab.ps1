param(
    [string]$ClusterName = "sre-lab"
)

$ErrorActionPreference = "Stop"
# PowerShell 默认不会把 docker/kubectl 的非零退出码转换为异常；开启该选项后，
# 任一镜像构建、资源应用或 rollout 失败都会立即终止脚本，避免输出“假成功”。
$PSNativeCommandUseErrorActionPreference = $true
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$KindPath = Join-Path $ProjectRoot "tools\kind.exe"
$K8sPath = Join-Path $ProjectRoot "infra\k8s"

function Ensure-Kind {
    <# 下载固定稳定版 kind 到项目 tools，不修改系统 PATH 或全局软件。 #>
    if (Test-Path -LiteralPath $KindPath) { return }
    New-Item -ItemType Directory -Force -Path (Split-Path $KindPath) | Out-Null
    Write-Host "Downloading kind v0.32.0..."
    curl.exe -fL "https://kind.sigs.k8s.io/dl/v0.32.0/kind-windows-amd64" -o $KindPath
}

function Get-GitRevision {
    <# 无 Git 历史时仍返回明确占位值；初始化 Scenario 提交后会使用真实 SHA。 #>
    try {
        # 使用完整 40 位 SHA，确保 K8s 注解、镜像标签与 Git MCP 的源码引用无歧义。
        return (& git -C $ProjectRoot rev-parse HEAD 2>$null).Trim()
    } catch {
        return "local-dev"
    }
}

Ensure-Kind

# kind NodePort 会占用与本地 Compose 相同的宿主机端口，部署前先停止本实验 Compose。
docker-compose -f (Join-Path $ProjectRoot "compose.yaml") down

$clusters = & $KindPath get clusters
if ($clusters -notcontains $ClusterName) {
    & $KindPath create cluster --name $ClusterName --config (Join-Path $K8sPath "kind-config.yaml")
}
kubectl config use-context "kind-$ClusterName" | Out-Null

$revision = Get-GitRevision
Write-Host "Building lab images with revision $revision"
$services = @("order-service", "inventory-service", "user-service", "payment-service")
foreach ($service in $services) {
    $image = "sre-lab/${service}:$revision"
    docker build --label "org.opencontainers.image.revision=$revision" --label "org.opencontainers.image.source=sre-broken-system" -t $image (Join-Path $ProjectRoot $service)
    & $KindPath load docker-image $image --name $ClusterName
}

kubectl apply -f (Join-Path $K8sPath "00-namespace.yaml")

# 先创建观测组件，再启动业务服务，使第一批请求就能进入 Metrics/Logs/Traces。
kubectl apply -f (Join-Path $K8sPath "30-observability.yaml")
$observabilityPath = Join-Path $ProjectRoot "infra\observability"
$configMaps = @{
    "prometheus-config" = @("prometheus.yml", (Join-Path $observabilityPath "prometheus.yml"))
    "loki-config" = @("loki.yaml", (Join-Path $observabilityPath "loki.yaml"))
    "tempo-config" = @("tempo.yaml", (Join-Path $observabilityPath "tempo.yaml"))
    "otel-collector-config" = @("otel-collector.yaml", (Join-Path $observabilityPath "otel-collector.yaml"))
    "alloy-config" = @("config.alloy", (Join-Path $observabilityPath "alloy.alloy"))
}
foreach ($entry in $configMaps.GetEnumerator()) {
    $fileArgument = "--from-file=$($entry.Value[0])=$($entry.Value[1])"
    kubectl -n sre-lab create configmap $entry.Key $fileArgument --dry-run=client -o yaml | kubectl apply -f -
}
kubectl -n sre-lab rollout restart deployment/prometheus deployment/loki deployment/tempo deployment/otel-collector
kubectl -n sre-lab rollout restart daemonset/alloy

# 实验密码只在本地集群 Secret 中创建；示例源码没有生产凭证。
kubectl -n sre-lab create secret generic mysql-credentials `
    --from-literal=root-password=root_dev_only `
    --from-literal=reader-password=sre_reader_dev_only `
    --dry-run=client -o yaml | kubectl apply -f -
$mysqlInitFile = Join-Path $ProjectRoot "mysql\init\001-schema.sql"
kubectl -n sre-lab create configmap mysql-init `
    "--from-file=001-schema.sql=$mysqlInitFile" `
    --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f (Join-Path $K8sPath "10-mysql.yaml")
kubectl apply -f (Join-Path $K8sPath "20-services.yaml")

# 把清单占位镜像替换成刚加载的不可变 SHA 标签，并记录 runtime→source 注解。
foreach ($service in $services) {
    kubectl -n sre-lab set image "deployment/$service" "$service=sre-lab/${service}:$revision"
    kubectl -n sre-lab annotate "deployment/$service" "sre.agent/git-sha=$revision" "sre.agent/repository=sre-broken-system" --overwrite
}

kubectl -n sre-lab rollout status deployment/mysql --timeout=240s
foreach ($service in $services) {
    kubectl -n sre-lab rollout status "deployment/$service" --timeout=240s
}
kubectl get pods -n sre-lab -o wide
