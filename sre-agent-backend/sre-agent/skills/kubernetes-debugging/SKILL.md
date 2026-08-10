---
name: kubernetes-debugging
description: Diagnose Kubernetes pod health, events, restart reasons, resource usage, deployment state, services, container images, and runtime annotations using read-only tools. Use for pod failures, restarts, scheduling issues, and runtime identity checks.
---

# Kubernetes Debugging

## 什么时候触发

出现 Pod 未就绪、反复重启、OOMKilled、CrashLoopBackOff、调度失败、Service 不可达或需要确认运行镜像时触发。

## 目标

确定 Namespace → Deployment → Pod → Container → Image → Git SHA 的运行身份及失败状态。

## 推荐诊断顺序

1. 列出 Deployment 与 Pod，确认副本和 Ready 状态。
2. 读取目标 Pod status、containerStatuses 与 lastState。
3. 查询 Events，按时间区分历史与当前故障。
4. 查询 restart count 和 CPU/memory 使用量。
5. 读取 Deployment image 与 `sre.agent/git-sha` 注解。

## 关注指标

关注 Ready、restart count、working set memory、CPU、OOMKilled reason、exit code 和资源 limit。

## 关注日志

关注容器启动、探针失败、OOM、SIGTERM、配置缺失和镜像拉取错误。

## 推荐 Tools

使用 `list_pods`、`get_pod_status`、`get_pod_events`、`get_restart_count`、`get_resource_usage`、`get_container_image`。

## 证据要求

重启结论需要 container lastState 或 Event，并至少与 Metrics/Logs 中一个独立来源交叉验证。

## 禁止事项

禁止 delete_pod、restart、scale、apply、patch。不得将旧 Event 当作当前根因，不得忽略 Namespace。
