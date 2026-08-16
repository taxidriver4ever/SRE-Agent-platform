# SRE Lab Infra

故障实验系统的基础设施与操作入口，统一管理 Kind Kubernetes、MySQL、Prometheus、Loki、Tempo、OpenTelemetry Collector、Alloy、六个业务服务、十个场景和 PowerShell 自动化。

## 为什么采用 Polyglot + Multi-Repo

Java、Go、Python、Node.js 具有不同的 runtime、连接池、GC、Event Loop 和故障模式；拆成独立 Git Repository 后，每个镜像、回滚和 Git Diff 都能准确对应单个服务，而不是从 Monorepo HEAD 猜测运行源码。

## 环境要求

- Windows PowerShell 7、Docker Desktop、Git、`kubectl`
- `kind` 位于 `PATH`，或通过 `KIND_EXE` 指定，或放在上级目录 `tools/kind.exe`
- 首次构建可访问 Java、Go、Python 和 npm 依赖源
- 当前 kubeconfig 指向本地实验环境；请勿对生产集群执行场景脚本

## 生命周期命令

```powershell
.\scripts\start-lab.ps1
.\scripts\deploy-lab.ps1 -SkipBuild
.\scripts\run-scenario.ps1 -Scenario SRE-001
.\scripts\reset-lab.ps1
.\scripts\stop-lab.ps1
# 显式删除集群和其中的数据
.\scripts\stop-lab.ps1 -DeleteCluster
```

构建脚本会从每个服务的 `good`/`bad` 标签创建临时 worktree，执行语言测试和 Docker build，以完整 Git SHA 标记镜像并加载到 Kind；不会切换开发目录的当前分支。部署脚本会对已有 MySQL PVC 重放幂等 schema。

## 本地入口

| 组件 | 地址 |
| --- | --- |
| order / inventory / user / payment | `127.0.0.1:18080` / `18081` / `18082` / `18083` |
| Prometheus / Loki / Tempo | `127.0.0.1:19090` / `13100` / `13200` |
| Lab MySQL | `127.0.0.1:13307` |

notification 和 recommendation 默认仅在集群内使用 `8084`、`8085`，可通过 `kubectl port-forward` 临时访问。

## 场景概览

| ID | 故障 |
| --- | --- |
| SRE-001 / 002 | 慢 SQL / Hikari 连接池耗尽 |
| SRE-003 / 004 | 依赖超时 / CPU 饱和 |
| SRE-005 / 006 | Node 内存泄漏 / 无退避重试风暴 |
| SRE-007～010 | 全量回归、单 Pod、混合版本、错误探针 |

权威机器定义位于 `scenarios/catalog.yaml`，详细证据预期见 [场景手册](docs/SCENARIOS.md)。

## Runtime → Source

`Deployment → ReplicaSet → Pod → image:<full-sha> → sre.agent/repository → 独立 Git Repo → Commit/Diff/Source`。`service-catalog.yaml` 是仓库标识、语言、依赖和源码入口的白名单来源。

## 目录与文档

- `k8s/`：Namespace、数据库、观测栈、业务 Deployment 和场景清单。
- `mysql/init/`：幂等 schema 与合成数据。
- `observability/`：各观测组件的唯一配置源。
- `scripts/`：构建、部署、注入、恢复和停止。
- `docs/`：[架构](docs/ARCHITECTURE.md)、[Runbook](docs/RUNBOOK.md)、[场景](docs/SCENARIOS.md)、[工作流](docs/WORKFLOWS.md)、[MCP](docs/MCP_TOOLS.md)、[评测](docs/EVALUATION.md)。

```powershell
kubectl get pods,deployments,services -n sre-lab -o wide
kubectl get events -n sre-lab --sort-by=.lastTimestamp
Invoke-RestMethod http://127.0.0.1:19090/-/ready
```

返回 [实验工作区](../README.md) 或 [平台总览](../../README.md)。
