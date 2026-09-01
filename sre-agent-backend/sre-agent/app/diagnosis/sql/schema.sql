-- Diagnosis 模块：一次事件级故障调查的主记录。
CREATE TABLE IF NOT EXISTS diagnosis_sessions (
    id CHAR(32) NOT NULL COMMENT '诊断会话唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '诊断所属用户标识',
    conversation_id CHAR(32) NOT NULL COMMENT '复用的 Conversation 上下文标识',
    run_id VARCHAR(80) NULL COMMENT '底层 DiagnosisWorkflow 运行标识',
    question TEXT NOT NULL COMMENT '用户问题或对象驱动诊断描述',
    trigger_type ENUM('QUESTION', 'SERVICE', 'POD') NOT NULL COMMENT '诊断触发类型',
    initial_target_type ENUM('SERVICE', 'POD') NULL COMMENT '初始资源类型；问题驱动可为空',
    initial_target_id VARCHAR(255) NULL COMMENT '初始 Service 或 Pod 名称',
    initial_target_namespace VARCHAR(120) NULL COMMENT '初始 Kubernetes Namespace',
    status ENUM('PENDING', 'INVESTIGATING', 'COMPLETED', 'FAILED', 'CANCELLED') NOT NULL COMMENT '诊断状态机状态',
    summary TEXT NULL COMMENT '对本次 Incident 的结构化摘要',
    affected_services_json TEXT NOT NULL COMMENT '受影响服务名称 JSON 数组',
    error_message TEXT NULL COMMENT '诊断级失败原因；单 Tool 失败不写入此字段',
    started_at VARCHAR(40) NULL COMMENT '诊断开始时间，ISO 8601 格式',
    finished_at VARCHAR(40) NULL COMMENT '诊断结束时间，ISO 8601 格式',
    created_at VARCHAR(40) NOT NULL COMMENT '记录创建时间，ISO 8601 格式',
    updated_at VARCHAR(40) NOT NULL COMMENT '记录更新时间，ISO 8601 格式',
    PRIMARY KEY (id),
    INDEX idx_diagnoses_user_updated (user_id, updated_at DESC),
    INDEX idx_diagnoses_status (status, updated_at),
    CONSTRAINT fk_diagnoses_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_diagnoses_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='事件级 Diagnosis Session';

ALTER TABLE diagnosis_sessions COMMENT = '事件级 Diagnosis Session';

-- Diagnosis 模块：可审计的外部调查步骤，不保存模型隐藏思维链。
CREATE TABLE IF NOT EXISTS diagnosis_investigation_steps (
    id CHAR(32) NOT NULL COMMENT '调查步骤唯一标识',
    diagnosis_id CHAR(32) NOT NULL COMMENT '所属诊断标识',
    sequence_no INT NOT NULL COMMENT '诊断内单调递增序号',
    step_type VARCHAR(40) NOT NULL COMMENT 'TARGET_RESOLUTION、TOOL 或 REPORT',
    target_type VARCHAR(40) NULL COMMENT 'SERVICE、POD、DATABASE 等调查对象类型',
    target_id VARCHAR(255) NULL COMMENT '调查对象名称',
    tool_name VARCHAR(120) NULL COMMENT '实际执行的只读 Tool 名称',
    status ENUM('PENDING', 'RUNNING', 'COMPLETED', 'FAILED') NOT NULL COMMENT '步骤执行状态',
    started_at VARCHAR(40) NOT NULL COMMENT '步骤开始时间，ISO 8601 格式',
    finished_at VARCHAR(40) NULL COMMENT '步骤结束时间，ISO 8601 格式',
    summary TEXT NOT NULL COMMENT '外部可验证结果摘要',
    evidence_ids_json TEXT NOT NULL COMMENT '本步骤产生的 Evidence ID JSON 数组',
    error_message TEXT NULL COMMENT '该 Tool 独立失败原因',
    PRIMARY KEY (id),
    UNIQUE KEY uk_diagnosis_step_sequence (diagnosis_id, sequence_no),
    CONSTRAINT fk_steps_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Diagnosis Investigation Timeline 步骤';

ALTER TABLE diagnosis_investigation_steps COMMENT = 'Diagnosis Investigation Timeline 步骤';

-- Diagnosis 模块：有界摘要与结构化原始数据分离保存的 Evidence Store。
CREATE TABLE IF NOT EXISTS diagnosis_evidence (
    diagnosis_id CHAR(32) NOT NULL COMMENT '所属诊断标识',
    id VARCHAR(64) NOT NULL COMMENT 'Evidence 唯一标识，可复用 Conversation Tool Result ID',
    source_type VARCHAR(40) NOT NULL COMMENT 'KUBERNETES、PROMETHEUS、LOKI、TEMPO、MYSQL、GIT 或 CODE',
    source_name VARCHAR(120) NOT NULL COMMENT '具体 Tool 或数据源名称',
    resource_type VARCHAR(40) NULL COMMENT 'SERVICE、POD、DATABASE 等资源类型',
    resource_id VARCHAR(255) NULL COMMENT '证据关联资源名称',
    title VARCHAR(255) NOT NULL COMMENT '证据标题',
    summary TEXT NOT NULL COMMENT '提供给 Agent 与列表页的有界摘要',
    raw_data_json LONGTEXT NOT NULL COMMENT '结构化原始结果或完整结果引用 JSON',
    metadata_json TEXT NOT NULL COMMENT '引用、父证据、导航提示等元数据 JSON',
    supports_conclusion BOOLEAN NOT NULL DEFAULT TRUE COMMENT '是否支持最终结论',
    timestamp VARCHAR(40) NOT NULL COMMENT '证据采集时间，ISO 8601 格式',
    PRIMARY KEY (diagnosis_id, id),
    INDEX idx_evidence_source (diagnosis_id, source_type),
    CONSTRAINT fk_evidence_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Diagnosis Evidence Store';

ALTER TABLE diagnosis_evidence COMMENT = 'Diagnosis Evidence Store';

-- Diagnosis 模块：Incident Graph 节点。
CREATE TABLE IF NOT EXISTS diagnosis_graph_nodes (
    diagnosis_id CHAR(32) NOT NULL COMMENT '所属诊断标识',
    node_id VARCHAR(255) NOT NULL COMMENT '图内稳定节点标识',
    node_type VARCHAR(40) NOT NULL COMMENT 'SERVICE、POD、DEPLOYMENT、DATABASE、REDIS、KAFKA 或 EXTERNAL_API',
    name VARCHAR(255) NOT NULL COMMENT '节点展示名称',
    status VARCHAR(40) NOT NULL COMMENT 'UNKNOWN、HEALTHY、AFFECTED 或 ROOT_CAUSE',
    metadata_json TEXT NOT NULL COMMENT '指标、Namespace、版本等扩展元数据 JSON',
    PRIMARY KEY (diagnosis_id, node_id),
    CONSTRAINT fk_graph_nodes_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Diagnosis Incident Graph 节点';

ALTER TABLE diagnosis_graph_nodes COMMENT = 'Diagnosis Incident Graph 节点';

-- Diagnosis 模块：Incident Graph 有向边与可验证延迟。
CREATE TABLE IF NOT EXISTS diagnosis_graph_edges (
    id CHAR(32) NOT NULL COMMENT '图边唯一标识',
    diagnosis_id CHAR(32) NOT NULL COMMENT '所属诊断标识',
    source_node_id VARCHAR(255) NOT NULL COMMENT '起点 node_id',
    target_node_id VARCHAR(255) NOT NULL COMMENT '终点 node_id',
    relation_type VARCHAR(40) NOT NULL COMMENT 'HTTP、GRPC、SQL、REDIS、KAFKA、OWNS、RUNS_ON、CALLS 或 DEPENDS_ON',
    latency_ms DECIMAL(14,3) NULL COMMENT '证据中观察到的边延迟毫秒数',
    status VARCHAR(40) NOT NULL COMMENT 'UNKNOWN、HEALTHY、AFFECTED 或 ROOT_CAUSE',
    evidence_ids_json TEXT NOT NULL COMMENT '支撑该关系的 Evidence ID JSON 数组',
    metadata_json TEXT NOT NULL COMMENT '协议、路由等扩展元数据 JSON',
    PRIMARY KEY (id),
    UNIQUE KEY uk_graph_edge (diagnosis_id, source_node_id, target_node_id, relation_type),
    CONSTRAINT fk_graph_edges_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Diagnosis Incident Graph 有向边';

ALTER TABLE diagnosis_graph_edges COMMENT = 'Diagnosis Incident Graph 有向边';

-- Diagnosis 模块：结构化 Root Cause；一个 Session 只保留当前最终结论。
CREATE TABLE IF NOT EXISTS diagnosis_root_causes (
    diagnosis_id CHAR(32) NOT NULL COMMENT '所属诊断标识',
    title VARCHAR(255) NOT NULL COMMENT '根因标题',
    description TEXT NOT NULL COMMENT '根因机制与传播链说明',
    root_resource_type VARCHAR(40) NULL COMMENT '根因资源类型',
    root_resource_name VARCHAR(255) NULL COMMENT '根因资源名称',
    confidence DECIMAL(5,4) NOT NULL COMMENT '0 到 1 的结论置信度',
    evidence_ids_json TEXT NOT NULL COMMENT '支撑根因的 Evidence ID JSON 数组',
    recommendations_json TEXT NOT NULL COMMENT '只读修复建议 JSON 数组',
    created_at VARCHAR(40) NOT NULL COMMENT '根因生成时间，ISO 8601 格式',
    PRIMARY KEY (diagnosis_id),
    CONSTRAINT fk_root_cause_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Diagnosis 结构化 Root Cause';

ALTER TABLE diagnosis_root_causes COMMENT = 'Diagnosis 结构化 Root Cause';

-- Diagnosis 模块：支持 SSE 断线重连和历史回放的领域事件。
CREATE TABLE IF NOT EXISTS diagnosis_events (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '全局单调递增事件标识',
    diagnosis_id CHAR(32) NOT NULL COMMENT '所属诊断标识',
    event_type VARCHAR(80) NOT NULL COMMENT 'diagnosis.started、step.completed 等事件类型',
    data_json LONGTEXT NOT NULL COMMENT '事件负载 JSON',
    created_at VARCHAR(40) NOT NULL COMMENT '事件创建时间，ISO 8601 格式',
    PRIMARY KEY (id),
    INDEX idx_diagnosis_events (diagnosis_id, id),
    CONSTRAINT fk_events_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Diagnosis SSE 领域事件';

ALTER TABLE diagnosis_events COMMENT = 'Diagnosis SSE 领域事件';
