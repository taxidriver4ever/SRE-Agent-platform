---
name: incident-triage
description: Scope vague SRE incident questions into a concrete service, symptom, environment, and time range. Use for first-response triage when a user says a module is slow, failing, unstable, or restarting without enough diagnostic context.
---

# Incident Triage

## 什么时候触发

在用户只描述“订单很慢”“接口报错”“服务不稳定”等模糊现象时触发。先确定调查边界，再进入专项诊断。

## 目标

输出 `service`、`symptom`、`environment`、`time_range` 四个字段。无法确认时保留 `unknown`，不得擅自认定根因。

## 推荐诊断顺序

1. 用 Service Catalog 将业务别名映射为 Kubernetes Service。
2. 未给时间时采用最近 30 分钟，并在报告中明示。
3. 查询 Deployment、Pod 和 service health 验证服务存在且可达。
4. 将症状归类为 latency、5xx、pod_restart、dependency_timeout 或 deployment_regression。
5. 转交对应专项工作流，并保留最初假设。

## 关注指标

关注 `up`、HTTP 请求量、P95、5xx rate、CPU、memory、restart count。此阶段只建立基线，不解释因果。

## 关注日志

查看最近异常级别、超时、OOM、连接池、拒绝执行和部署启动日志；限制 Top 20 并保留时间戳。

## 推荐 Tools

依次使用 `list_deployments`、`list_pods`、`get_service`、`get_service_health`、`query_metrics`、`query_logs`。

## 证据要求

服务归属必须由 Catalog 或 K8s 对象支持。时间窗口和环境必须出现在最终报告中。

## 禁止事项

不得重启、扩缩容、删除 Pod 或修改配置。不得从服务名称直接推导根因，不得跳过基线观测。
