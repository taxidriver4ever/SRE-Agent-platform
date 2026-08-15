-- Conversation 模块：会话主表。
CREATE TABLE IF NOT EXISTS conversations (
    id CHAR(32) NOT NULL COMMENT '会话唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '会话所属用户标识',
    title VARCHAR(120) NOT NULL COMMENT '会话标题',
    created_at VARCHAR(40) NOT NULL COMMENT '会话创建时间，ISO 8601 格式',
    updated_at VARCHAR(40) NOT NULL COMMENT '会话最后更新时间，ISO 8601 格式',
    PRIMARY KEY (id),
    INDEX idx_conversations_user_updated (user_id, updated_at DESC),
    CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户与 Agent 的会话';

ALTER TABLE conversations COMMENT = '用户与 Agent 的会话';

-- Conversation 模块：会话消息明细表。
CREATE TABLE IF NOT EXISTS conversation_messages (
    id CHAR(32) NOT NULL COMMENT '消息唯一标识',
    conversation_id CHAR(32) NOT NULL COMMENT '消息所属会话标识',
    role ENUM('user', 'assistant') NOT NULL COMMENT '消息发送角色',
    message_type VARCHAR(32) NOT NULL DEFAULT 'assistant' COMMENT '消息业务类型',
    content_json LONGTEXT NOT NULL COMMENT '消息结构化内容 JSON',
    estimated_tokens INT NOT NULL DEFAULT 1 COMMENT '消息估算 Token 数量',
    run_id VARCHAR(80) NULL COMMENT '关联的 Agent 运行标识',
    tool_name VARCHAR(120) NULL COMMENT '关联的工具名称',
    created_at VARCHAR(40) NOT NULL COMMENT '消息创建时间，ISO 8601 格式',
    PRIMARY KEY (id),
    INDEX idx_messages_conversation_time (conversation_id, created_at, id),
    CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话中的用户、Agent 与工具消息';

ALTER TABLE conversation_messages COMMENT = '会话中的用户、Agent 与工具消息';
