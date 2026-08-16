param([switch]$DeleteCluster)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$KindPath = Join-Path $ProjectRoot "tools\kind.exe"

if ($DeleteCluster) {
    # 显式传入 -DeleteCluster 才删除 kind 集群及实验 PVC；默认只停止业务 Deployment。
    & $KindPath delete cluster --name sre-lab
} else {
    kubectl -n sre-lab scale deployment order-service inventory-service user-service payment-service --replicas=0
}
