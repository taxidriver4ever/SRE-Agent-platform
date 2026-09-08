# SRE Agent Backend

单 Agent 后端通过统一 `GatewayLLM` 连接 `sre-gateway → Docker vLLM`，实际模型由请求数据指定；迁移期仍可显式切回 Ollama。项目自有工具使用 FastMCP 3.4.5；Kubernetes 改为维护活跃的 `containers/kubernetes-mcp-server`，以只读、单集群、core 工具集模式直接调用 Kubernetes API。

## 模块

- `app/llm/`：Provider 无关协议与 Gateway Client，Agent 不使用厂商 SDK。
- `app/intent/`：LLM Structured Output 意图分类与工作流闸门；合法意图确认前禁止进入工具 Runtime。
- `app/security/`：项目级 Tool Policy、严格参数 Schema 与服务端 Task Scope。
- `app/audit/`：按 user/project/task 记录的 MySQL Tool Audit Log。
- `app/sandbox/`：一次性 Task Workspace 与未来 CodeExecuteTool 的 Docker 隔离层。
- `app/agent/`：通用 JSON-ReAct Runtime，不再保留 calculator/current_time 等凑框架工具。
- `app/mcp_servers/`：项目自有 FastMCP Server，只注册 Prometheus/Loki/Tempo/MySQL 与 Git 只读工具。
- `app/mcp_clients/`：FastMCP 官方 Client 聚合层，以及第三方 Kubernetes MCP 的只读语义适配；Client 不放在 tools 中。
- `app/repositories/`：从 K8s `repository-url` 注解建立模块→远程仓库绑定，并按运行 SHA 创建浅克隆缓存。
- `app/auth/`：PBKDF2 密码登录、随机 Bearer Token、数据库验证与注销撤销。
- `app/conversation/`：按用户隔离的 Conversation/Message MySQL 持久化与历史缓存 API。
- `app/conversation_memory/`：80% 预算压缩、短 State、MySQL Repository、请求 Scope 与固定表只读检索工具。
- `app/evidence/`：Tool Result 的外部来源引用模型；完整原文仍属于 Conversation Message。
- `app/code_state/`：Git 仓库导航 State、首次有限扫描、按 commit diff 增量更新与固定表只读检索。
- `app/workflow/`：八阶段硬性工作流、专项策略、证据门槛与统一报告模型。
- `app/api/`：保留旧 `/v1/agent/run` 与 `/api/agent/chat` 兼容接口。
- `app/diagnosis/`：Diagnosis Session、Step、Evidence Store、Incident Graph、Root Cause、Repository、Orchestrator 与 SSE API。
- `app/validation/`：独立 ValidationRun、模块/接口级 Test Suite 与不可变版本、FastAPI/Spring 接口发现、一键 AI 生成/更新并回归、显式 AI 参考样本、冻结 Commit、Maven/Python Adapter、Ephemeral Docker Runner、JUnit 归一化、确定性 Comparator、持久事件和 Diagnosis Evidence 联动。
- `app/resources/`：后端 Service Catalog 与只读 Kubernetes Pod 浏览接口。
- `skills/`：12 个独立 SRE Skill，覆盖语言运行时、Kubernetes、数据库、依赖、发布、Tracing 与证据综合。
- `evals/`：SRE-001～010 评测数据与 Runner。

## 配置

