# Architecture

## Polyglot commerce graph

`order-service(Java) → inventory-service(Go) → recommendation-service(Python)`；订单同时调用 `user-service(Python)`、`payment-service(Node/TS)`、`notification-service(Go)`，payment 也调用 notification。所有调用使用 Kubernetes Service DNS，不绑定 Pod IP。

## Repository and release boundary

六个业务服务与 `sre-lab-infra` 都是独立 Git Repository。每个服务具有 GOOD/BAD commit/tag、独立 Dockerfile、测试和完整 SHA 镜像。Infra 只保存 K8s/观测/数据库/Scenario，不复制业务源码。

## Kubernetes topology

order 默认 3 副本；inventory、user、payment、notification、recommendation 默认 2 副本。Service selector 不含 version；SRE-009 的 stable/canary Deployment 因此会共享负载均衡后端。

## Observability

公共标签为 `service`、`version`、`pod`、`deployment.environment`；日志含 timestamp/service/version/pod/level/trace_id/message。Prometheus 抓取业务与 runtime 指标，Filebeat 经 Logstash 标准化和脱敏后写入 Elasticsearch，原生 Python Trace 进入 SkyWalking OAP；现有 OTLP Trace 经 Collector 进入 OAP 的兼容存储，Tool 统一使用原生 GraphQL 与 OAP Zipkin Query 读取。没有独立 Zipkin 服务。

## Diagnosis History 与统一采集

MySQL is the source of truth for diagnosis tasks and historical records. Elasticsearch acts as a search index for logs and historical diagnosis cases. History retrieval failures do not affect the core diagnosis workflow.

Logstash 以 Beats/HTTP 接收 Logs、History、Event，分别路由独立索引。Agent 标准 History JSON 经后台直接写 ES；MySQL Task/Checkpoint/Lease/Heartbeat 保持原实现。完整架构图、同步和恢复见 [History Search 架构报告](../../../docs/history-search-architecture.md)。
