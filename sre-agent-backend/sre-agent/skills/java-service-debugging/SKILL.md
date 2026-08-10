---
name: java-service-debugging
description: Diagnose Java service incidents involving HikariCP, GC, JVM heap, OOM, thread pools, SQLTimeout, RejectedExecution, or downstream timeouts. Use after triage identifies a JVM service such as order-service.
---

# Java Service Debugging

## 什么时候触发

目标服务语言为 Java，或日志出现 Hikari、GC、OOM、SQLTimeout、RejectedExecution、线程池及下游超时信号时触发。

## 目标

区分数据库等待、线程池排队、GC/Heap 压力、CPU 饱和与下游等待，并给出可验证的耗时归属。

## 推荐诊断顺序

1. 查询 HTTP 延迟、错误率、JVM heap、GC pause 和 process CPU。
2. 用 Loki 检索 Hikari timeout、SQLTimeout、OOM、RejectedExecution 和 timeout。
3. 用 Tempo 比较入口 Span、JDBC Span 与 HTTP client Span。
4. 若 JDBC 慢，转入 database-troubleshooting；若 HTTP client 慢，转入 dependency-timeout。
5. 查询运行镜像和 Git SHA，再读取对应提交源码。

## 关注指标

关注 `jvm_memory_used_bytes`、`jvm_gc_pause_seconds`、Hikari active/pending、HTTP P95、process CPU 和 container memory。

## 关注日志

关注 HikariCP、GC overhead、OutOfMemoryError、SQLTimeoutException、RejectedExecutionException、SocketTimeoutException。

## 推荐 Tools

使用 `query_metrics`、`query_logs`、`query_trace`、`query_slow_queries`、`get_container_image`、`read_file_at_commit`。

## 证据要求

至少用 Trace 与 Metrics/Logs 中的一项确认时间消耗位置；代码只作为因果补充，不能替代运行证据。

## 禁止事项

不得仅凭单条异常日志定根因。不得读取 main 最新代码代替运行 SHA。不得自动修改 JVM 参数或重启服务。
