# order-service

Java 21 / Spring Boot 订单编排服务。分层包含 Controller、Application Service、Repository、Domain、HTTP Clients、Config、Exception 与测试。

业务 API：`POST /orders`、`GET /orders/{id}`、`GET /orders`、`GET /orders/search`、`POST /orders/{id}/cancel`。创建链路会通过 Kubernetes Service 调用 user、inventory、payment、notification。

故障重点：真实无索引 Slow SQL、HikariCP 连接占用、无退避重试、单 Pod 退化和混合版本回归。

## 依赖与配置

服务使用 MySQL 的 `orders`、`order_items` 数据，并调用 inventory、user、payment、notification。主要环境变量：`DB_URL`、`DB_USERNAME`、`DB_PASSWORD`、`DB_POOL_MAX`、`DB_CONNECTION_TIMEOUT_MS`、四个 `*_BASE_URL`、`SERVICE_VERSION` 和 `POD_NAME`。凭据从本地环境或 Kubernetes Secret 注入，不写入仓库。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orders` | 创建订单并编排用户、库存、支付和通知 |
| GET | `/orders/{id}` | 查询订单详情 |
| GET | `/orders?afterId=0&limit=20` | 游标分页 |
| GET | `/orders/search?email=...&limit=20` | 邮箱搜索热路径 |
| POST | `/orders/{id}/cancel` | 取消订单 |
| GET/POST | `/debug/fault/{mode}` | 查询或设置当前 Pod 故障模式 |
| GET | `/actuator/health` | 健康与探针 |
| GET | `/actuator/prometheus` | Prometheus 指标 |

故障白名单：`normal`、`slow_sql`、`pool_exhaustion`、`dependency_timeout`、`retry_storm`、`single_pod_slow`、`bad_health`。该接口仅供隔离实验环境使用。

## 本地运行与测试

要求 Java 21、Maven 和可访问的 MySQL/依赖服务。

```powershell
$env:DB_URL = "jdbc:mysql://127.0.0.1:13307/sre_lab"
# 从本地安全配置注入 DB_USERNAME 和 DB_PASSWORD
mvn test
mvn spring-boot:run
```

推荐通过 [Infra 脚本](../sre-lab-infra/README.md) 构建精确 GOOD/BAD commit 并在 Kubernetes 中运行，以获得完整的 Metrics、Logs、Traces、Pod 和 Git 证据。

返回 [实验工作区](../README.md)。
