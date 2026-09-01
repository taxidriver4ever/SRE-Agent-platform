-- COMMENT: Gateway 模型调用用量与结果日志，不保存 Prompt、回复或 API Key 明文。
CREATE TABLE IF NOT EXISTS gateway_usage_logs (
    -- COMMENT: 调用日志自增主键。
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '调用日志自增主键',
    -- COMMENT: Gateway 请求唯一标识，用于链路追踪。
    request_id VARCHAR(64) NOT NULL COMMENT 'Gateway 请求唯一标识，用于链路追踪',
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
    -- COMMENT: 调用是否成功，MySQL BOOLEAN 使用 0 或 1 保存。
    success BOOLEAN NOT NULL,
    -- COMMENT: Provider 或 Gateway 结果状态码。
    status_code INTEGER NOT NULL,
    -- COMMENT: 失败时的脱敏错误摘要，成功时为 NULL。
    error_message VARCHAR(500) NULL,
    -- COMMENT: 日志创建时间，带 UTC 时区的 ISO 8601 字符串。
    created_at VARCHAR(40) NOT NULL COMMENT '日志创建时间，UTC ISO 8601 字符串',
    PRIMARY KEY (id),
    UNIQUE KEY uq_gateway_usage_request_id (request_id),
    KEY idx_gateway_usage_token_time (token_id, created_at),
    KEY idx_gateway_usage_provider_model (provider, model, success, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Gateway 模型调用用量与结果日志，不保存 Prompt、回复或 API Key 明文';
