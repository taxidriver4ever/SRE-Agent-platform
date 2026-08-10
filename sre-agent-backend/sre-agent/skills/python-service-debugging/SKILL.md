---
name: python-service-debugging
description: Diagnose Python FastAPI incidents involving asyncio event-loop blocking, synchronous I/O, SQLAlchemy pools, CPU-bound work, process memory, database latency, or worker imbalance. Use for user-service and recommendation-service symptoms or Python source analysis.
---

# Python Service Debugging

## 目标

区分 async Event Loop 阻塞、同步数据库 I/O、CPU-bound 算法、连接池等待与普通下游延迟。

## 诊断顺序

1. 比较所有 Python Pods 的版本、CPU、内存、P95、错误率和 worker 重启。
2. 检查 event-loop/blocking 指标，并将峰值时间与请求日志对齐。
3. 用 Trace 判断耗时位于应用 Span、SQLAlchemy/数据库还是 HTTP 下游。
4. 检查同步函数是否直接运行于 `async def`，CPU 任务是否错误占用事件循环。
5. 读取运行 SHA 的 API、Service、Repository 与 GOOD..BAD diff。

## 关注证据

- CPU 高且 Trace 没有长下游 Span，优先检查 CPU-bound 工作。
- CPU 低但同一 worker 的无关请求同时停顿，检查 blocking I/O。
- 数据库 Span 与 pool pending 同步增长，检查 SQLAlchemy 连接处理。
- 按 Pod/版本比较，防止聚合平均值掩盖单实例异常。

## 推荐 Tools

使用 `query_metrics`、`query_logs`、`query_trace`、`list_pods`、`get_pod_version`、`get_resource_limits`、`get_commit_diff`、`read_file_at_commit`。

## 禁止事项

不得把 `async def` 等同于非阻塞；不得运行任意 Python 诊断代码、安装包或修改 worker 数量。
