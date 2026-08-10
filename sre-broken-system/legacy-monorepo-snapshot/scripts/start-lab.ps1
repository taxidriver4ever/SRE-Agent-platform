# start-lab 是面向使用者的稳定入口；实际构建与部署细节集中在 deploy-lab。
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "deploy-lab.ps1")
