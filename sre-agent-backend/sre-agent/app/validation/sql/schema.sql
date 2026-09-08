-- Pre-Merge Validation 主任务；Branch 在创建时冻结为两个精确 Commit SHA。
CREATE TABLE IF NOT EXISTS validation_runs (
    id CHAR(32) NOT NULL COMMENT 'ValidationRun 唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '任务所属用户；所有 API 查询必须携带该字段',
    project_id VARCHAR(80) NOT NULL COMMENT '工具策略中的项目安全域',
    idempotency_key VARCHAR(128) NOT NULL COMMENT '用户作用域内的创建请求幂等键',
    repository VARCHAR(120) NOT NULL COMMENT 'RepositoryRegistry 中的授权仓库名称',
    repository_url VARCHAR(1000) NULL COMMENT '去凭据后的授权 HTTPS 仓库地址',
    base_ref VARCHAR(240) NOT NULL COMMENT '用户选择的 Base Branch 名称，仅用于展示',
    base_commit_sha CHAR(40) NOT NULL COMMENT '创建时解析并冻结的 Base Commit SHA',
    candidate_ref VARCHAR(240) NOT NULL COMMENT '用户选择的 Candidate Branch 名称，仅用于展示',
    candidate_commit_sha CHAR(40) NOT NULL COMMENT '创建时解析并冻结的 Candidate Commit SHA',
    status ENUM('PENDING','PREPARING','RUNNING','COMPARING','COMPLETED','FAILED','CANCELLED') NOT NULL COMMENT 'Validation 明确状态机',
    runner_type VARCHAR(40) NOT NULL COMMENT '当前固定为 EPHEMERAL_DOCKER',
    project_type ENUM('MAVEN','GRADLE','PYTHON','NODE','UNKNOWN') NOT NULL COMMENT 'ProjectDetector 识别结果',
    test_suite_hash CHAR(64) NOT NULL COMMENT '两侧必须一致的测试套件哈希',
    environment_fingerprint CHAR(64) NULL COMMENT '两侧一致环境配置的稳定指纹',
    run_existing_tests BOOLEAN NOT NULL COMMENT '是否运行仓库已有测试',
    run_uploaded_tests BOOLEAN NOT NULL COMMENT '是否注入已校验的用户测试',
    generate_ai_tests BOOLEAN NOT NULL COMMENT '是否生成受限 AI 测试',
    started_at VARCHAR(40) NULL COMMENT '开始执行时间，ISO 8601',
    finished_at VARCHAR(40) NULL COMMENT '终态时间，ISO 8601',
    heartbeat_at VARCHAR(40) NULL COMMENT '最近编排心跳；启动恢复据此识别遗留任务',
    summary TEXT NULL COMMENT '有界 Validation 结果摘要',
    regression_count INT NOT NULL DEFAULT 0 COMMENT '新回归和构建回归数量',
    existing_failure_count INT NOT NULL DEFAULT 0 COMMENT 'Base 已存在且 Candidate 仍失败的数量',
    possible_fix_count INT NOT NULL DEFAULT 0 COMMENT 'Base 失败而 Candidate 通过的数量',
    comparison_confidence ENUM('HIGH','MEDIUM','LOW','INCONCLUSIVE') NOT NULL DEFAULT 'INCONCLUSIVE' COMMENT '差分结论置信度',
    diagnosis_id CHAR(32) NULL COMMENT '用户主动创建的关联 DiagnosisSession',
    error_message TEXT NULL COMMENT '任务级失败或恢复原因',
    changed_files_json TEXT NOT NULL COMMENT 'name-status 变更文件 JSON',
    created_at VARCHAR(40) NOT NULL COMMENT '创建时间，ISO 8601',
    updated_at VARCHAR(40) NOT NULL COMMENT '更新时间，ISO 8601',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_user_idempotency (user_id, idempotency_key),
    INDEX idx_validation_user_updated (user_id, updated_at DESC),
    INDEX idx_validation_status (status, updated_at),
    CONSTRAINT fk_validation_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_validation_diagnosis FOREIGN KEY (diagnosis_id) REFERENCES diagnosis_sessions(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Base 与 Candidate 差分验证任务';

-- 每个任务恰好拥有 BASE/CANDIDATE 两个逻辑执行，唯一键防止重试重复插入。
CREATE TABLE IF NOT EXISTS validation_executions (
    id CHAR(32) NOT NULL COMMENT '执行记录唯一标识',
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    side ENUM('BASE','CANDIDATE') NOT NULL COMMENT '差分执行侧',
    commit_sha CHAR(40) NOT NULL COMMENT '实际执行的不可变 Commit SHA',
    idempotency_key CHAR(64) NOT NULL COMMENT 'validation+side+sha+suite hash 的稳定键',
    test_suite_hash CHAR(64) NOT NULL COMMENT '该侧使用的测试套件哈希',
    environment_fingerprint CHAR(64) NOT NULL COMMENT 'Runner 环境稳定指纹',
    environment_json TEXT NOT NULL COMMENT '镜像、CPU、内存、运行时和网络策略 JSON',
    status ENUM('PENDING','RUNNING','COMPLETED','FAILED','TIMEOUT') NOT NULL COMMENT '单侧执行状态',
    build_status ENUM('NOT_RUN','PASSED','FAILED','TIMEOUT') NOT NULL COMMENT '构建阶段状态',
    test_status ENUM('NOT_RUN','PASSED','FAILED','TIMEOUT') NOT NULL COMMENT '测试阶段状态',
    started_at VARCHAR(40) NULL COMMENT 'Runner 启动时间',
    finished_at VARCHAR(40) NULL COMMENT 'Runner 结束时间',
    exit_code INT NULL COMMENT '最后执行阶段退出码',
    duration_ms BIGINT NOT NULL DEFAULT 0 COMMENT '准备、构建和测试总耗时毫秒',
    stdout_summary TEXT NOT NULL COMMENT '标准输出有界摘要，不保存无限日志',
    stderr_summary TEXT NOT NULL COMMENT '标准错误有界摘要，不保存无限日志',
    artifact_metadata_json TEXT NOT NULL COMMENT '完整日志和结果 Artifact 引用 JSON',
    created_at VARCHAR(40) NOT NULL COMMENT '创建时间',
    updated_at VARCHAR(40) NOT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_execution_logical (validation_id, side, commit_sha, test_suite_hash),
    UNIQUE KEY uk_validation_execution_key (validation_id, idempotency_key),
    CONSTRAINT fk_validation_execution_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Validation Base/Candidate 逻辑执行';

-- Surefire/pytest XML 归一化后的单测试结果。
CREATE TABLE IF NOT EXISTS validation_test_results (
    id CHAR(32) NOT NULL COMMENT '测试结果唯一标识',
    execution_id CHAR(32) NOT NULL COMMENT '所属 BASE 或 CANDIDATE 执行',
    test_identity VARCHAR(700) NOT NULL COMMENT 'source:suite::name 稳定身份，超长时附加 SHA-256',
    suite VARCHAR(500) NOT NULL COMMENT '归一化测试 Suite',
    name VARCHAR(500) NOT NULL COMMENT '归一化测试名称',
    source ENUM('REPOSITORY','UPLOADED','AI_GENERATED') NOT NULL COMMENT '测试来源',
    status ENUM('PASSED','FAILED','SKIPPED','ERROR') NOT NULL COMMENT '归一化测试状态',
    duration_ms BIGINT NOT NULL COMMENT '测试耗时毫秒',
    failure_type VARCHAR(255) NULL COMMENT '异常或断言类型',
    message TEXT NULL COMMENT '有界失败消息',
    stack_trace_summary TEXT NULL COMMENT '有界堆栈摘要',
    PRIMARY KEY (id),
    UNIQUE KEY uk_execution_test_identity (execution_id, test_identity),
    CONSTRAINT fk_validation_test_execution FOREIGN KEY (execution_id) REFERENCES validation_executions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='统一 Validation TestCaseResult';

-- Comparator 输出；最终分类只来自真实 Base/Candidate Runner 结果。
CREATE TABLE IF NOT EXISTS validation_regressions (
    id CHAR(32) NOT NULL COMMENT '差分结果唯一标识',
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    test_identity VARCHAR(700) NOT NULL COMMENT '对应统一测试身份或 BUILD/COMPARISON',
    suite VARCHAR(500) NOT NULL COMMENT '测试 Suite',
    name VARCHAR(500) NOT NULL COMMENT '测试或构建名称',
    source ENUM('REPOSITORY','UPLOADED','AI_GENERATED') NOT NULL COMMENT '测试来源',
    classification VARCHAR(60) NOT NULL COMMENT 'NEW_REGRESSION、EXISTING_FAILURE、POSSIBLE_FIX 等',
    confidence ENUM('HIGH','MEDIUM','LOW','INCONCLUSIVE') NOT NULL COMMENT '差分结论置信度',
    base_status VARCHAR(20) NULL COMMENT 'Base 归一化状态',
    candidate_status VARCHAR(20) NULL COMMENT 'Candidate 归一化状态',
    failure_type VARCHAR(255) NULL COMMENT 'Candidate 失败类型',
    message TEXT NULL COMMENT '有界失败说明',
    stack_trace_summary TEXT NULL COMMENT '有界堆栈摘要',
    related_changes_json TEXT NOT NULL COMMENT '关联 Git changed files JSON',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_regression_identity (validation_id, test_identity),
    CONSTRAINT fk_validation_regression_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Base/Candidate Comparator 结构化结果';

-- Git、Build、Test、AI Test、Runner 与 Code State 证据。
CREATE TABLE IF NOT EXISTS validation_evidence (
    id CHAR(64) NOT NULL COMMENT 'Validation Evidence 稳定标识',
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    source_type VARCHAR(40) NOT NULL COMMENT 'GIT_DIFF、BUILD、TEST、AI_TEST、RUNNER 或 CODE_STATE',
    title VARCHAR(255) NOT NULL COMMENT '证据标题',
    summary TEXT NOT NULL COMMENT '列表可用的有界摘要',
    structured_data_json LONGTEXT NOT NULL COMMENT '结构化数据 JSON',
    raw_artifact_reference VARCHAR(1000) NULL COMMENT '完整输出的服务端 Artifact 引用',
    timestamp VARCHAR(40) NOT NULL COMMENT '证据产生时间',
    PRIMARY KEY (validation_id, id),
    INDEX idx_validation_evidence_source (validation_id, source_type),
    CONSTRAINT fk_validation_evidence_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Pre-Merge Validation Evidence Store';

-- JSON 上传测试；经过路径、扩展名、数量和大小校验后才可写入 Runner。
CREATE TABLE IF NOT EXISTS validation_uploaded_tests (
    id CHAR(32) NOT NULL COMMENT '上传文件唯一标识',
    validation_id CHAR(32) NOT NULL COMMENT '绑定 ValidationRun',
    user_id CHAR(32) NOT NULL COMMENT '绑定上传用户，阻止跨用户复用',
    path VARCHAR(500) NOT NULL COMMENT '允许测试目录内的规范相对路径',
    content LONGTEXT NOT NULL COMMENT 'UTF-8 文本测试内容',
    content_sha256 CHAR(64) NOT NULL COMMENT '文件内容哈希',
    size_bytes INT NOT NULL COMMENT 'UTF-8 字节数',
    created_at VARCHAR(40) NOT NULL COMMENT '上传时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_uploaded_path (validation_id, path),
    CONSTRAINT fk_validation_upload_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_validation_upload_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='经过安全校验的额外测试文件';

-- AI 只生成候选测试；valid 标识和 Runner 结果决定其是否可用于比较。
CREATE TABLE IF NOT EXISTS validation_generated_tests (
    id CHAR(32) NOT NULL COMMENT 'AI 测试唯一标识',
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    target_file VARCHAR(500) NOT NULL COMMENT '受 allow-list 约束的 test source 路径',
    test_name VARCHAR(240) NOT NULL COMMENT '生成测试名称',
    test_code LONGTEXT NOT NULL COMMENT '候选测试源码，仅存在 Validation Workspace',
    reason TEXT NOT NULL COMMENT '测试目标说明',
    covered_change TEXT NOT NULL COMMENT '覆盖的 Diff 或 symbol',
    valid BOOLEAN NOT NULL COMMENT '语法与路径预校验是否通过',
    validation_error TEXT NULL COMMENT 'AI_TEST_INVALID 原因',
    created_at VARCHAR(40) NOT NULL COMMENT '生成时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_generated_path (validation_id, target_file),
    CONSTRAINT fk_validation_generated_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='受限 AI Generated Tests';

-- 持久化 SSE 事件，支持页面刷新与 Last-Event-ID 续传。
CREATE TABLE IF NOT EXISTS validation_events (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '全局单调递增事件标识',
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    event_type VARCHAR(80) NOT NULL COMMENT 'validation.preparing、execution.completed 等事件类型',
    event_key VARCHAR(255) NULL COMMENT '相同逻辑事件幂等键',
    data_json LONGTEXT NOT NULL COMMENT '事件负载 JSON',
    created_at VARCHAR(40) NOT NULL COMMENT '事件时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_event_key (validation_id, event_key),
    INDEX idx_validation_events (validation_id, id),
    CONSTRAINT fk_validation_event_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Validation SSE 领域事件';

-- 用户可复用的测试集元数据；文件内容保存在不可变版本表中。
CREATE TABLE IF NOT EXISTS validation_test_suites (
    id CHAR(32) NOT NULL COMMENT '持久化测试集唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '测试集所属用户，阻止跨用户读取或引用',
    repository VARCHAR(120) NOT NULL COMMENT '绑定的授权 RepositoryRegistry 名称',
    name VARCHAR(160) NOT NULL COMMENT '用户可读测试集名称',
    description TEXT NOT NULL COMMENT '测试目标、适用范围和维护说明',
    project_type ENUM('MAVEN','PYTHON') NOT NULL COMMENT '决定测试文件路径与扩展名白名单',
    latest_version INT NOT NULL DEFAULT 1 COMMENT '最新不可变版本号',
    archived BOOLEAN NOT NULL DEFAULT FALSE COMMENT '软归档标识；历史 Validation 引用仍可读取',
    created_at VARCHAR(40) NOT NULL COMMENT '创建时间，ISO 8601',
    updated_at VARCHAR(40) NOT NULL COMMENT '最近元数据或版本更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_test_suite_name (user_id, repository, name),
    INDEX idx_validation_test_suite_user (user_id, repository, archived, updated_at),
    CONSTRAINT fk_validation_test_suite_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户持久化管理的 Validation Test Suite';

-- 测试集的不可变版本；编辑文件会追加版本而不是覆盖旧版本。
CREATE TABLE IF NOT EXISTS validation_test_suite_versions (
    id CHAR(32) NOT NULL COMMENT '测试集版本唯一标识',
    suite_id CHAR(32) NOT NULL COMMENT '所属持久化测试集',
    version_number INT NOT NULL COMMENT '从 1 开始单调递增的版本号',
    change_note VARCHAR(1000) NOT NULL COMMENT '本次版本变更说明',
    content_hash CHAR(64) NOT NULL COMMENT '规范路径和文件内容形成的稳定 SHA-256',
    created_at VARCHAR(40) NOT NULL COMMENT '版本创建时间，ISO 8601',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_test_suite_version (suite_id, version_number),
    CONSTRAINT fk_validation_test_suite_version_suite FOREIGN KEY (suite_id) REFERENCES validation_test_suites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='不可变 Validation Test Suite Version';

-- 一个测试集版本中的受控 UTF-8 测试源码。
CREATE TABLE IF NOT EXISTS validation_test_suite_files (
    id CHAR(32) NOT NULL COMMENT '版本文件唯一标识',
    version_id CHAR(32) NOT NULL COMMENT '所属不可变测试集版本',
    path VARCHAR(500) NOT NULL COMMENT '测试目录白名单内的规范相对路径',
    content LONGTEXT NOT NULL COMMENT '经过安全校验的 UTF-8 测试源码',
    content_sha256 CHAR(64) NOT NULL COMMENT '单文件内容 SHA-256',
    size_bytes INT NOT NULL COMMENT 'UTF-8 文件大小',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_test_suite_file (version_id, path),
    CONSTRAINT fk_validation_test_suite_file_version FOREIGN KEY (version_id) REFERENCES validation_test_suite_versions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='持久化测试集版本文件';

-- Validation 创建时冻结所引用的测试集版本，后续新版本不会改变历史任务。
CREATE TABLE IF NOT EXISTS validation_run_test_suite_versions (
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    version_id CHAR(32) NOT NULL COMMENT '创建时选择的不可变测试集版本',
    sequence_no INT NOT NULL COMMENT '多个测试集的稳定合并顺序',
    PRIMARY KEY (validation_id, version_id),
    UNIQUE KEY uk_validation_run_suite_sequence (validation_id, sequence_no),
    CONSTRAINT fk_validation_run_suite_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_validation_run_suite_version FOREIGN KEY (version_id) REFERENCES validation_test_suite_versions(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Validation 与冻结测试集版本关联';

-- 用户为 AI Test Generator 显式选择的 Candidate Commit 参考测试路径。
CREATE TABLE IF NOT EXISTS validation_ai_reference_samples (
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    path VARCHAR(500) NOT NULL COMMENT '冻结 Candidate Commit 中的参考测试相对路径',
    sequence_no INT NOT NULL COMMENT '用户选择顺序',
    PRIMARY KEY (validation_id, path),
    UNIQUE KEY uk_validation_ai_reference_sequence (validation_id, sequence_no),
    CONSTRAINT fk_validation_ai_reference_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 生成测试的显式参考样本';

-- 创建请求完整指纹，覆盖测试集版本与 AI 样本，防止幂等 Key 被不同配置复用。
CREATE TABLE IF NOT EXISTS validation_request_configs (
    validation_id CHAR(32) NOT NULL COMMENT '所属 ValidationRun',
    request_fingerprint CHAR(64) NOT NULL COMMENT '规范化完整创建配置 SHA-256',
    created_at VARCHAR(40) NOT NULL COMMENT '首次记录时间，ISO 8601',
    PRIMARY KEY (validation_id),
    CONSTRAINT fk_validation_request_config_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Validation 幂等请求完整配置';

-- Test Suite 对应的服务模块和接口；一个 Suite 专注一个接口，版本继承该目标。
CREATE TABLE IF NOT EXISTS validation_test_suite_targets (
    suite_id CHAR(32) NOT NULL COMMENT '所属持久化 Test Suite',
    interface_id CHAR(64) NOT NULL COMMENT 'Repository+源码+symbol+method+route 的稳定接口标识',
    module_name VARCHAR(160) NOT NULL COMMENT '服务内部模块名称，例如 payment.controller',
    module_path VARCHAR(500) NOT NULL COMMENT '模块相对路径',
    interface_name VARCHAR(240) NOT NULL COMMENT '接口或处理函数名称',
    http_method VARCHAR(20) NOT NULL COMMENT 'HTTP 方法；非 HTTP 函数使用 FUNCTION',
    route_path VARCHAR(500) NOT NULL COMMENT 'HTTP Route；非 HTTP 函数可为空',
    source_file VARCHAR(500) NOT NULL COMMENT '生产代码相对路径，仅作为只读定位信息',
    symbol VARCHAR(240) NOT NULL COMMENT 'Controller method 或 Python function symbol',
    created_at VARCHAR(40) NOT NULL COMMENT '目标首次绑定时间，ISO 8601',
    PRIMARY KEY (suite_id),
    INDEX idx_validation_suite_target_interface (interface_id),
    CONSTRAINT fk_validation_suite_target_suite FOREIGN KEY (suite_id) REFERENCES validation_test_suites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Test Suite 的模块与接口归属';

-- 接口级一键生成的幂等操作记录；同一个用户的同一个 Key 只能生成一次版本和回归任务。
CREATE TABLE IF NOT EXISTS validation_interface_generation_requests (
    id CHAR(32) NOT NULL COMMENT '一键生成操作唯一标识',
    user_id CHAR(32) NOT NULL COMMENT '发起用户，用于租户隔离',
    idempotency_key VARCHAR(128) NOT NULL COMMENT '客户端幂等键',
    request_fingerprint CHAR(64) NOT NULL COMMENT 'Repository、冻结 SHA、接口与配置的请求摘要',
    status VARCHAR(20) NOT NULL COMMENT 'PROCESSING、COMPLETED 或 FAILED',
    suite_id CHAR(32) NULL COMMENT '创建或更新的 Test Suite',
    version_id CHAR(32) NULL COMMENT '本次创建的不可变版本',
    validation_id CHAR(32) NULL COMMENT '本次自动触发的回归 Validation',
    action VARCHAR(20) NULL COMMENT 'CREATED 或 VERSIONED',
    error_message VARCHAR(2000) NULL COMMENT '失败摘要，不包含密钥或完整源码',
    created_at VARCHAR(40) NOT NULL COMMENT '创建时间，UTC ISO-8601',
    updated_at VARCHAR(40) NOT NULL COMMENT '最近更新时间，UTC ISO-8601',
    PRIMARY KEY (id),
    UNIQUE KEY uk_validation_interface_generation_idempotency (user_id,idempotency_key),
    KEY idx_validation_interface_generation_validation (validation_id),
    CONSTRAINT fk_validation_interface_generation_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_validation_interface_generation_suite FOREIGN KEY (suite_id) REFERENCES validation_test_suites(id),
    CONSTRAINT fk_validation_interface_generation_version FOREIGN KEY (version_id) REFERENCES validation_test_suite_versions(id),
    CONSTRAINT fk_validation_interface_generation_run FOREIGN KEY (validation_id) REFERENCES validation_runs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='接口级测试生成与自动回归幂等记录';
