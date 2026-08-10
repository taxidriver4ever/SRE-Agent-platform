---
name: go-service-debugging
description: Diagnose Go microservice latency, CPU, memory, goroutine growth, context deadline, HTTP timeout, GC, retry amplification, and goroutine leaks. Use for incidents involving inventory-service, notification-service, Go runtime metrics, or Go source code.
---

# Go Service Debugging

## 目标

用 Metrics、Logs、Trace、Kubernetes 与运行 SHA 对应源码区分下游超时、CPU 饱和、GC 压力和 goroutine 泄漏。

## 诊断顺序

1. 获取 Deployment、全部 Pods、镜像 SHA、重启次数和资源限制。
2. 对比 `go_goroutines`、CPU、heap、GC pause、按 Pod P95/错误率。
3. 以异常请求 `trace_id` 检查 `context deadline exceeded` 和下游 Span。
4. 查询结构化日志中的 attempt、timeout、pod、version。
5. 读取异常 Pod SHA 对应的 client/service/worker 源码及 GOOD..BAD diff。

## 关注证据

- goroutine 持续单调增长且请求结束后不回落，支持泄漏候选。
- 下游 Span 占主导且本服务 CPU 低，支持 dependency timeout。
- 同一输入出现多次无间隔下游 Span，支持 retry amplification。
- 结论至少需要两个独立来源；单条 `context deadline` 日志不够。

## 推荐 Tools

使用 `list_pods`、`get_pod_metrics`、`get_pod_version`、`query_metrics`、`query_logs`、`query_trace`、`get_commit_diff`、`read_file_at_commit`。

## 禁止事项

不得重启 Pod、扩缩容、发送信号、修改环境变量或把所有超时归因于 Go runtime。