```text
GATEWAY_BASE_URL=http://127.0.0.1:8000
GATEWAY_API_KEY=gw_sk_...                 # 只保存 Gateway Token，不是 Provider Key
GATEWAY_MODEL=vllm/qwen3-4b
GATEWAY_MAX_TOKENS=512
PROMETHEUS_BASE_URL=http://127.0.0.1:19090
LOKI_BASE_URL=http://127.0.0.1:13100
TEMPO_BASE_URL=http://127.0.0.1:13200
MYSQL_HOST=127.0.0.1
MYSQL_PORT=13307
MYSQL_USER=sre_reader
MYSQL_PASSWORD=sre_reader_dev_only
SRE_REPOSITORY_PATH=D:\SRE-Agent-platform\sre-broken-system
SERVICE_CATALOG_PATH=D:\SRE-Agent-platform\sre-broken-system\sre-lab-infra\service-catalog.yaml
TOOL_TIMEOUT_SECONDS=15
DIAGNOSIS_DEADLINE_SECONDS=240
DIAGNOSIS_MAX_ATTEMPTS=3
DIAGNOSIS_LEASE_TTL_SECONDS=60
DIAGNOSIS_HEARTBEAT_INTERVAL_SECONDS=10
TOOL_OUTPUT_LIMIT=12000
SRE_VALIDATION_MAVEN_IMAGE=maven:3.9.9-eclipse-temurin-21
SRE_VALIDATION_PYTHON_IMAGE=sre-validation-python:3.12
SRE_VALIDATION_CPUS=1.0
SRE_VALIDATION_MEMORY_MB=1024
SRE_VALIDATION_PIDS_LIMIT=128
SRE_VALIDATION_PREPARE_TIMEOUT_SECONDS=60
SRE_VALIDATION_BUILD_TIMEOUT_SECONDS=300
SRE_VALIDATION_TEST_TIMEOUT_SECONDS=600
SRE_VALIDATION_OVERALL_TIMEOUT_SECONDS=900
SRE_VALIDATION_ALLOW_BUILD_NETWORK=false
KUBERNETES_MCP_VERSION=0.0.65
SRE_REPOSITORY_CACHE_PATH=D:\SRE-Agent-platform\.cache\sre-agent-repositories
SRE_REPOSITORY_ALLOWED_HOSTS=github.com,gitlab.com,bitbucket.org
MODEL_CONTEXT_WINDOW=32768
CONTEXT_COMPACTION_RATIO=0.80
CONTEXT_RESERVED_OUTPUT_TOKENS=4096
APPLICATION_MYSQL_HOST=127.0.0.1
APPLICATION_MYSQL_PORT=13308
APPLICATION_MYSQL_USER=sre_agent
APPLICATION_MYSQL_DATABASE=sre_agent
AUTH_TOKEN_TTL_HOURS=24
SRE_DEFAULT_PROJECT_ID=sre-lab
SRE_TOOL_POLICY_PATH=D:\SRE-Agent-platform\sre-agent-backend\sre-agent\config\tool-policy.yaml
PROMETHEUS_BEARER_TOKEN=                   # 可选，仅保留在后端
LOKI_BEARER_TOKEN=                         # 可选，仅保留在后端
SRE_SANDBOX_WORKSPACE_ROOT=D:\SRE-Agent-platform\sre-agent-backend\sre-agent\.sandbox-tasks
SRE_SANDBOX_IMAGE=python:3.12-alpine
SRE_SANDBOX_CPUS=1.0
SRE_SANDBOX_MEMORY_MB=512
SRE_SANDBOX_PIDS_LIMIT=128
SRE_SANDBOX_TIMEOUT_SECONDS=120
```

本地登录用户名和密码只保存在未提交的 `.env` 中，不写入本配置示例或文档。

业务表不集中在 Core 层，各模块分别拥有自己的 MySQL 建表语句：

- `app/auth/sql/schema.sql`：`users`、`auth_tokens`
- `app/conversation/sql/schema.sql`：`conversations`、`conversation_messages`
- `app/conversation_memory/sql/schema.sql`：`conversation_compactions`、`conversation_memory_items`
- `app/code_state/sql/schema.sql`：`code_state_repositories`、`code_state_components`
- `app/audit/sql/schema.sql`：`tool_audit_logs`
- `app/diagnosis/sql/schema.sql`：Session、Step、Evidence、Graph、Root Cause、Event 与 migration registry
- `app/diagnosis/sql/001_durable_execution.sql`：Checkpoint、Lease、CAS、原子 Step Sequence 与 event key
- `app/diagnosis/sql/002_tool_parent_evidence.sql`：RUNNING Tool 恢复需要的父 Evidence 血缘

每份 SQL 都包含字段级 `COMMENT` 和表级 `COMMENT`。各模块的 `schema.py` 只负责定位并执行本模块 SQL；`app/core/database.py` 只负责通用 MySQL 连接、事务和 SQL 文件执行，不依赖任何业务表。

## 启动

MySQL 数据统一持久化到后端根目录的 `data/mysql`，不在 Agent 或 Gateway 模块内生成数据库目录。

