-- Conversation Memory 模块：会话上下文压缩记录。
CREATE TABLE IF NOT EXISTS conversation_compactions (
    id CHAR(32) NOT NULL COMMENT '压缩记录唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '压缩记录所属用户标识',
    conversation_id CHAR(32) NOT NULL COMMENT '压缩记录所属会话标识',
    conversation_summary TEXT NOT NULL COMMENT '压缩后的会话摘要',
    context_state_json LONGTEXT NOT NULL COMMENT '压缩后的上下文状态 JSON',
    compacted_through_message_id CHAR(32) NOT NULL COMMENT '本次压缩覆盖到的最后消息标识',
    input_token_count INT NOT NULL COMMENT '执行压缩时的输入 Token 数量',
    created_at VARCHAR(40) NOT NULL COMMENT '压缩记录创建时间，ISO 8601 格式',
    PRIMARY KEY (id),
    INDEX idx_compactions_latest (conversation_id, created_at DESC, id DESC),
    CONSTRAINT fk_compactions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_compactions_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话上下文压缩快照';

ALTER TABLE conversation_compactions COMMENT = '会话上下文压缩快照';

-- Conversation Memory 模块：从压缩上下文提取的长期记忆项。
CREATE TABLE IF NOT EXISTS conversation_memory_items (
    id CHAR(32) NOT NULL COMMENT '记忆项唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '记忆项所属用户标识',
    conversation_id CHAR(32) NOT NULL COMMENT '记忆项所属会话标识',
    compaction_id CHAR(32) NOT NULL COMMENT '产生该记忆项的压缩记录标识',
    item_type VARCHAR(40) NOT NULL COMMENT '记忆项类型',
    title VARCHAR(255) NOT NULL COMMENT '记忆项标题',
    content TEXT NOT NULL COMMENT '记忆项正文',
    status VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '记忆项状态',
    importance DOUBLE NOT NULL DEFAULT 0.5 COMMENT '重要度，范围由业务层约束',
    source_message_id CHAR(32) NULL COMMENT '来源消息标识',
    source_tool_name VARCHAR(120) NULL COMMENT '来源工具名称',
    fingerprint CHAR(64) NOT NULL COMMENT '会话内去重指纹',
    created_at VARCHAR(40) NOT NULL COMMENT '记忆项创建时间，ISO 8601 格式',
    updated_at VARCHAR(40) NOT NULL COMMENT '记忆项最后更新时间，ISO 8601 格式',
    PRIMARY KEY (id),
    UNIQUE KEY uq_memory_conversation_fingerprint (conversation_id, fingerprint),
    INDEX idx_memory_conversation_lookup (user_id, conversation_id, status, item_type, importance DESC, updated_at DESC),
    CONSTRAINT fk_memory_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_compaction FOREIGN KEY (compaction_id) REFERENCES conversation_compactions(id) ON DELETE CASCADE,
    CONSTRAINT fk_memory_source_message FOREIGN KEY (source_message_id) REFERENCES conversation_messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='可检索的会话长期记忆项';

ALTER TABLE conversation_memory_items COMMENT = '可检索的会话长期记忆项';
