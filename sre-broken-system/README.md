# SRE Broken System Workspace

SRE Agent 的可控故障实验工作区。这里包含六个多语言业务服务和一个基础设施仓库，用真实计算、数据库查询、资源限制、依赖调用和 Kubernetes 发布状态制造可观测、可复现、可恢复的故障。

## 独立仓库

| 目录 | 技术栈 | 责任 | 文档 |
| --- | --- | --- | --- |
| `order-service` | Java 21 / Spring Boot | 订单、搜索与跨服务编排 | [README](order-service/README.md) |
| `inventory-service` | Go 1.23 / net/http | 库存读取、预占与释放 | [README](inventory-service/README.md) |
| `user-service` | Python 3.12 / FastAPI | 用户资料与会员状态 | [README](user-service/README.md) |
| `payment-service` | Node.js 22 / TypeScript | 支付、退款与幂等处理 | [README](payment-service/README.md) |
| `notification-service` | Go 1.23 / net/http | 有界队列与异步通知 | [README](notification-service/README.md) |
| `recommendation-service` | Python 3.12 / FastAPI | 商品推荐、缓存与排序 | [README](recommendation-service/README.md) |
| `sre-lab-infra` | Kubernetes / PowerShell / SQL | Kind、MySQL、观测栈与 SRE-001～010 | [README](sre-lab-infra/README.md) |

进入任一目录后执行 `git status`、`git log good..bad` 或构建命令，操作的都是该组件自己的仓库。`service-catalog.yaml` 记录 owner、依赖、关键源码和版本；镜像标签使用完整 Git SHA，Deployment 注解将运行 Pod 映射回精确源码。

## 启动与实验

```powershell
Set-Location sre-lab-infra
.\scripts\start-lab.ps1
.\scripts\run-scenario.ps1 -Scenario SRE-009
.\scripts\reset-lab.ps1
```

`deploy-lab.ps1` 可以在保留已有 MySQL PVC 的情况下幂等重跑；它会补齐表结构和完全合成的数据。故障必须产生可在 Metrics、Logs、Traces、Kubernetes、MySQL 或 Git 中验证的真实证据。所有故障仅用于本地实验，不应部署到生产集群。

## 旧单仓归档

- `legacy-monorepo-history.bundle` 是旧根仓库的完整 Git bundle，已通过 `git bundle verify`。
- `.legacy-monorepo-git` 是原 `.git` 目录的可恢复副本。
- `legacy-monorepo-snapshot` 保存旧的平面 `docs/infra/scripts/mysql/scenarios` 与根配置文件。

根目录刻意不再包含 `.git`。如需审计旧历史，优先使用 bundle 创建临时克隆，不要把旧根仓库重新作为七个新仓库的父仓库启用。归档不参与当前构建和诊断，详见 [归档说明](legacy-monorepo-snapshot/README.md)。

返回 [平台总览](../README.md)。
