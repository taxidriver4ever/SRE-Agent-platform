# SRE Agent Backend

单 Agent 后端通过统一 `GatewayLLM` 连接 `sre-gateway → Docker Ollama/qwen3:4b`。项目自有工具使用 FastMCP 3.4.5；Kubernetes 改为维护活跃的 `containers/kubernetes-mcp-server`，以只读、单集群、core 工具集模式直接调用 Kubernetes API。

## 模块

- `app/llm/`：Provider 无关协议与 Gateway Client，Agent 不使用厂商 SDK。
- `app/agent/`：通用 JSON-ReAct Runtime，不再保留 calculator/current_time 等凑框架工具。
- `app/mcp_servers/`：项目自有 FastMCP Server，只注册 Prometheus/Loki/Tempo/MySQL 与 Git 只读工具。
- `app/mcp_clients/`：FastMCP 官方 Client 聚合层，以及第三方 Kubernetes MCP 的只读语义适配；Client 不放在 tools 中。
- `app/repositories/`：从 K8s `repository-url` 注解建立模块→远程仓库绑定，并按运行 SHA 创建浅克隆缓存。
- `app/context/`：Active Context Compaction、Evidence Store 与 Source Reference。
- `app/storage/`：Docker MinIO 客户端、完整 Evidence 对象读写和短时效签名。
- `app/uploads/`：用户附件预签名直传、完成校验、会话绑定和下载签名。
- `app/auth/`：PBKDF2 密码登录、随机 Bearer Token、数据库验证与注销撤销。
- `app/conversation/`：按用户隔离的 Conversation/Message SQLite 持久化与历史缓存 API。
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
MINIO_ENDPOINT=127.0.0.1:19100
MINIO_PUBLIC_ENDPOINT=127.0.0.1:19100
MINIO_ACCESS_KEY=sreagent
MINIO_SECRET_KEY=sreagent-dev-secret
MINIO_BUCKET=sre-agent-evidence
MINIO_PRESIGN_EXPIRE_MINUTES=15
UPLOAD_MAX_BYTES=52428800
LARGE_TEXT_THRESHOLD_BYTES=12288
ACTIVE_CONTEXT_CHARACTER_BUDGET=8000
APPLICATION_DATABASE_PATH=.data/sre-agent.sqlite3
AUTH_TOKEN_TTL_HOURS=24
SRE_INITIAL_USERNAME=admin
SRE_INITIAL_PASSWORD=admin123
```

## 启动

```powershell
# MinIO 与 Ollama 都由网关 Compose 编排；19101 是 MinIO 管理控制台。
Set-Location D:\SRE-Agent-platform\sre-agent-backend\sre-gateway
docker-compose up -d minio ollama ollama-model-init gateway

Set-Location D:\SRE-Agent-platform\sre-agent-backend\sre-agent
$env:GATEWAY_API_KEY = "从 POST /v1/auth/tokens 获得的 Token"
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## API

```powershell
$login = Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/auth/login `
  -ContentType application/json `
  -Body (@{ username="admin"; password="admin123" } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }
$body = @{ message = "为什么订单模块最近这么慢？"; conversation_id = $null } | ConvertTo-Json
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/agent/chat `
  -Headers $headers -ContentType application/json -Body $body
