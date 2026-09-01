-- COMMENT: Gateway API Key 表，只保存不可逆 Hash，不保存 API Key 明文。
CREATE TABLE IF NOT EXISTS gateway_tokens (
    -- COMMENT: API Key 记录自增主键，对外仅作为内部调用方标识。
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'API Key 记录自增主键，对外仅作为内部调用方标识',
    -- COMMENT: gw_sk_ API Key 的 SHA-256 Hash，长度固定为 64。
    token_hash VARCHAR(64) NOT NULL COMMENT 'gw_sk_ API Key 的 SHA-256 Hash，数据库不保存明文',
    -- COMMENT: API Key 创建时间，带 UTC 时区的 ISO 8601 字符串。
    created_at VARCHAR(40) NOT NULL COMMENT 'API Key 创建时间，UTC ISO 8601 字符串',
    -- COMMENT: API Key 禁用时间；NULL 表示当前有效。
    disabled_at VARCHAR(40) NULL COMMENT 'API Key 禁用时间；NULL 表示当前有效',
    PRIMARY KEY (id),
    UNIQUE KEY uq_gateway_tokens_hash (token_hash),
    KEY idx_gateway_tokens_enabled (token_hash, disabled_at),
    CONSTRAINT ck_token_hash_length CHECK (CHAR_LENGTH(token_hash) = 64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Gateway API Key 表，仅保存不可逆 Hash，不保存 API Key 明文';
