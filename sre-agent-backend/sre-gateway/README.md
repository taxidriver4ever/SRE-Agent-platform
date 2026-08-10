# SRE Agent Backend - LLM Gateway Auth

当前实现 API Token 鉴权和非流式 LLM Gateway 调用链，使用 SQLAlchemy ORM + SQLite，不包含账号密码或 JWT。

## 两类 API Key

- **用户 Gateway API Key**：格式为 `gw_sk_...`，由前端生成，客户端通过 `Authorization: Bearer <key>` 访问本系统；数据库只保存它的 Hash。
- **模型厂商 API Key**：例如 `OPENAI_API_KEY`、`CLAUDE_API_KEY`、`DEEPSEEK_API_KEY`，只配置在后端环境变量中，由 Provider Adapter 调用模型厂商。

两类 Key 相互独立。用户 Gateway API Key 不会作为厂商 Key 发送给 OpenAI、Claude 或 DeepSeek，厂商 Key也不会返回前端或写入 Usage 日志。

`gateway_tokens` 表由 Auth 模块的 `model` 包定义，并由 `repository` 包初始化和访问；`service` 不直接依赖 SQLAlchemy，通用数据库层也不依赖任何业务表。

## 安装与运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

前端通过以下 HTTP 接口生成 Token。明文只在响应中返回一次，SQLite 中仅保存 SHA-256 Hash：

```text
POST /v1/auth/tokens
```

启动 FastAPI：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

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

模型名可使用 `openai/gpt-4o-mini`、`claude/claude-sonnet-4`、`deepseek/deepseek-chat` 或 `ollama/qwen3:8b`。配置通过环境变量提供：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`
- `CLAUDE_API_KEY`（兼容 `ANTHROPIC_API_KEY`）、`CLAUDE_BASE_URL`
- `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`
- `OLLAMA_BASE_URL`
- `PROVIDER_TIMEOUT_SECONDS`

每次调用的 Provider、模型、Token 数、耗时和成功状态会写入 `gateway_usage_logs`，不会记录请求消息或模型回复。

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

## 使用 Docker 运行 Ollama 与 Gateway

Compose 会启动 Ollama、自动拉取 `qwen3:4b`，随后启动 Gateway。模型和 SQLite
数据分别保存在命名 Volume 中，重建容器不会丢失。

CPU 模式：

```powershell
docker-compose -f compose.yaml up -d --build
```

NVIDIA GPU 模式（Docker Desktop + WSL2 需要具备 GPU 容器支持）：

```powershell
docker-compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

首次启动需要下载约 2.5GB 的 `qwen3:4b`。可在 `.env` 中用
`OLLAMA_MODEL=qwen3:1.7b` 等值替换默认模型，但 Agent 的 `GATEWAY_MODEL` 也要
设置为对应的 `ollama/qwen3:1.7b`。

检查服务：

```powershell
docker-compose -f compose.yaml ps
Invoke-RestMethod http://127.0.0.1:11434/api/tags
Invoke-RestMethod http://127.0.0.1:8000/health
```

Gateway 容器通过 `http://ollama:11434` 访问 Ollama；宿主机仍可通过
`http://127.0.0.1:11434` 调试 Ollama。官方镜像和 GPU 参数说明见
[Ollama Docker 文档](https://docs.ollama.com/docker)。

## 测试

```bash
pytest
```
