# Evaluation

`sre-agent/evals/SRE-001.json`～`SRE-007.json` 分别定义 question、expected_service、expected_root_cause、expected_evidence_types、forbidden_wrong_causes 和 minimum_confidence。

运行前必须先触发对应场景，例如：

1. `./scripts/run-scenario.ps1 -Scenario SRE-001`
2. 在 `sre-agent` 执行 `python evals/run_evals.py --case SRE-001`

Runner 调用真实 `/api/agent/chat`，检查服务、根因关键词、证据类型、禁用误因与最低置信度。它不会自动注入故障，避免评测脚本修改系统状态或把多个场景证据混合。

评测失败必须保留为失败：不能因为模型提到预期关键词就忽略服务错误、证据不足或禁用误因。空查询不计入 VERIFY 的独立证据数。
