---
name: distributed-tracing
description: Trace one request across Java, Go, Python, Node.js, HTTP, and MySQL using W3C trace context and Tempo. Use for intermittent latency, dependency timeout, retry amplification, cross-service errors, or when identifying the exact causal span and running service version.
---

# Distributed Tracing

## 目标

从真实 `trace_id` 重建请求关键路径，指出具体服务、Pod、版本、Span、SQL 或下游调用，而不是只比较服务平均延迟。

## 调查顺序

1. 从 Loki 业务日志或响应头取得合法 32 位 `trace_id`。
2. 使用 `query_trace(trace_id=...)` 精确读取 Trace，避免健康检查和无关请求。
3. 按父子关系计算 critical path，标记 server/client/database Span。
4. 检查同一 Trace 是否保持 `service.name`、`service.version`、environment 和 Pod 身份。
5. 对长数据库 Span 查询 slow log/EXPLAIN；对长 HTTP Span查询下游 Metrics/Logs。
6. 对重复兄弟 Span检查 retry attempt、间隔、幂等性和放大倍数。

## 证据要求

- Trace 只能证明耗时归属，根因仍需下游 Metrics、Logs、DB 或 Git Source 验证。
- 缺失 Span 时明确报告 instrumentation gap，不能假设缺失服务正常。
- 混合版本场景必须比较 Span 的 `service.version`，不能只看 service.name。

## 推荐 Tools

使用 `query_logs`、`query_trace`、`query_metrics`、`query_slow_queries`、`explain_sql`、`get_pod_version`、`read_file_at_commit`。

## 禁止事项

不得展示隐藏思维链，不得把相邻时间的两条请求拼成一条 Trace，不得修改采样率或服务配置。
