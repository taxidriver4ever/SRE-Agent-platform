-- Code State 模块：代码仓库状态快照。
CREATE TABLE IF NOT EXISTS code_state_repositories (
    repository VARCHAR(128) NOT NULL COMMENT '仓库业务标识',
    repository_url VARCHAR(1024) NULL COMMENT '远程仓库地址',
    commit_sha VARCHAR(64) NOT NULL COMMENT '当前状态对应的 Git Commit SHA',
    directory_summary_json LONGTEXT NOT NULL COMMENT '目录结构摘要 JSON',
    updated_at VARCHAR(40) NOT NULL COMMENT '仓库状态更新时间，ISO 8601 格式',
    PRIMARY KEY (repository)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='代码仓库导航状态';

ALTER TABLE code_state_repositories COMMENT = '代码仓库导航状态';

-- Code State 模块：代码组件和符号导航信息。
CREATE TABLE IF NOT EXISTS code_state_components (
    id CHAR(32) NOT NULL COMMENT '代码组件唯一标识',
    repository VARCHAR(128) NOT NULL COMMENT '组件所属仓库业务标识',
    commit_sha VARCHAR(64) NOT NULL COMMENT '组件状态对应的 Git Commit SHA',
    module VARCHAR(255) NOT NULL COMMENT '组件所属业务模块',
    kind VARCHAR(40) NOT NULL COMMENT '组件或符号类型',
    symbol VARCHAR(128) NOT NULL COMMENT '代码符号名称',
    path VARCHAR(384) NOT NULL COMMENT '代码文件相对路径',
    role TEXT NOT NULL COMMENT '组件职责说明',
    relationships_json LONGTEXT NOT NULL COMMENT '组件依赖关系 JSON',
    start_line INT NULL COMMENT '符号起始行号',
    end_line INT NULL COMMENT '符号结束行号',
    updated_at VARCHAR(40) NOT NULL COMMENT '组件状态更新时间，ISO 8601 格式',
    PRIMARY KEY (id),
    UNIQUE KEY uq_code_state_component (repository, path, symbol),
    INDEX idx_code_state_navigation (repository, commit_sha, kind, path, symbol)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='代码组件、符号与依赖导航信息';

ALTER TABLE code_state_components COMMENT = '代码组件、符号与依赖导航信息';
