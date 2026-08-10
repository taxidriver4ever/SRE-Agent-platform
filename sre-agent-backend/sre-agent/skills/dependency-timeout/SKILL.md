---
name: dependency-timeout
description: Diagnose downstream dependency latency, client timeouts, retry amplification, and cascading failures using distributed traces, metrics, and logs. Use when an upstream service is slow despite low local CPU or logs show socket/read/connect timeout.
---

# Dependency Timeout

## 什么时候触发

上游延迟高但本地 CPU 不高、Trace 中 HTTP client Span 占主要耗时，或出现 connect/read/socket timeout 与重试日志时触发。

## 目标

定位最慢的调用边，区分连接失败、读取超时、下游处理慢和无退避重试风暴。

## 推荐诊断顺序

1. 从入口 Trace 找到最长 downstream Span 和目标 service。
2. 对比上下游 HTTP P95、5xx 与 health。
3. 查询下游同一时间窗口日志。
4. 统计超时后重试次数、间隔与放大倍数。
5. 从运行提交检查 timeout、retry、backoff 与熔断配置。

## 关注指标

关注 client/server latency、timeout/error rate、请求量放大、CPU、连接数与 retry count。

## 关注日志

关注 connect timeout、read timeout、context deadline、socket timeout、retry attempt 和 circuit breaker。

## 推荐 Tools

使用 `query_trace`、`query_metrics`、`query_logs`、`get_service_health`、`get_container_image`、`read_file_at_commit`。

## 证据要求

至少需要 Trace 加上下游 Metrics/Logs 之一；必须说明耗时发生在哪个服务边界和采用的时间窗口。

## 禁止事项

不得把所有超时归因于网络。不得无条件增加超时。不得建议无上限、无退避、无抖动的重试。
