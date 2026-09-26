CREATE TABLE IF NOT EXISTS diagnosis_history_items (
    history_id CHAR(32) NOT NULL,
    task_id CHAR(32) NOT NULL,
    user_id CHAR(32) NOT NULL,
    project_id VARCHAR(120) NOT NULL,
    document_json LONGTEXT NOT NULL,
    index_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    retry_count INT NOT NULL DEFAULT 0,
    last_index_error VARCHAR(255) NULL,
    next_retry_at VARCHAR(40) NOT NULL,
    indexed_at VARCHAR(40) NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (history_id),
    UNIQUE KEY uq_history_task (task_id),
    INDEX idx_history_retry (index_status, next_retry_at),
    INDEX idx_history_owner (user_id, project_id),
    CONSTRAINT fk_history_task FOREIGN KEY (task_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='MySQL historical case source and retry queue';

CREATE TABLE IF NOT EXISTS history_event_deliveries (
    event_id BIGINT NOT NULL,
    delivered_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (event_id),
    CONSTRAINT fk_history_event FOREIGN KEY (event_id) REFERENCES diagnosis_events(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Acknowledged optional event search deliveries';
