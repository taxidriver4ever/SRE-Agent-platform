# Evaluation Results

该目录保存真实评测产物，不存放手工编写的“通过”结果。

- `latest.json`：最近一次固定 10 Case 全量评测。
- `SRE-xxx.json`：对应单 Case 的诊断结果，包括失败和超时。
- 失败记录不会被过滤，`failure_reason` 保留原始失败原因。

评测器只把 `symptom` 和 `project_id` 发送给 Agent；期望服务、根因和证据类型只在评测端参与打分。
