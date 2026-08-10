---
name: nodejs-service-debugging
description: Diagnose Node.js and TypeScript microservice incidents involving event-loop delay, heap or RSS growth, retained objects, Promise backlogs, HTTP timeouts, OOMKilled, and async error handling. Use for payment-service symptoms or Node source analysis.
---

# Node.js Service Debugging

## 目标

通过 Node runtime、容器和代码证据区分 V8 heap 泄漏、外部 Buffer/RSS 泄漏、Event Loop 阻塞和 Promise 积压。

## 诊断顺序

1. 查询所有 payment Pods 的 restart count、last state、limits、RSS、heap 和版本。
2. 对齐 event-loop delay、HTTP P95、请求量和错误率时间线。
3. OOM 场景检查 K8s `OOMKilled`、容器 limit 与内存增长斜率。
4. 查询日志中的 retained bytes、Promise 数量、timeout 和 trace_id。
5. 读取运行 SHA 对应 Repository/Service/Client 源码和 diff，寻找无界集合、listener、Buffer 或未完成 Promise。

## 证据要求

- RSS 增长但 V8 heap 稳定时，优先检查 Buffer/native memory。
- heap 与对象保留同步增长时，检查 Map、数组、闭包和 listener。
- 至少用 Kubernetes Events + Metrics，或 Metrics + Source 交叉确认。

## 推荐 Tools

使用 `get_restart_count`、`get_events`、`get_resource_limits`、`query_metrics`、`query_logs`、`query_trace`、`get_commit_diff`、`read_file_at_commit`。

## 禁止事项

不得触发 heap dump、向进程发送信号、提高 memory limit 或自动重启 Pod。
