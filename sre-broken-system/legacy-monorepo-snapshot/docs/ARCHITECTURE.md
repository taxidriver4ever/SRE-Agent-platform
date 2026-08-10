# Architecture

```mermaid
flowchart LR
  UI[Vue SRE Chat] -->|SSE/JSON| Agent[Single SRE Agent]
  Agent --> Gateway[LLM Gateway]
  Gateway --> Ollama[Ollama qwen3:4b]
  Agent --> K8s[Kubernetes Read Tools]
  Agent --> Obs[Prometheus / Loki / Tempo]
  Agent --> DB[MySQL Read-only]
  Agent --> Git[Local Git Read-only]
  K8s --> Runtime[Deployment → Pod → Container → Image → Git SHA]
  Runtime --> Services[order / inventory / user / payment]
  Services --> OTel[OpenTelemetry Collector]
  OTel --> Tempo
  Services --> Prometheus
  Services --> Alloy[Grafana Alloy]
  Alloy --> Loki
```

边界原则：Agent 不使用 Provider SDK，只依赖 GatewayClient；Tool 层硬限制读操作；运行源码必须先从 Deployment 注解取得 SHA；VERIFY 至少需要两个独立证据源。

数据路径：应用 Prometheus endpoint 被抓取；CRI 日志由 Alloy 送 Loki；Java 自动插桩和 Go 手工 OTLP 送 Collector/Tempo；MySQL 同时写 FILE 与 `mysql.slow_log` 表。
