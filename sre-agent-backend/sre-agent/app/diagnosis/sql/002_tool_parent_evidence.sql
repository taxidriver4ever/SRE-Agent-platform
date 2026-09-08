-- Investigation Step：保存 logical key 所依赖的父 Evidence，确保 RUNNING Tool 恢复后证据血缘不丢失。
ALTER TABLE diagnosis_investigation_steps
    ADD COLUMN parent_evidence_ids_json TEXT NULL COMMENT '生成 logical Tool key 时使用的父 Evidence ID JSON 数组' AFTER arguments_json;
