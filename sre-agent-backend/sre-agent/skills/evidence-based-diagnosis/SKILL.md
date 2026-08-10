---
name: evidence-based-diagnosis
description: Build evidence-backed SRE root-cause reports with independent sources, explicit causal chains, confidence, fixes, and an investigation timeline. Use before declaring any incident root cause or producing the final diagnosis report.
---

# Evidence Based Diagnosis

## 什么时候触发

每次诊断进入 VERIFY 与 REPORT 阶段时触发，尤其用于防止模型把候选原因表述成已确认根因。

## 目标

输出 Conclusion、Root Cause、Evidence、Root Cause Chain、Recommended Fix、Confidence 和 Investigation Timeline。

## 推荐诊断顺序

1. 将每条 Observation 标注来源、时间、工具和摘要。
2. 区分事实、推断和候选原因。
3. 为首选根因寻找至少两个独立来源。
4. 写出从缺陷到用户症状的逐步因果链。
5. 检查修复建议是否直接切断链条，并定义复测方法。
6. 按证据强度给出 0~1 Confidence。

## 关注指标

只引用实际返回的数据；空结果、查询失败和过期窗口必须显式记录，不能被当成正常证据。

## 关注日志

保留时间戳、service、pod、level、trace_id 和关键正文；使用 Top N 与关键词过滤，不转储数千行。

## 推荐 Tools

综合使用 `query_metrics`、`query_logs`、`query_trace`、MySQL Tools、Kubernetes Tools 与 Git Tools。

## 证据要求

确定性结论至少需要两个独立类别，例如 Logs+Metrics、Trace+Slow Query、Events+Metrics 或 Slow Query+Source。否则写“高可能性候选根因”。

## 禁止事项

禁止捏造数值、Trace、提交或查询结果。禁止隐藏工具错误。禁止把同一条日志的不同字段算作多个独立证据。
