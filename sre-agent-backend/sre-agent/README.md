# SRE Agent Backend

单 Agent 后端通过统一 `GatewayLLM` 连接 `sre-gateway → Docker Ollama`，实际模型由请求数据指定。项目自有工具使用 FastMCP 3.4.5；Kubernetes 改为维护活跃的 `containers/kubernetes-mcp-server`，以只读、单集群、core 工具集模式直接调用 Kubernetes API。

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
- `app/api/`：旧 `/v1/agent/run`、结构化 `/api/agent/chat` 和 SSE `/api/agent/chat/stream`。
- `skills/`：12 个独立 SRE Skill，覆盖语言运行时、Kubernetes、数据库、依赖、发布、Tracing 与证据综合。
- `evals/`：SRE-001～010 评测数据与 Runner。

## 配置

```text
GATEWAY_BASE_URL=http://127.0.0.1:8000
GATEWAY_API_KEY=gw_sk_...                 # 只保存 Gateway Token，不是 Provider Key
GATEWAY_MODEL=ollama/qwen3:4b
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
TOOL_OUTPUT_LIMIT=12000
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

每份 SQL 都包含字段级 `COMMENT` 和表级 `COMMENT`。各模块的 `schema.py` 只负责定位并执行本模块 SQL；`app/core/database.py` 只负责通用 MySQL 连接、事务和 SQL 文件执行，不依赖任何业务表。

## 启动

MySQL 数据统一持久化到后端根目录的 `data/mysql`，不在 Agent 或 Gateway 模块内生成数据库目录。

```powershell
# 后端统一使用根目录 compose.yml；表由各业务模块启动时初始化。
Set-Location D:\SRE-Agent-platform\sre-agent-backend
docker-compose -f compose.yml up -d mysql

# Compose 只启动基础设施；Gateway 保持本地 Python 进程运行。
docker-compose -f compose.yml up -d ollama ollama-model-init

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
- VERIFY 过滤空查询；少于两个独立证据源只能报告“高可能性候选根因”。

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
python -m pytest -q
python evals/run_evals.py --case SRE-001
# 使用本地已有 mysql:8.4 镜像验证真实 Docker 隔离参数
python scripts/verify_sandbox.py
```

当前代码包含 MCP 安全、Agent、API、MySQL Conversation Compaction、Memory 权限隔离、Code State 增量更新与 Source Reference 回归测试；最终通过数以本机 `pytest` 输出为准。

返回 [平台总览](../../README.md)。
