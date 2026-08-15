-- COMMENT: Gateway API Key 表，只保存不可逆 Hash，不保存 API Key 明文。
CREATE TABLE IF NOT EXISTS gateway_tokens (
    -- COMMENT: API Key 记录自增主键，对外仅作为内部调用方标识。
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- COMMENT: gw_sk_ API Key 的 SHA-256 Hash，长度固定为 64。
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    -- COMMENT: API Key 创建时间，带 UTC 时区的 ISO 8601 字符串。
    created_at VARCHAR NOT NULL,
    -- COMMENT: API Key 禁用时间；NULL 表示当前有效。
    disabled_at VARCHAR NULL,
    CONSTRAINT ck_token_hash_length CHECK (length(token_hash) = 64)
);

-- COMMENT: 加速有效 API Key 的 Hash 与禁用状态查询。
CREATE INDEX IF NOT EXISTS idx_gateway_tokens_enabled
    ON gateway_tokens (token_hash, disabled_at);
