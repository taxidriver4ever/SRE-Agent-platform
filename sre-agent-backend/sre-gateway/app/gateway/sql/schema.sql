-- COMMENT: Gateway 模型调用用量与结果日志，不保存 Prompt、回复或 API Key 明文。
CREATE TABLE IF NOT EXISTS gateway_usage_logs (
    -- COMMENT: 调用日志自增主键。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- COMMENT: Gateway 请求唯一标识，用于链路追踪。
    request_id VARCHAR(64) NOT NULL UNIQUE,
    -- COMMENT: 发起调用的 Gateway API Key 内部 ID。
    token_id INTEGER NOT NULL,
    -- COMMENT: 实际调用的模型厂商。
    provider VARCHAR(32) NOT NULL,
    -- COMMENT: 路由后调用的模型名称。
    model VARCHAR(200) NOT NULL,
    -- COMMENT: 输入 Token 数量。
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    -- COMMENT: 输出 Token 数量。
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    -- COMMENT: 输入与输出 Token 总数。
    total_tokens INTEGER NOT NULL DEFAULT 0,
    -- COMMENT: Provider 调用耗时，单位毫秒。
    latency_ms INTEGER NOT NULL,
    -- COMMENT: 调用是否成功，SQLite 使用 0 或 1 保存布尔值。
    success BOOLEAN NOT NULL,
    -- COMMENT: Provider 或 Gateway 结果状态码。
    status_code INTEGER NOT NULL,
    -- COMMENT: 失败时的脱敏错误摘要，成功时为 NULL。
    error_message VARCHAR(500) NULL,
    -- COMMENT: 日志创建时间，带 UTC 时区的 ISO 8601 字符串。
    created_at VARCHAR NOT NULL
);

-- COMMENT: 按 API Key 和时间查询调用记录。
CREATE INDEX IF NOT EXISTS idx_gateway_usage_token_time
    ON gateway_usage_logs (token_id, created_at);

-- COMMENT: 按 Provider、模型和调用结果进行统计。
CREATE INDEX IF NOT EXISTS idx_gateway_usage_provider_model
    ON gateway_usage_logs (provider, model, success, created_at);
