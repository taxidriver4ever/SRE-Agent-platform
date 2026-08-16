# SRE LLM Gateway

当前实现 API Token 鉴权和非流式 LLM Gateway 调用链，使用 SQLAlchemy ORM + SQLite，不包含账号密码或 JWT。

## 两类 API Key

- **用户 Gateway API Key**：格式为 `gw_sk_...`，由前端生成，客户端通过 `Authorization: Bearer <key>` 访问本系统；数据库只保存它的 Hash。
- **模型厂商 API Key**：例如 `OPENAI_API_KEY`、`CLAUDE_API_KEY`、`DEEPSEEK_API_KEY`，只配置在后端环境变量中，由 Provider Adapter 调用模型厂商。

两类 Key 相互独立。用户 Gateway API Key 不会作为厂商 Key 发送给 OpenAI、Claude 或 DeepSeek，厂商 Key也不会返回前端或写入 Usage 日志。

数据库建表语句按模块存放，不在 Python 中硬编码：

- `app/auth/sql/schema.sql`：`gateway_tokens`，保存 Gateway API Key 的 Hash、创建时间和禁用状态。
- `app/gateway/sql/schema.sql`：`gateway_usage_logs`，保存模型调用用量、耗时和结果。
- `app/operation_log/sql/schema.sql`：`gateway_operation_logs`，保存 API Key 与模型调用操作审计记录。

SQLite 不支持原生字段 `COMMENT` 语法，因此 SQL 文件使用 `-- COMMENT:` 注释每张表、每个字段和索引；对应 ORM 模型同时保留表级和字段级 Comment 元数据。通用数据库层只负责执行各模块自己的 SQL 文件，不依赖任何业务表。

## 安装与运行

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

前端通过以下 HTTP 接口生成 Token。明文只在响应中返回一次，SQLite 中仅保存 SHA-256 Hash：

```text
POST /v1/auth/tokens
```

启动 FastAPI：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows 下若没有全局 `python`，或 IDE 选到了未安装依赖的解释器，可直接使用
项目虚拟环境（在 `sre-gateway` 目录执行）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app\main.py
```

IDE 的 Python Interpreter 也应设置为
`sre-gateway\.venv\Scripts\python.exe`，不要直接使用一个未安装 FastAPI 的全局解释器。

访问受保护接口：

```bash
curl -i http://127.0.0.1:8000/v1/auth/check \
  -H "Authorization: Bearer gw_sk_xxx"
```

数据库默认位于 `data/auth.db`，可通过 `GATEWAY_AUTH_DB` 环境变量修改。无效或已禁用 Token 均返回 `401 Unauthorized`。

## Gateway 调用

统一接口：

```text
POST /v1/gateway/chat/completions
Authorization: Bearer gw_sk_xxx
```

模型名可使用 `openai/gpt-4o-mini`、`claude/claude-sonnet-4`、`deepseek/deepseek-chat` 或任意已安装的 Ollama 模型（例如 `ollama/llama3.2:3b`）。配置通过环境变量提供：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`
- `CLAUDE_API_KEY`（兼容 `ANTHROPIC_API_KEY`）、`CLAUDE_BASE_URL`
- `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`
- `OLLAMA_BASE_URL`
- `PROVIDER_TIMEOUT_SECONDS`（本地 Evidence Planner 建议至少 `180` 秒）

每次调用的 Provider、模型、Token 数、耗时和成功状态会写入 `gateway_usage_logs`。API Key 创建、鉴权、禁用以及 Gateway 模型调用成功或失败会写入 `gateway_operation_logs`。两类日志都不会记录请求消息、模型回复、Gateway API Key 明文或厂商 API Key。

请求示例：

```json
{
  "model": "openai/gpt-4o-mini",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello"}
  ],
  "temperature": 0.7,
  "max_tokens": 512
}
```

当前版本只支持非流式请求，`stream: true` 会返回参数校验错误。

## API 一览

| 方法 | 路径 | 鉴权 | 作用 |
| --- | --- | --- | --- |
| POST | `/v1/auth/tokens` | 无 | 创建 Gateway Token，明文仅返回一次 |
| GET | `/v1/auth/check` | Bearer | 校验 Token 状态 |
| POST | `/v1/gateway/chat/completions` | Bearer | 执行一次非流式模型调用 |

Gateway 不保存提示词和模型回复，只记录 Provider、模型、Token 用量、耗时、成功状态和必要的操作审计。SQLite 仅属于 Gateway 自身，不保存 Agent 会话；Agent 会话与 State 使用独立 MySQL。

Ollama 模型不是在网关代码中写死的。每次请求都从 JSON 的 `model` 字段选择，
`ollama/` 是路由前缀，传给 Ollama 的实际名称是斜杠后的部分。例如同一个网关
进程可先后接收 `ollama/llama3.2:3b` 和 `ollama/deepseek-r1:7b`。目标模型需先
出现在 `ollama list` 中，否则 Ollama 会返回模型不存在。

## 使用 Docker 运行 Ollama

Compose 只启动 Ollama 基础设施，不构建或运行 Gateway 镜像。Gateway 继续使用
本地 Python/IDE 运行。若设置了 `OLLAMA_MODEL`，初始化容器会预先拉取该模型；
未设置时跳过预拉取，不再默认绑定到 qwen3。模型保存在命名 Volume 中。

CPU 模式：

```powershell
cd ..
docker-compose -f compose.yml up -d ollama ollama-model-init
```

NVIDIA GPU 模式（Docker Desktop + WSL2 需要具备 GPU 容器支持）：

```powershell
cd ..
docker-compose -f compose.yml -f compose.gpu.yml up -d ollama ollama-model-init
```

可在启动 Compose 前设置 `OLLAMA_MODEL=llama3.2:3b` 预拉取常用模型，也可以直接执行
`docker exec sre-ollama ollama pull deepseek-r1:7b` 安装更多模型。这个环境变量
只控制容器初始化，不控制请求路由；调用时仍以 JSON 中的
`"model": "ollama/deepseek-r1:7b"` 为准。

检查服务：

```powershell
docker-compose -f compose.yml ps
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

本地 Gateway 通过 `http://127.0.0.1:11434` 访问 Ollama。官方镜像和 GPU 参数
说明见 [Ollama Docker 文档](https://docs.ollama.com/docker)。

## 测试

```bash
pytest
```

返回 [平台总览](../../README.md)。
