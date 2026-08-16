# inventory-service

Go 1.23 库存服务，采用 `cmd/internal` 分层，包含 HTTP Handler、用例 Service、并发安全 Repository、Recommendation Client、Domain、Config 和 Observability。

业务 API：库存查询、库存预占、库存释放。库存查询会调用 recommendation-service，并透传 W3C `traceparent`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/inventory/{sku}` | 查询库存并预热推荐 |
| POST | `/inventory/reservations` | 按 `reservation_id`、`sku`、`quantity` 预占 |
| DELETE | `/inventory/reservations/{id}` | 释放预占 |
| GET | `/health` | 健康、版本和故障模式 |
| GET | `/metrics` | Prometheus 指标 |
| GET/POST | `/debug/fault?mode=...` | 查询或设置当前 Pod 故障 |

故障白名单：`normal`、`dependency_timeout`、`goroutine_leak`。BAD commit 还用于 SRE-006 的无退避 recommendation 重试风暴。

## 配置

| 环境变量 | 默认值 |
| --- | --- |
| `HTTP_ADDRESS` | `:8081` |
| `RECOMMENDATION_BASE_URL` | `http://recommendation-service:8085` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空 |
| `SERVICE_VERSION` / `POD_NAME` | `dev` / `local` |

## 本地运行与测试

```powershell
go test ./...
$env:RECOMMENDATION_BASE_URL = "http://127.0.0.1:8085"
go run ./cmd/server
```

生产式实验请使用 [Infra 脚本](../sre-lab-infra/README.md)，它会绑定运行镜像 SHA、Pod 标签和遥测版本。

返回 [实验工作区](../README.md)。
