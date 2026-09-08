-- 为 Diagnosis Session 增加独立工作流游标、Checkpoint、Lease、Heartbeat、CAS 和原子步骤序号。
ALTER TABLE diagnosis_sessions
    ADD COLUMN project_id VARCHAR(80) NOT NULL DEFAULT 'sre-lab' COMMENT '恢复时重新建立服务端安全作用域的项目标识' AFTER conversation_id,
    ADD COLUMN current_phase VARCHAR(40) NULL COMMENT '最近开始或完成的 Workflow Phase' AFTER status,
    ADD COLUMN phase_status ENUM('PENDING', 'RUNNING', 'COMPLETED') NOT NULL DEFAULT 'PENDING' COMMENT '当前 Phase 的持久执行状态' AFTER current_phase,
    ADD COLUMN checkpoint_json LONGTEXT NULL COMMENT 'DiagnosisState.model_dump(mode=json) 的可恢复快照' AFTER phase_status,
    ADD COLUMN checkpoint_seq BIGINT NOT NULL DEFAULT 0 COMMENT '每次成功保存 Checkpoint 后单调递增' AFTER checkpoint_json,
    ADD COLUMN attempt_no INT NOT NULL DEFAULT 0 COMMENT '同一 logical run 被物理 Executor claim 的次数' AFTER checkpoint_seq,
    ADD COLUMN heartbeat_at VARCHAR(40) NULL COMMENT '当前 Executor 最近心跳时间，ISO 8601 格式' AFTER attempt_no,
    ADD COLUMN lease_owner VARCHAR(120) NULL COMMENT '当前拥有执行权的进程级 Executor ID' AFTER heartbeat_at,
    ADD COLUMN lease_expires_at VARCHAR(40) NULL COMMENT 'Lease 过期时间，ISO 8601 格式' AFTER lease_owner,
    ADD COLUMN state_version BIGINT NOT NULL DEFAULT 0 COMMENT 'Durable State CAS 乐观锁版本' AFTER lease_expires_at,
    ADD COLUMN next_step_sequence INT NOT NULL DEFAULT 1 COMMENT '事务内分配的下一个 InvestigationStep 序号' AFTER state_version,
    ADD COLUMN interrupted_at VARCHAR(40) NULL COMMENT '进程关闭导致执行中断的时间，不代表业务取消' AFTER next_step_sequence,
    ADD COLUMN recovery_reason VARCHAR(255) NULL COMMENT '最近一次恢复或中断原因' AFTER interrupted_at,
    ADD INDEX idx_diagnoses_recovery (status, lease_expires_at),
    ADD INDEX idx_diagnoses_lease_owner (lease_owner, lease_expires_at);

-- 一个 logical Tool 始终复用一行 Step；RUNNING 恢复只增加 attempt_no。
ALTER TABLE diagnosis_investigation_steps
    ADD COLUMN idempotency_key VARCHAR(64) NULL COMMENT 'phase、tool、规范化参数和父 Evidence 的 SHA-256' AFTER sequence_no,
    ADD COLUMN arguments_json LONGTEXT NULL COMMENT '规范化 Tool 参数 JSON' AFTER tool_name,
    ADD COLUMN parent_evidence_ids_json TEXT NULL COMMENT '生成 logical Tool key 时使用的父 Evidence ID JSON 数组' AFTER arguments_json,
    ADD COLUMN result_json LONGTEXT NULL COMMENT '完成后持久化的原始只读 Tool Result JSON' AFTER parent_evidence_ids_json,
    ADD COLUMN attempt_no INT NOT NULL DEFAULT 1 COMMENT '该 logical Tool 的实际调用尝试次数' AFTER result_json,
    ADD COLUMN evidence_id VARCHAR(64) NULL COMMENT '与 logical Tool 绑定的稳定 Evidence ID' AFTER attempt_no,
    ADD COLUMN updated_at VARCHAR(40) NULL COMMENT 'Step 最近状态更新时间，ISO 8601 格式' AFTER error_message,
    ADD UNIQUE KEY uk_diagnosis_step_idempotency (diagnosis_id, idempotency_key);

-- 旧 Session 可能已经存在 Timeline；迁移时把原子计数器推进到当前最大序号之后。
UPDATE diagnosis_sessions session
LEFT JOIN (
    SELECT diagnosis_id, COALESCE(MAX(sequence_no), 0) + 1 AS next_no
    FROM diagnosis_investigation_steps
    GROUP BY diagnosis_id
) existing ON existing.diagnosis_id = session.id
SET session.next_step_sequence = COALESCE(existing.next_no, 1);

-- 重要领域事件使用 event_key 去重；NULL 保持对旧数据和非幂等事件兼容。
ALTER TABLE diagnosis_events
    ADD COLUMN event_key VARCHAR(190) NULL COMMENT '逻辑事件幂等键；恢复 attempt 事件使用不同键' AFTER event_type,
    ADD UNIQUE KEY uk_diagnosis_event_key (diagnosis_id, event_key);
