-- COMMENT: Gateway 操作审计日志，记录 API Key 与模型调用行为，不保存敏感明文。
CREATE TABLE IF NOT EXISTS gateway_operation_logs (
    -- COMMENT: 操作日志自增主键。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- COMMENT: 操作类型，例如 api_key.create、api_key.authenticate 或 gateway.chat.completion。
    operation VARCHAR(64) NOT NULL,
    -- COMMENT: 关联的 Gateway API Key 内部 ID，鉴权失败时可以为 NULL。
    token_id INTEGER NULL,
    -- COMMENT: 关联的 Gateway 请求 ID，非模型调用事件可以为 NULL。
    request_id VARCHAR(64) NULL,
    -- COMMENT: 操作是否成功，SQLite 使用 0 或 1 保存布尔值。
    success BOOLEAN NOT NULL,
    -- COMMENT: 操作结果状态码。
    status_code INTEGER NOT NULL,
    -- COMMENT: 不含 API Key、Prompt 或回复内容的结果摘要。
    detail VARCHAR(500) NULL,
    -- COMMENT: 操作时间，带 UTC 时区的 ISO 8601 字符串。
    created_at VARCHAR NOT NULL
);

-- COMMENT: 按操作类型、结果和时间检索审计事件。
CREATE INDEX IF NOT EXISTS idx_gateway_operations_type_time
    ON gateway_operation_logs (operation, success, created_at);

-- COMMENT: 按 API Key 内部 ID 和时间追踪操作。
CREATE INDEX IF NOT EXISTS idx_gateway_operations_token_time
    ON gateway_operation_logs (token_id, created_at);
