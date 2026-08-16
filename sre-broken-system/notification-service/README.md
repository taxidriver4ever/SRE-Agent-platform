# notification-service

Go 1.23 异步通知服务，使用有界内部队列替代第一版 Kafka。模块包含 Handler、Worker Service、Queue Repository、Domain 和 Observability。

业务 API：提交通知、查询通知状态。故障重点为队列积压、外部依赖不稳定和 goroutine 泄漏。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/notifications` | 接收通知并返回 `202 Accepted` |
| GET | `/notifications/{id}` | 查询投递状态和尝试次数 |
| GET | `/health` | 健康、版本、Pod 和故障状态 |
| GET | `/metrics` | 队列、成功和失败指标 |
| GET/POST | `/debug/fault?mode=...` | 查询或设置当前 Pod 故障 |

故障白名单：`normal`、`queue_backlog`、`external_unstable`、`goroutine_leak`。队列为进程内有界实现，Pod 重启后数据不持久化，这是第一版实验模型的明确边界。

## 运行与测试

服务固定监听 `:8084`，使用 `SERVICE_VERSION` 和 `POD_NAME` 标识遥测维度。

```powershell
go test ./...
go run ./cmd/server
```

应用日志为结构化输出，指标暴露在 `/metrics`。Kubernetes 部署、资源限制和日志采集由 [Infra](../sre-lab-infra/README.md) 管理。

返回 [实验工作区](../README.md)。