```powershell
# 后端统一使用根目录 compose.yml；表由各业务模块启动时初始化。
Set-Location D:\SRE-Agent-platform\sre-agent-backend
docker-compose -f compose.yml up -d mysql

# Compose 只启动基础设施；Gateway 保持本地 Python 进程运行。
docker-compose -f compose.yml up -d vllm

Set-Location D:\SRE-Agent-platform\sre-agent-backend\sre-agent
$env:GATEWAY_API_KEY = "从 POST /v1/auth/tokens 获得的 Token"
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## API

```powershell
$credentials = Get-Content .env -Raw | ConvertFrom-StringData
$login = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/login `
  -ContentType application/json `
  -Body (@{
    username=$credentials.SRE_INITIAL_USERNAME
    password=$credentials.SRE_INITIAL_PASSWORD
  } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$body = @{ message = "为什么订单模块最近这么慢？"; conversation_id = $null; project_id = "sre-lab" } | ConvertTo-Json
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/agent/chat `
  -Headers $headers -ContentType application/json -Body $body
```

`/api/agent/chat/stream` 返回 SSE `intent`、`phase`、`tool`、`message`、`final` 事件。前端可展示公开调查步骤、工具参数与摘要，但不展示隐藏 Chain-of-Thought。

新的 SRE Console 使用统一的 Incident/Diagnosis Session API：

- `POST /api/diagnoses`：使用 `QUESTION`、`SERVICE` 或 `POD` 创建独立 Session。
- `GET /api/diagnoses`、`GET /api/diagnoses/{id}`：读取历史和完整聚合。
- `GET /api/diagnoses/{id}/steps`：公开、可审计的 Investigation Timeline。
- `GET /api/diagnoses/{id}/evidence`：Evidence Store 的摘要、结构化数据与原始结果引用。
- `GET /api/diagnoses/{id}/graph`：由后端生成的跨服务 Incident Graph。
- `GET /api/diagnoses/{id}/root-cause`：结构化根因、置信度与建议。
- `GET /api/diagnoses/{id}/events`：支持 `Last-Event-ID` 回放的持久化 SSE 事件流。
- `GET /api/system/self-check?level=1|2|3`：登录后执行只读 Diagnosis Runtime 自检。
- `GET /api/services`、`GET /api/services/{name}/pods`、`GET /api/pods/{name}`：服务目录和真实只读 Kubernetes 资源浏览。
- `GET /api/repositories/{repository}/branches`：刷新受信任远程并返回真实 Branch。
- `GET /api/repositories/{repository}/test-files?ref=...`：列出 Candidate Commit 中可作为 AI 样本的测试文件。
- `POST/GET /api/validation-test-suites`：创建或查询当前用户的持久化测试集。
- `GET /api/validation-test-suites/{id}`：读取全部不可变版本及文件。
- `POST /api/validation-test-suites/{id}/versions`：追加完整测试文件快照作为新版本。
- `POST /api/validation-test-suites/{id}/metadata`、`.../archive`：修改元数据或软归档。
- `POST/GET /api/validations`：创建、查询 Base/Candidate 合并前验证。
- `GET /api/validations/{id}`、`.../events`：读取执行、测试集、AI 测试、比较结果和可恢复事件。
- `POST /api/validations/{id}/diagnose`、`.../cancel`：诊断确认回归或取消活动任务。

Session 业务状态机为 `PENDING -> INVESTIGATING -> COMPLETED`，会话级异常进入 `FAILED`，明确业务取消进入 `CANCELLED`。单一 Tool 失败只产生 `FAILED` Step，Orchestrator 会继续尝试其余证据源。Workflow 阶段由独立 `current_phase / phase_status` 表示，不扩张业务状态枚举。

## Durable Diagnosis / Crash Recovery

### Source of Truth 与职责

持久 Diagnosis 的事实来源是 Application MySQL，不是 `asyncio.Task`。进程内 Task 只负责当前一次物理执行；它可以因 `kill -9`、OOM、容器替换或主机重启消失，而数据库仍保存 logical Diagnosis。

| 组件 | Durable Execution 职责 |
| --- | --- |
| `DiagnosisRepository` | 单 logical operation 的事务、CAS、Lease、Step、Evidence、Checkpoint 和 Event |
| `DiagnosisExecutionManager` | 生成 executor_id、claim、Heartbeat、启动 Task、startup recovery、graceful interrupt |
| `DurableWorkflowRuntime` | 把 Phase/Tool hook 映射到 Repository，并在恢复时提供缓存结果或 RUNNING Tool |
| `DiagnosisOrchestrator` | Workflow 报告投影为 Root Cause、Graph、REPORT Step 和 Session 完成 |
| `DiagnosisWorkflow` | 接受 `resume_state`，按持久 Phase 游标跳过已完成阶段，继续使用已有 Evidence/Timeline |
| `NoopWorkflowRuntime` | 保持 Quick Diagnosis 无 Session、无 Checkpoint、无记忆的原行为 |

```text
HTTP 202
  ↓
diagnosis_sessions: PENDING
  ↓ atomic affected_rows == 1
Executor claim (lease_owner / lease_expires_at / attempt_no)
  ↓
INVESTIGATING
  ├─ Phase RUNNING     → checkpoint
  ├─ Tool RUNNING      → stable logical Step
  ├─ Tool completion   → Step + Evidence + Checkpoint + Event（同一事务）
  └─ Phase COMPLETED   → checkpoint
  ↓ process crash
Lease expires
  ↓ startup scan + CAS claim
load DiagnosisState.model_validate_json(...)
  ↓
resume original run_id
  ↓
idempotent REPORT → COMPLETED
```

### 数据字段与双层状态机

`diagnosis_sessions.status` 保留 `PENDING / INVESTIGATING / COMPLETED / FAILED / CANCELLED`，其中后三者不参与恢复。执行游标另存为：

| 字段 | 含义 |
| --- | --- |
| `current_phase` | 最近开始或完成的 Workflow Phase |
| `phase_status` | 当前 Phase 的 `PENDING / RUNNING / COMPLETED` |
| `checkpoint_json` | `DiagnosisState.model_dump(mode="json")` 的完整可恢复快照 |
| `checkpoint_seq` | 每次 Checkpoint 单调递增的计数 |
| `attempt_no` | 同一 logical Diagnosis 被物理 Executor claim 的次数 |
| `heartbeat_at` | 当前 Executor 最近一次存活更新时间 |
| `lease_owner / lease_expires_at` | 执行所有权和到期时间 |
| `state_version` | 乐观锁版本，防止旧 Executor 覆盖新状态 |
| `next_step_sequence` | 并发安全分配 Investigation Timeline 序号 |
| `interrupted_at / recovery_reason` | 进程中断和最近恢复原因 |

Checkpoint 沿用现有 `DiagnosisState`，包括 query、conversation、user、run_id、service、symptom、Pod、runtime commit、repository、dependencies、Evidence、Candidates、Timeline、Phases、Synthesis 和 Token Usage。它只保存可公开验证的调查状态，不保存隐藏 Chain-of-Thought。同一 Diagnosis 恢复时 `run_id` 不变，只有 `attempt_no` 从 1 增至 2、3，用于区分物理执行尝试。

### Checkpoint 时机与 Resume 规则

Checkpoint 在 Phase 开始、Phase 完成、每个 Tool 完成、Evidence 更新、Planner 调查轮完成，以及 Pod/commit 等关键 Runtime State 更新后保存。每个 Phase 的写法是：

```text
current_phase = TRIAGE, phase_status = RUNNING
  → triage
  → save DiagnosisState
current_phase = TRIAGE, phase_status = COMPLETED
```

若恢复记录是 `TRIAGE / COMPLETED`，Workflow 跳过 START 和 TRIAGE，从 BASELINE 开始。若是 `INVESTIGATE / RUNNING`，则直接带着原 Evidence 和 Timeline 进入 Investigation。已完成 Tool 的数据库结果会回填为 Workflow Evidence/Timeline；中断时仍为 RUNNING 的 Tool 会先于下一次 Planner 决策恢复，避免依赖模型碰巧生成相同决定。

### Tool、Evidence、Conversation 和 Event 幂等

Tool logical key 由 `phase + tool_name + canonical(arguments) + sorted(parent_evidence_ids)` 计算 SHA-256，其中参数严格使用：

```python
json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

`diagnosis_investigation_steps` 对 `(diagnosis_id, idempotency_key)` 建唯一索引，并持久化 `arguments_json`、`parent_evidence_ids_json`、`result_json`、稳定 `evidence_id`、Step `attempt_no` 和 `updated_at`。执行流程为：

```text
calculate logical key
  → SELECT existing Step FOR UPDATE
  → COMPLETED: load result/Evidence，不调用真实 Tool
  → missing: INSERT RUNNING
  → stale RUNNING: UPDATE 同一行，attempt_no + 1
  → execute read-only Tool
  → atomic commit Step + Evidence + Checkpoint + Event
```

Evidence ID 由 `diagnosis_id + logical key` 派生并保持稳定。Conversation 的 `tool_call`、`tool_result` 和 final report 使用稳定 Message ID 和 MySQL UPSERT。重要 Event 使用 `(diagnosis_id, event_key)` 唯一键，例如 `phase.completed:TRIAGE`、`step.completed:<key>` 和 `diagnosis.completed`；Recovery attempt 使用 `diagnosis.recovered:<attempt>`，因此每次物理恢复仍可审计。

Baseline 的并发 Step 序号不再查询 `MAX(sequence_no) + 1`。Repository 锁定 Session 行，读取并递增 `next_step_sequence`，保证同一 Diagnosis 内序号唯一且单调。

### Lease、Heartbeat、CAS 与进程生命周期

进程级 `executor_id` 由 hostname、PID 和随机 UUID 组成。Claim 只接受 `PENDING`，或没有有效 Lease 的 `INVESTIGATING`；只有 UPDATE 的 `rowcount == 1` 才创建 Executor Task。默认 Lease TTL 60 秒、Heartbeat 间隔 10 秒，避免 20 秒级 Tool 调用被误判死亡。

Checkpoint、Heartbeat、Tool begin/complete 和 Session completion 都以 `lease_owner + state_version` 为 CAS 条件。CAS 失败表示当前 Executor 已失去 ownership，必须立即停止写入，不能覆盖新进程保存的状态。Heartbeat 只更新 Session，不写高频 SSE Event。

Graceful shutdown 会取消 Task、写 `diagnosis.interrupted`、清空 Lease 并保持 `INVESTIGATING`。这不是用户取消；`CANCELLED` 只用于明确的业务动作。应用启动顺序为：模块 Schema/Migration → Level 1 Self-Check → stale scan → atomic claim → load checkpoint → recovery Task。达到 `DIAGNOSIS_MAX_ATTEMPTS` 后进入 `FAILED` 并记录 `maximum recovery attempts exceeded`。

### 最终报告和 Crash Window

REPORT 重放是安全的：Evidence 与 Root Cause UPSERT，Graph 以稳定 edge ID 重建，REPORT Step 使用固定 `final-report` key，completed Event 使用固定 key，final Conversation Message 使用 run_id 派生 ID，Session completion 对同一 run_id 可重复确认。

外部只读 Tool 无法承诺 exactly-once。如果 Tool 已返回但进程在 MySQL commit 前消失，新 Executor 无法知道外部调用是否发生，允许再调用一次。因此准确保证是：

```text
External Tool                 at-least-once in crash window
Logical Step/Evidence/Event   idempotent / exactly-once effect
```

这一边界安全的前提是所有诊断 Tool 继续保持只读。系统没有引入 Celery、Kafka、RabbitMQ、Redis Queue，也没有创建跨整个 Diagnosis 的长事务。

## Diagnosis Runtime Self-Check

`GET /api/system/self-check` 默认运行 Level 3，可用 `?level=1` 或 `?level=2` 降低扫描范围。端点必须登录，并且只返回当前用户的 Diagnosis ID；启动时只运行不枚举用户数据的 basic Level 1。

| Level | 检查内容 |
| --- | --- |
| 1 Runtime Health | Application MySQL、必要列和索引、executor_id、Heartbeat 子系统是否可用 |
| 2 Durable State | 活跃 Session 的 Lease、Heartbeat、Checkpoint、run_id、Phase、版本计数和 attempt |
| 3 Data Consistency | Step/Evidence/Root Cause/Graph/Event 的引用与 terminal invariant |

自检实际用 `DiagnosisState.model_validate_json()` 反序列化 Checkpoint，而不是只判断非空。主要 invariant：

- INVESTIGATING 必须有可恢复 Checkpoint；有效运行应有未过期 Lease 和近期 Heartbeat；
- Checkpoint run_id 必须等于 Session run_id，current_phase 必须出现在 checkpoint phases；
- COMPLETED 必须处于 END，拥有 Root Cause、REPORT Step 和 completed Event；
- terminal Session 不能持 Lease，也不能残留 RUNNING Step；
- RUNNING Step + expired Lease 标记为可恢复的 `INTERRUPTED_RUNNING_STEP`；
- Step、Root Cause 引用的 Evidence 必须存在，Evidence logical key 必须能找到 Tool Step；
- Graph Edge 的 source/target Node 必须存在；
- checkpoint_seq/state_version 不能为负或倒退，attempt 不能越过配置上限；
- durable schema 必须具备 Checkpoint、Lease、idempotency 和 recovery 索引。

状态策略：Warning-only 返回 `DEGRADED`，例如 `STALE_LEASE`、`MISSING_ACTIVE_LEASE`、`LEASE_EXPIRING_SOON`；Error/Critical 返回 `UNHEALTHY`，例如 `INVALID_CHECKPOINT`、`TERMINAL_SESSION_HOLDS_LEASE`、`ORPHAN_RUNNING_STEP`、`DANGLING_GRAPH_EDGE`、`DURABLE_SCHEMA_INCOMPLETE`。无 issue 返回 `HEALTHY`。

Self-Check 始终 read-only，只发现问题；Recovery 才会 claim、增加 attempt、恢复或在达到上限时失败。它不调用 SRE Tool、LLM 或 Lab 数据库，不消耗 Token，也不替换 `/health`。快照之后发生的并发变化和“外部 Tool 完成但数据库未 commit”的瞬间无法由一次检查完全观察，最终由 Lease/CAS 与 logical idempotency 收敛。

每条请求先被分类为 `SPECIFIC_INCIDENT`、`GENERAL_DIAGNOSIS`、`NEED_CLARIFICATION` 或 `OUT_OF_SCOPE`。具体故障进入 Investigation Workflow；整体巡检先执行全局 System Scan；信息不足或非运维问题只返回普通消息，不允许调用 Kubernetes、Prometheus、Loki、Tempo、MySQL 或 Git 工具。

### Intent Schema

```json
{
  "intent": "SPECIFIC_INCIDENT",
  "target": "order-service",
  "symptom": "high_latency"
}
```

- `intent` 是严格枚举，禁止模型生成其他分类。
- `SPECIFIC_INCIDENT` 必须同时提供非空 `target` 和 `symptom`，否则 Schema 校验失败并要求模型改判或补齐。
- `NEED_CLARIFICATION`、`OUT_OF_SCOPE` 的 `target` 和 `symptom` 必须为 `null`。
- `target` 最终还会经过 Service Catalog 归一化；模型生成的任意服务名不会直接成为工具参数。

### Workflow Router

| Intent | 路径 | Tool 权限 |
| --- | --- | --- |
| `SPECIFIC_INCIDENT` | Intent → TRIAGE → Baseline → Investigation → Verify → Report | 允许只读工具 |
| `GENERAL_DIAGNOSIS` | Intent → `SYSTEM_SCAN` → TRIAGE → 全局 Baseline → Investigation → Report | 允许只读工具 |
| `NEED_CLARIFICATION` | Intent → 普通回复 | 禁止工具调用 |
| `OUT_OF_SCOPE` | Intent → 能力边界提示 | 禁止工具调用 |

`SYSTEM_SCAN` 会读取全局 Deployment、Pod、服务健康、按服务聚合的 5xx、Pod CPU/内存和全局错误日志。服务仍未知时保持 `unknown`，不会退回假定 `order-service`。

### SSE 事件

| 事件 | 内容 |
| --- | --- |
| `conversation` | 服务端确认的持久会话 ID |
| `intent` | 分类后的 `intent`、`target`、`symptom` |
| `phase` | `SYSTEM_SCAN`、`TRIAGE` 等公开工作流阶段 |
| `tool` | 只读工具名称、参数、耗时和结果摘要 |
| `message` | 澄清问题或能力边界普通回复 |
| `final` | 完整结构化诊断报告 |
| `error` | SSE 建立后的可读错误 |

## Structured Output 容错

模型决策和上下文压缩结果都必须通过 Pydantic Schema 校验。JSON 格式错误会先执行受限 JSON Repair 并重新校验；字段缺失或类型错误会把安全的校验信息反馈给模型。常规重试仍失败后，系统提供预设 JSON 模板，让模型基于原始输出重新填充。模板结果仍无效时，本轮决策返回结构化失败；压缩任务则放弃本次 State 更新并保留 Conversation Store 中的原始消息，不用错误 State 覆盖已有记忆。

该流程只修复结构，不改变工具权限，也不会执行模型输出中的任意代码或 SQL。

Intent Router 在模板回填仍失败时采用更严格的失败策略：保存首次原始模型输出用于排查，并返回 `NEED_CLARIFICATION`。因此结构错误不能绕过 Intent 闸门触发诊断工具。

最终报告中的每条证据包含 `evidence_id` 和 `source_references`。User/Assistant Message、Tool Call 和完整 Tool Result 全部永久写入 MySQL Conversation Store；正常阶段全部保留在 Active Context。活动上下文加预留输出达到模型窗口约 80% 后，模型生成短 Conversation Summary、短 Context State 和可检索 Memory Item，成功提交后旧消息退出 Active Context，但原始记录不删除。`evidence_id` 就是原始 Tool Result Message ID，可通过 `GET /api/agent/evidence/{run_id}/{evidence_id}` 在当前用户权限内回查。

### Evidence-driven Investigation

Workflow 只控制阶段、预算、Tool 调度、Evidence 持久化和终止条件，不包含服务专属根因、固定 SQL、固定 Trace ID 或 Eval Case 答案。Baseline 建立通用 Metrics、Logs、Pod 与健康视图；随后 Evidence Planner 只使用 Tool Result 中实际出现的关联字段推进：日志中的 `trace_id` → 精确 Trace，Trace/慢日志中的 SQL → `EXPLAIN`，Pod 重启 → Pod/Events/restart，运行 commit 与发布现象 → Git Diff。

所有成功 Tool Result 都会归一化为 `tool/status/summary/data/structured_data/references/next_hints`，Evidence 同时记录 `source_type`、短 `summary`、`parent_evidence_ids` 和原始 Tool Result 的 `evidence_id`。最终 Finding 必须列出有效 Evidence ID。Evidence Gate 会检查引用是否存在、是否包含直接运行时证据以及证据是否矛盾；不满足条件时报告状态为 `insufficient_evidence`，不会把模型猜测包装成根因。

历史 State/Evidence 只允许通过 `search_conversation_memory` MCP 工具读取。该工具的 SQL 和表名固定为 `conversation_memory_items`，用户与 Conversation 从服务端请求 Scope 注入；模型不能传入身份、会话、表名或原始 SQL，最多读取当前会话 20 条有效记忆。

## Code State 与精确源码读取

首次发现仓库时，系统只枚举目录、构建清单、配置文件和 Controller/Service/Repository 等关键入口，生成包含模块、职责、symbol、路径、行号和 commit SHA 的短导航 State；数据库不保存源码正文。排查时先用 `search_code_state` 在固定的 `code_state_components` 表中筛选组件，再通过 Git 工具按对应 commit、路径和 symbol 精确读取少量源码。代码原文由 Git 保存，不进入 Evidence Store。

仓库出现新 commit 后，系统执行 `git diff --name-status -M old..new`，仅重新处理新增或修改文件；删除文件会移除对应 State，重命名会迁移路径和 Reference，调用关系只在受影响组件内更新。若本地浅克隆已无法取得旧 commit，才对新 commit 执行一次有限的导航扫描。

前端启动顺序为：读取本地 Token → `GET /api/auth/me` 服务端校验 → `GET /api/conversations` 加载会话摘要缓存。诊断 SSE 的第一条 `conversation` 事件返回持久会话 ID，用户消息和最终结构化报告都会落入 `conversation_messages` 表。

标准 MCP Streamable HTTP 端点挂载在 `http://127.0.0.1:8001/mcp/`，只暴露项目的 Git/可观测性只读工具。Kubernetes MCP 是独立第三方 stdio Server，不伪装成项目自研 Server。

## Kubernetes 与远程仓库绑定

1. 可选地应用 `deploy/kubernetes-mcp-reader.yaml`，并为该只读 ServiceAccount 生成专用 kubeconfig。
2. 复制 `config/repository-bindings.example.yaml`，填入真实的 HTTPS Git remote；不要把 Token 写进 URL。
3. 由管理员手动执行 `python scripts/bind_kubernetes_repositories.py config/repository-bindings.yaml`。脚本会把 `sre.agent/repository-url` 同时写入 Deployment 和 Pod Template。
4. Agent 先通过 Kubernetes MCP 读取 `repository`、`repository-url`、`git-sha`，再由 Git FastMCP 对白名单主机执行精确 SHA 的浅抓取和源码读取。若当前实验仓库尚未配置 remote，则明确使用现有本地只读镜像，不伪造远程地址。

## Pre-Merge Validation 与测试集管理

Validation 创建时把 Base/Candidate Branch 解析并冻结为完整 40 位 Commit SHA。对于显式配置的白名单 HTTPS 远程仓库，Branch 查询会在 Repository 级异步锁内执行 `fetch --prune`；刷新失败直接返回错误。本地实验仓库没有远程绑定时只读取当前本地 Branch Ref。

测试输入可以组合：Repository Tests、临时 Uploaded Tests、多套持久化 Test Suite（每套选择一个 Version），以及最多 8 个 AI Generated Tests。Managed/Uploaded 文件合并后统一接受路径、扩展名、数量和字节数校验；同路径不同内容会拒绝，AI 文件不能覆盖用户文件。测试集编辑不会覆盖旧内容，而是追加 `v2/v3/...`，Validation 绑定具体 `version_id`，保证历史任务可复现。

每套 Test Suite 必须明确绑定 `Repository → Module → Interface`，并保存 HTTP Method、Route、源码文件和 Symbol。`interfaces.py` 只读取冻结 Git Object：Python 解析 FastAPI 装饰器与 `APIRouter` 前缀，Java 解析 Spring Mapping 与 Controller 前缀，再用稳定 `interface_id` 对比 Base/Candidate，输出 `ADDED/MODIFIED/REMOVED/UNCHANGED`。接口变更只表示源码差异，不会自动推断测试是否通过。

`POST /api/interface-test-suites/generate` 是“一键生成测试集并回归”的受控入口。新增接口会创建绑定目标的 `v1`；已有接口会读取最新测试快照，生成新增或更新测试源码，并追加不可变 `v2/v3/...`。随后服务自动创建只引用该新 `version_id` 的 Validation，所以 Base/Candidate 输入保持完全相同。旧版本不会覆盖，历史 Run 也不会跟随 Branch 或 Suite 的后续变化。

一键入口有独立的用户级 `Idempotency-Key` 记录：同一请求重试返回原 Suite/Version/Validation，不重复追加版本；处理中、失败、或 Key 被不同参数复用时明确报错。Suite 用户、Repository、项目类型、接口 ID、路径白名单、文件数量和大小都会在服务端重新校验。AI 仍只能输出测试源码，不具备修改生产代码、Git、CI、Merge、Push 或部署的能力。

AI 模式允许用户从冻结 Candidate Commit 中选择最多 10 个 `.py/.java` 测试文件作为 `representative_tests`。未选择时自动读取最多 3 个样本。模型只能生成候选测试源码；同一份有效生成文件会注入 Base 和 Candidate，最终分类仍只由真实 Docker Build/JUnit 结果决定。

Comparator 会在两侧都有规范化用例时优先逐测试比较，即使 Base 测试进程退出码非零也不会提前返回。因此 `Base Fail → Candidate Pass` 会稳定分类为 `POSSIBLE_FIX`；`Base Pass → Candidate Fail` 分类为 `NEW_REGRESSION` 或 `AI_CONFIRMED_REGRESSION`。只有无可比较用例、超时、Runner 失败或 Base 无法形成可靠基线时才进入 `COMPARISON_INCONCLUSIVE`。

Validation 详情返回 `test_suite_versions[]`、`ai_reference_test_paths[]`、`generated_tests[]`、`executions[].tests[]` 和 `regressions[]`。完整创建配置另存 SHA-256 指纹，同一个 `Idempotency-Key` 搭配不同测试版本或 AI 样本会被拒绝。

## 安全

### Tool Policy 与项目隔离

- `config/tool-policy.yaml` 是唯一项目级白名单，绑定 `project_id → namespace → repositories → allowed_paths → enabled_tools`。
- Tool Client 只把白名单中的工具暴露给模型，并把通用 MCP Schema 收窄成逐工具最小 Schema；执行前再次校验，不能依赖模型自律。
- 当前没有 Shell、`run_code`、`write_code`、Kubernetes 写入或 Git 写入 Tool。未来执行型 Tool 必须标为高风险并强制走 Sandbox，不得接入普通只读 Client。
- `project_id` 只能选择服务端已配置项目。namespace、repo、path、user_id 和 task_id 均由服务端策略或 Scope 控制，浏览器和模型不能自由指定。
- Git 路径先与仓库根目录组合并 `resolve()`，再检查真实路径仍位于项目 allowed paths 内，阻止 `../` 和软链接越界。
- PromQL、LogQL 结构参数、label selector、trace ID、源码行范围、时间窗口和条数均有类型、字符、长度与范围限制；额外字段直接拒绝。

### 凭证与 RBAC

- 所有工具由 `@mcp.tool()` 注册，annotations 明确为只读、非破坏、幂等。
- 第三方 Kubernetes MCP 使用 `--read-only --toolsets core --disable-multi-cluster`；生产 Client 不暴露 `list_namespaces`，只允许项目 namespace 内的 list/get/events/image/restart-count。
- `deploy/kubernetes-mcp-reader.yaml` 创建独立 ServiceAccount、Role 和 RoleBinding，仅授予 `get/list/watch` 与 `pods/log`，不允许读取 Secret，也没有任何写动词。
- MySQL 账号只读，代码仅放行单条 SELECT/EXPLAIN SELECT。
- Git 远程地址必须为白名单主机上的无凭证 HTTPS URL；只读 Token 由后端 Git 凭证机制提供，不写入 URL、Tool 参数或模型上下文。
- Prometheus/Loki Bearer Token 仅由后端 HTTP Client 注入 Authorization Header，不进入 Tool Schema、Audit 参数、前端或 LLM。
- 用户密码使用独立随机盐和 60 万轮 PBKDF2-HMAC-SHA256；数据库不保存明文密码或明文 Token。
- 诊断、会话与 Evidence API 强制 Bearer Token，Conversation 查询始终同时校验 user_id。
- Git 仅 read/search/diff；`repository` 必须来自 Service Catalog 白名单，路径不能逃逸对应独立仓库，并优先读取 Pod 正在运行的 SHA。
- `search_code_state` 的 SQL 与表名固定，模型只能提交仓库名、关键词、组件类型和条数；查询结果不含源码，Reference 始终绑定完整 commit SHA。
- Tool Call 与 Tool Result 原文永久进入 MySQL Conversation Store；压缩成功后旧原文退出 Active Context，短 State/Evidence 进入专用 Memory 表。
- Intent 判断仅调用 LLM Gateway；分类成功前不会读取工具清单或执行任何 MCP/Kubernetes 调用。
- Tool 有参数校验、15 秒超时、结构化错误；工作流最多 12 步。
- VERIFY 过滤空结果并校验 Finding 引用；缺少至少两条互相支持的证据、缺少直接运行时证据或存在矛盾时返回 `insufficient_evidence`。

### Task Workspace 与 Docker Sandbox

每个 Agent API 请求都会生成服务端 `task_id` 和独立 `.sandbox-tasks/<task_id>`，请求结束或取消后校验路径并销毁任务目录。当前只读 Tool 不在容器内执行，Workspace 用于项目数据隔离；代码执行能力尚未暴露给模型。

未来 `CodeExecuteTool` 只能调用内部 `DockerSandboxManager.run()`。安全选项由后端固定，模型不能覆盖：

```text
--network none
--cpus <limit>
--memory <limit>
--pids-limit <limit>
--cap-drop ALL
--security-opt no-new-privileges:true
--read-only
--tmpfs /tmp:rw,noexec,nosuid,size=64m
--mount type=bind,source=<task-workspace>,target=/workspace
```

Docker 使用 argv + `shell=False` 启动，并有硬超时。未来工作流为：仓库副本 → Task Workspace → 修改/编译/测试 → `git diff` → 销毁 Sandbox。

### Audit Log

每次 Tool 成功、失败或被策略拒绝都会追加到 `tool_audit_logs`：`user_id`、`project_id`、`task_id`、`tool_name`、脱敏参数、`result_status`、`execution_time_ms`、`error_type`、UTC 时间。密码、Token、Authorization 和 Credential 字段统一替换为 `[REDACTED]`。

## 测试与评测

```powershell
python -B -m pytest -q
python -B -m pytest tests/test_durable_diagnosis.py tests/test_diagnosis_self_check.py -q
python evals/run_evals.py --case SRE-001
python evals/run_evals.py
# 使用本地已有 mysql:8.4 镜像验证真实 Docker 隔离参数
python scripts/verify_sandbox.py
```

当前回归包含 MCP 安全、Agent、API、MySQL Conversation Compaction、Memory 权限隔离、Code State 增量更新、Source Reference、Durable Diagnosis / Self-Check，以及 40 个 Pre-Merge Validation 专项测试。2026-09-08 本机真实 MySQL 8.4 结果为 `164 passed`；唯一 Warning 是 FastAPI TestClient 上游 Starlette/httpx 弃用提示，不影响断言或退出码。新增专项测试覆盖 FastAPI/Spring 接口发现、模块/接口归属持久化、手工创建目标校验、一键创建 v1、修改后追加 v2、冻结版本引用、幂等重试，以及 10 个显式 AI 参考样本全部进入生成提示。

Durable 专项覆盖 Phase checkpoint、INVESTIGATE 中途恢复、RUNNING Tool retry、completed Tool cache hit、Evidence/Event/Report 幂等、Lease 抢占、stale/fresh Lease、graceful shutdown、terminal state、最大尝试次数和近真实 startup recovery。Self-Check 专项覆盖 Healthy、expired Lease、损坏 Checkpoint、run_id/phase mismatch、terminal Lease、orphan Step、缺失 Evidence、dangling Graph、用户隔离 API 与轻量 `/health` 回归。

固定评测只读取 `SRE-001`～`SRE-010`。Runner 发送给 Agent 的请求只有 `symptom + project_id`，`expected_root_cause`、`required_evidence` 和 `forbidden_shortcuts` 只由 Evaluator 使用。每次运行都会在 `evals/results/latest.json` 保存逐 Case 的服务定位、根因、Evidence 完整度、Tool Calls、耗时、Token、最终状态和失败原因，以及整体 Accuracy/平均值；失败 Case 不会被过滤。

返回 [平台总览](../../README.md)。
