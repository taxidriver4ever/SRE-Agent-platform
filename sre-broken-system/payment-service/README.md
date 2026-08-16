# payment-service

Node.js 22 / TypeScript / Express 支付服务。模块包含 Routes、Application Service、Repository、Notification Client、Models、Config、Observability 和 Node Test。

业务 API：支付授权、支付状态、退款。实现幂等键、状态转换、通知下游，以及真实 Buffer 内存泄漏与 Event Loop Delay 指标。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payments` | 使用 `order_id`、`amount`、`idempotency_key` 授权 |
| GET | `/payments/{id}` | 查询支付状态 |
| POST | `/payments/{id}/refund` | 退款 |
| GET | `/health` | 健康、版本、Pod 和故障状态 |
| GET | `/metrics` | Prometheus 指标 |
| GET/POST | `/debug/fault?mode=...` | 查询或设置当前 Pod 故障 |

故障白名单：`normal`、`memory_leak`、`promise_backlog`、`event_loop_blocking`。`memory_leak` 每秒保留真实 Buffer，配合 Kubernetes 内存限制产生 RSS 增长、OOMKilled 和重启证据。

## 配置与运行

| 环境变量 | 默认值 |
| --- | --- |
| `PORT` | `8083` |
| `NOTIFICATION_BASE_URL` | `http://notification-service:8084` |
| `SERVICE_VERSION` / `POD_NAME` | `dev` / `local` |

```powershell
npm install
npm test
npm run build
$env:NOTIFICATION_BASE_URL = "http://127.0.0.1:8084"
npm start
```

本地启动前需准备 notification-service；完整 OOM 场景请通过 [Infra](../sre-lab-infra/README.md) 运行。

返回 [实验工作区](../README.md)。
