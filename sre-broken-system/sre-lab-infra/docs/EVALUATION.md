# Evaluation

`sre-agent/evals/SRE-001.json`～`SRE-010.json` 固定定义 `case_id`、`symptom`、`expected_service`、`expected_root_cause`、`required_evidence` 和 `forbidden_shortcuts`，不再增加新 Case。

运行前必须先触发对应场景，例如：

1. `./scripts/run-scenario.ps1 -Scenario SRE-001`
2. 在 `sre-agent` 执行 `python evals/run_evals.py --case SRE-001`

Runner 通过真实认证调用 `/api/agent/chat`，但请求体只包含 `symptom + project_id`。Case ID、Expected Root Cause、Required Evidence 与 Forbidden Shortcuts 不发送给 Agent，只供 Evaluator 评分。它不会自动注入故障，避免评测脚本修改系统状态或把多个场景证据混合。

SRE-008/009 还必须断言 `affected_pod` 非空；SRE-009 必须返回少数 BAD canary 的完整 Git SHA 与 `OrderRepository.java` 源码位置，不能用 Deployment 的多数 GOOD 版本代替 Pod 级事实。

全量执行 `python evals/run_evals.py`，结果写入 `evals/results/latest.json`。报告保留每个失败 Case 的原因，并统计 Service/Root Cause Accuracy、Evidence Completion Rate、平均 Tool Calls、诊断耗时和 Token Usage。空结果、无有效引用或 `insufficient_evidence` 不得伪装为通过。
