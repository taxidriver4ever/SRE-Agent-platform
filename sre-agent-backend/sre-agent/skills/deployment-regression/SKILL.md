---
name: deployment-regression
description: Diagnose incidents correlated with a deployment by mapping the running Kubernetes image and Git SHA to commit metadata, changed files, diffs, and source at that exact revision. Use when symptoms begin after release or runtime code identity matters.
---

# Deployment Regression

## 什么时候触发

用户提到“发布后”“升级后”，指标拐点接近 rollout，或需要确认当前运行代码是否包含某项变更时触发。

## 目标

建立 Runtime Image → Git SHA → Commit → Diff → Source → Symptom 的可审计映射。

## 推荐诊断顺序

1. 用 K8s 读取 Deployment image 和 `sre.agent/git-sha` 注解。
2. 获取该 SHA 的提交元数据，禁止默认使用 HEAD。
3. 确定前一个已知 GOOD SHA。
4. 列出 GOOD..BAD changed files，再读取最相关 diff。
5. 用运行时 Metrics/Logs/Trace 验证变更确实解释症状。

## 关注指标

关注 rollout 时间前后的 P95、5xx、CPU、memory、restart 与请求量变化。

## 关注日志

关注启动版本、配置变化、异常首次出现时间、OOM、timeout 和新代码路径标识。

## 推荐 Tools

使用 `get_container_image`、`get_commit`、`list_changed_files`、`get_commit_diff`、`read_file_at_commit`、`search_code`。

## 证据要求

代码差异只能作为根因证据之一；还需要至少一个运行来源证明时间相关性和机制相关性。

## 禁止事项

不得读取 main/master 最新内容代替运行 SHA。不得因时间接近就断言因果。不得执行 checkout、reset、commit 或 push。
