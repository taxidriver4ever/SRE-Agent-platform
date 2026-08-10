"""Auth、Conversation 与 Message 共用的 SQLite 数据库初始化器。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


class ApplicationDatabase:
    """提供短连接和幂等 Schema 初始化，避免模块各自创建不一致的数据表。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        """创建启用外键、Row 访问和并发等待的独立短连接。"""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        """一次事务创建认证、会话和对象映射表；重复启动不会覆盖已有数据。"""
        with closing(self.connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_auth_tokens_lookup
                    ON auth_tokens(token_hash, expires_at, revoked_at);

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
                    ON conversations(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
                    ON conversation_messages(conversation_id, created_at, id);

                -- 用户附件表只保存会话关系和 MinIO 对象 Key。文件名、Content-Type、
                -- 文件正文及预签名 URL 均不进入数据库，避免数据库承担对象存储职责。
                CREATE TABLE IF NOT EXISTS conversation_attachments (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    oss_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attachments_conversation_time
                    ON conversation_attachments(conversation_id, created_at, id);

                -- Evidence 映射表不保存 payload_json。原始 Tool 结果和用户上传内容
                -- 都在 MinIO，SQLite 仅负责从可读 Evidence ID 定位 oss_key。
                CREATE TABLE IF NOT EXISTS evidence_objects (
                    run_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    oss_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_objects_run_time
                    ON evidence_objects(run_id, created_at, evidence_id);
            """)
            connection.commit()
