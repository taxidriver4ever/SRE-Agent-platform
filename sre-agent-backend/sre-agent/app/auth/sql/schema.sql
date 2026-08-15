-- Auth 模块：用户账号表。
CREATE TABLE IF NOT EXISTS users (
    id CHAR(32) NOT NULL COMMENT '用户唯一标识',
    username VARCHAR(80) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录用户名，不区分大小写',
    password_hash VARCHAR(255) NOT NULL COMMENT 'PBKDF2 密码哈希，不保存明文密码',
    created_at VARCHAR(40) NOT NULL COMMENT '账号创建时间，ISO 8601 格式',
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Agent 登录用户账号';

-- 确保已有表也能获得表级注释。
ALTER TABLE users COMMENT = 'Agent 登录用户账号';

-- Auth 模块：Bearer Token 表。
CREATE TABLE IF NOT EXISTS auth_tokens (
    id CHAR(32) NOT NULL COMMENT 'Token 记录唯一标识',
    user_id CHAR(32) NOT NULL COMMENT 'Token 所属用户标识',
    token_hash CHAR(64) NOT NULL COMMENT 'Token 的 SHA-256 哈希，不保存明文 Token',
    created_at VARCHAR(40) NOT NULL COMMENT 'Token 创建时间，ISO 8601 格式',
    expires_at VARCHAR(40) NOT NULL COMMENT 'Token 过期时间，ISO 8601 格式',
    revoked_at VARCHAR(40) NULL COMMENT 'Token 撤销时间，为空表示未撤销',
    PRIMARY KEY (id),
    UNIQUE KEY uq_auth_tokens_hash (token_hash),
    INDEX idx_auth_tokens_lookup (token_hash, expires_at, revoked_at),
    CONSTRAINT fk_auth_tokens_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户登录认证 Token';

ALTER TABLE auth_tokens COMMENT = '用户登录认证 Token';