```

`/api/agent/chat/stream` 返回 SSE `phase`、`tool`、`final` 事件。前端可展示公开调查步骤、工具参数与摘要，但不展示隐藏 Chain-of-Thought。

最终报告中的每条证据包含 `evidence_id` 和 `source_references`。压缩摘要用于 LLM 上下文；完整原文持久化到私有 MinIO bucket，并可通过 `GET /api/agent/evidence/{run_id}/{evidence_id}` 回查。SQLite 的 `evidence_objects` 只保存 `run_id/evidence_id/oss_key/created_at`，没有 Evidence 正文列。

附件上传顺序为：前端创建或复用 Conversation → `POST /api/uploads/presign` → 浏览器直接 `PUT` MinIO → `POST /api/uploads/complete` 校验大小并保存 `oss_key` → 调用诊断 SSE。粘贴文本超过 12 KiB 时，前端会在普通附件之后把原文转换为 `.log` 并执行同一流程；预签名 URL 只短暂存在浏览器函数内存。

旧版 Evidence SQLite 可用以下命令一次性迁移。只有每一条 MinIO 对象回读验证成功后，`--purge-legacy` 才会删除旧 `payload_json` 表并压缩数据库文件：

```powershell
python scripts/migrate_legacy_evidence_to_minio.py --purge-legacy
```

前端启动顺序为：读取本地 Token → `GET /api/auth/me` 服务端校验 → `GET /api/conversations` 加载会话摘要缓存。诊断 SSE 的第一条 `conversation` 事件返回持久会话 ID，用户消息和最终结构化报告都会落入 `conversation_messages` 表。

标准 MCP Streamable HTTP 端点挂载在 `http://127.0.0.1:8001/mcp/`，只暴露项目的 Git/可观测性只读工具。Kubernetes MCP 是独立第三方 stdio Server，不伪装成项目自研 Server。

## Kubernetes 与远程仓库绑定

1. 可选地应用 `deploy/kubernetes-mcp-reader.yaml`，并为该只读 ServiceAccount 生成专用 kubeconfig。
2. 复制 `config/repository-bindings.example.yaml`，填入真实的 HTTPS Git remote；不要把 Token 写进 URL。
3. 由管理员手动执行 `python scripts/bind_kubernetes_repositories.py config/repository-bindings.yaml`。脚本会把 `sre.agent/repository-url` 同时写入 Deployment 和 Pod Template。
4. Agent 先通过 Kubernetes MCP 读取 `repository`、`repository-url`、`git-sha`，再由 Git FastMCP 对白名单主机执行精确 SHA 的浅抓取和源码读取。若当前实验仓库尚未配置 remote，则明确使用现有本地只读镜像，不伪造远程地址。

## 安全

- 所有工具由 `@mcp.tool()` 注册，输入 Schema 和参数验证由 FastMCP 从类型注解生成。
- 工具 annotations 明确设置 `readOnlyHint=true`、`destructiveHint=false`。
- 第三方 Kubernetes MCP 使用 `--read-only --toolsets core --disable-multi-cluster`，Client 仅暴露 list/get/events/image/restart-count 语义；建议再叠加 Namespace 级只读 RBAC。
- MySQL 账号只读，代码仅放行单条 SELECT/EXPLAIN SELECT。
- 用户密码使用独立随机盐和 60 万轮 PBKDF2-HMAC-SHA256；数据库不保存明文密码或明文 Token。
- 诊断、会话与 Evidence API 强制 Bearer Token，Conversation 查询始终同时校验 user_id。
- MinIO bucket 保持私有；上传/下载 URL 默认 15 分钟失效，Key 绑定 user_id 与 conversation_id，完成接口还会校验对象真实大小。
- Git 仅 read/search/diff；`repository` 必须来自 Service Catalog 白名单，路径不能逃逸对应独立仓库，并优先读取 Pod 正在运行的 SHA。
- Tool 原文进入 Evidence Store；Active Context Compaction 按来源多样性、结论支持度和源码引用选择摘要，避免日志淹没 Trace/SQL/源码。
- Tool 有参数校验、15 秒超时、结构化错误；工作流最多 12 步。
- VERIFY 过滤空查询；少于两个独立证据源只能报告“高可能性候选根因”。

## 测试与评测

```powershell
python -m pytest -q
python evals/run_evals.py --case SRE-001
```

当前代码包含 MCP 安全、Agent、API、Evidence Store、上下文压缩与 Source Reference 回归测试；最终通过数以本机 `pytest` 输出为准。
