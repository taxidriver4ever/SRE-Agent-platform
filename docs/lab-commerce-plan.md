# SRE Lab 电商化增量计划

基线：94c25f4。平台 Agent/Gateway/Frontend 与其数据库保持原有职责。

## 当前架构

六个 Lab 服务已有 Dockerfile、健康端点、故障开关和独立 Lab CI/GitOps Chart。
order(Java/Spring Boot) 已调用 user(Python)、inventory(Go)、payment(TypeScript)、notification(Go)，
但库存确认/释放、支付落库、下单失败状态和推荐跨服务调用尚不完整。
recommendation 使用 Python。保持全部现有语言，不按示意图重写服务。
原有 MySQL/Prometheus/ELK/SkyWalking/OTel Collector 位于 sre-lab。
已有故障包含 SQL 扫描、连接池占用、依赖超时、CPU、内存、队列和重试；部分回归行为目前默认开启。
现有入口为 order NodePort/Compose 直连，尚无 Gateway API。

## 实施顺序

1. 完整下单链路：用户状态校验、库存预占/确认/释放、支付持久化与幂等、通知非核心失败。
2. 补足用户偏好与推荐 HTTP 依赖，明确订单/支付状态、统一错误结构，保留旧接口别名。
3. Envoy Gateway + Gateway API；Compose 使用同类 Envoy 数据面，公开路由只允许订单/用户/推荐。
4. 全服务 W3C trace/request ID、独立下游 span、JSON 请求日志和并发/错误/延迟指标。
5. 带 TTL、参数边界和自动恢复的内部故障 API，旧 debug API 兼容，故障不默认开启。
6. 更新现有 sre-lab Chart、ClusterIP、Gateway 策略、数据/Secret/观测配置，保留 namespace 约定。
7. 保留逐服务 build/test/scan/push；增加契约和故障场景验证，不让单服务改动重建其他五个镜像。
8. 可执行场景、README 拓扑、超时预算、观测信号、启动/升级/回滚说明。

每一阶段执行相应编译/测试，最后执行隔离 Compose 端到端验证；不改写正在使用的数据库或集群。
支付共享现有 MySQL 实例但使用独立 payment_db；订单不查询支付表。
库存使用单副本持久化内存模型，明确不假装具备跨副本一致性；避免引入新的分布式存储技术栈。
