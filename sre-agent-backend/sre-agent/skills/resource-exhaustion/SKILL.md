---
name: resource-exhaustion
description: Diagnose CPU saturation, memory leaks, OOMKilled containers, connection pool exhaustion, and thread pool exhaustion. Use when latency, errors, or restarts correlate with finite compute, memory, connection, or worker capacity.
---

# Resource Exhaustion

## 什么时候触发

CPU 长时间接近 limit、内存单调增长、OOMKilled、Hikari pending、线程池拒绝或高并发下错误突增时触发。

## 目标

识别被耗尽的具体资源、增长或占用原因，以及它如何传播成延迟、5xx 或重启。

## 推荐诊断顺序

1. 对比当前用量、request、limit 与历史趋势。
2. 查询 Pod restart、lastState 和 Event。
3. 检查应用池 active/pending/queue 与异常日志。
4. 区分真正 CPU 计算、内存泄漏、慢依赖占用连接和错误重试放大。
5. 映射运行 SHA 并查找无界缓存、对象保留或无退避循环。

## 关注指标

关注 container CPU/throttling、working set、JVM heap、Node RSS、restart count、Hikari pending 和线程池 queue。

## 关注日志

关注 OOMKilled、OutOfMemory、allocation failure、connection timeout、RejectedExecution 和 retry。

## 推荐 Tools

使用 `query_metrics`、`query_logs`、`get_pod_status`、`get_pod_events`、`get_restart_count`、`read_file_at_commit`。

## 证据要求

资源耗尽必须有趋势或状态证据，并用 Event/Logs/Source 之一解释原因；仅看到高用量不足以证明根因。

## 禁止事项

不得直接提高 limit 掩盖泄漏。不得自动重启释放资源。不得混淆 JVM heap、进程 RSS 和容器 working set。
