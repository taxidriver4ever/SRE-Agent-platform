CREATE TABLE IF NOT EXISTS tool_audit_logs (
    id CHAR(32) PRIMARY KEY COMMENT '审计记录 UUID',
    user_id CHAR(32) NOT NULL COMMENT '服务端认证用户 ID',
    project_id VARCHAR(80) NOT NULL COMMENT '项目级 Tool Policy 标识',
    task_id VARCHAR(64) NOT NULL COMMENT '一次 API/Agent Task 标识',
    tool_name VARCHAR(120) NOT NULL COMMENT '白名单工具名称',
    parameters_json JSON NOT NULL COMMENT '脱敏后的工具参数',
    result_status VARCHAR(20) NOT NULL COMMENT 'success、failed 或 denied',
    execution_time_ms INT UNSIGNED NOT NULL COMMENT '策略校验与执行总耗时',
    error_type VARCHAR(120) NULL COMMENT '失败类型，不保存异常栈或凭证',
    created_at VARCHAR(40) NOT NULL COMMENT 'UTC ISO-8601 时间',
    INDEX idx_tool_audit_task (project_id, task_id, created_at),
    INDEX idx_tool_audit_user (user_id, created_at),
    INDEX idx_tool_audit_tool (tool_name, result_status, created_at)
) COMMENT='只追加的 Tool 调用安全审计日志';
