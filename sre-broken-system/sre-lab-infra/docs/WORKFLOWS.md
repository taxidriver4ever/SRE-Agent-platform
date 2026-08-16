# Workflows

固定流程仍为 `START → TRIAGE → BASELINE_OBSERVATION → ANALYZE → INVESTIGATE → VERIFY → REPORT → END`。

Baseline 查询目标 Pods、运行版本、健康、P95、5xx、按 Pod CPU/内存和近期日志。Investigate 共享 State/Evidence/Report，由 Evidence Planner 根据当前 Tool Result 中真实出现的 `trace_id`、SQL、Pod、下游服务和 commit 选择下一步；Workflow 不按服务名或 Eval Case 写入根因分支。

每条后续 Evidence 保存触发它的 `parent_evidence_ids`。VERIFY 要求 Finding 引用存在、包含至少两条互相支持的证据和直接运行时证据，且证据没有矛盾；否则最终状态为 `insufficient_evidence`。
