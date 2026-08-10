# SRE Broken System

该目录是本地 Agent/SRE 自动故障诊断实验平台的工作区，不再是一个把所有服务绑定在一起的 Git 单仓库。每个业务服务和基础设施都拥有独立的提交历史、GOOD/BAD 版本与构建边界。

## 独立仓库

| 目录 | 技术栈 | 默认副本 | 责任 |
| --- | --- | ---: | --- |
| `order-service` | Java 21 / Spring Boot | 3 | 订单、搜索与跨服务编排 |
| `inventory-service` | Go | 2 | 库存读取、预占与释放 |
| `user-service` | Python / FastAPI | 2 | 用户资料与会员状态 |
| `payment-service` | Node.js 22 / TypeScript | 2 | 支付、退款与幂等处理 |
| `notification-service` | Go | 2 | 有界队列与异步通知 |
| `recommendation-service` | Python / FastAPI | 2 | 商品推荐与排序 |
| `sre-lab-infra` | Kubernetes / PowerShell / SQL | - | Kind、MySQL、观测栈与 SRE-001～010 |

进入任一目录后执行 `git status`、`git log good..bad` 或构建命令，操作的都是该组件自己的仓库。镜像标签必须使用对应仓库的完整 40 位 Git SHA，不能使用 `latest`。

## 启动与实验

```powershell
cd D:\SRE-Agent-platform\sre-broken-system\sre-lab-infra
.\scripts\deploy-lab.ps1
.\scripts\run-scenario.ps1 -Scenario SRE-009
.\scripts\reset-lab.ps1
```

`deploy-lab.ps1` 可以在保留已有 MySQL PVC 的情况下幂等重跑；它会补齐表结构和完全合成的数据。所有故障仅用于本地实验，不应部署到生产集群。

## 旧单仓归档

- `legacy-monorepo-history.bundle` 是旧根仓库的完整 Git bundle，已通过 `git bundle verify`。
- `.legacy-monorepo-git` 是原 `.git` 目录的可恢复副本。
- `legacy-monorepo-snapshot` 保存旧的平面 `docs/infra/scripts/mysql/scenarios` 与根配置文件。

根目录刻意不再包含 `.git`。如需审计旧历史，优先使用 bundle 创建临时克隆，不要把旧根仓库重新作为七个新仓库的父仓库启用。
