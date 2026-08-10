# Scenarios

| ID | 服务 | 故障注入 | 预期证据链 |
|---|---|---|---|
| SRE-001 | order/MySQL | 前导 LIKE + 无索引 + SLEEP | P95 → JDBC Trace → slow_log → EXPLAIN ALL → Repository |
| SRE-002 | order/MySQL | 3 连接 Hikari + 4s SQL + 高并发 | 5xx → Hikari timeout → pool pending → slow query |
| SRE-003 | order/inventory | 下游延迟 4s、上游 1s timeout | order Trace → inventory client Span → timeout logs |
| SRE-004 | user | 低效质数计算 | container CPU → request latency → cpu_saturation log/source |
| SRE-005 | payment | 每秒保留约 6MiB Buffer | memory slope → OOMKilled/Event → restart → server.js |
| SRE-006 | order/inventory | timeout 后连续 5 次无退避重试 | Trace/流量放大 → retry logs → retry loop source |
| SRE-007 | order | GOOD 等值索引查询变为 BAD 前导 LIKE | runtime SHA → GOOD..BAD diff → slow SQL → P95 |

统一触发：`./scripts/run-scenario.ps1 -Scenario <ID>`。故障必须通过 `reset-lab.ps1` 关闭，不能让实验环境随机永久损坏。
