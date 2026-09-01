# SRE LLM Gateway

当前实现 API Token 鉴权和非流式 LLM Gateway 调用链，使用 SQLAlchemy ORM + MySQL，不包含账号密码或 JWT。

## 两类 API Key

- **用户 Gateway API Key**：格式为 `gw_sk_...`，由前端生成，客户端通过 `Authorization: Bearer <key>` 访问本系统；数据库只保存它的 Hash。
- **模型 Provider API Key**：例如 `VLLM_API_KEY`、`OPENAI_API_KEY`、`CLAUDE_API_KEY`、`DEEPSEEK_API_KEY`，只配置在后端环境变量中，由 Provider Adapter 调用推理服务。

两类 Key 相互独立。用户 Gateway API Key 不会作为 Provider Key 发送给 vLLM 或云端厂商，Provider Key 也不会返回前端或写入 Usage 日志。

数据库建表语句按模块存放，不在 Python 中硬编码：

- `app/auth/sql/schema.sql`：`gateway_tokens`，保存 Gateway API Key 的 Hash、创建时间和禁用状态。
- `app/gateway/sql/schema.sql`：`gateway_usage_logs`，保存模型调用用量、耗时和结果。
- `app/operation_log/sql/schema.sql`：`gateway_operation_logs`，保存 API Key 与模型调用操作审计记录。

三个模块的 MySQL DDL 均包含表、字段与索引说明，并通过原生 `COMMENT` 保存数据库元数据。通用数据库层只负责执行各模块自己的 SQL 文件，不依赖任何业务表。

## 安装与运行

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

前端通过以下 HTTP 接口生成 Token。明文只在响应中返回一次，MySQL 中仅保存 SHA-256 Hash：

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

Gateway 的 API Key Hash、模型调用用量和操作审计统一写入后端 Docker MySQL；连接参数由 `.env` 中的 `GATEWAY_MYSQL_*` 配置。无效或已禁用 Token 均返回 `401 Unauthorized`。

## Gateway 调用

统一接口：

```text
POST /v1/gateway/chat/completions
Authorization: Bearer gw_sk_xxx
```

模型名默认使用 vLLM 的稳定 served model name，例如 `vllm/qwen3-4b`；也可使用 `openai/gpt-4o-mini`、`claude/claude-sonnet-4`、`deepseek/deepseek-chat`，迁移期仍支持 `ollama/llama3.2:3b`。配置通过环境变量提供：

- `OPENAI_API_KEY`、`OPENAI_BASE_URL`
- `CLAUDE_API_KEY`（兼容 `ANTHROPIC_API_KEY`）、`CLAUDE_BASE_URL`
- `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`
- `VLLM_API_KEY`、`VLLM_BASE_URL`
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

Gateway 不保存提示词和模型回复，只在 Gateway MySQL 表中记录 Provider、模型、Token 用量、耗时、成功状态和必要的操作审计；Agent 会话与 State 使用独立的 Agent MySQL Schema。

本地模型不是在网关代码中写死的。每次请求都从 JSON 的 `model` 字段选择；
`vllm/` 和 `ollama/` 是显式路由前缀，传给对应推理服务的是斜杠后的名称。
默认 Compose 将 Hugging Face 模型 `Qwen/Qwen3-4B` 发布为稳定名称 `qwen3-4b`，
因此 Agent 使用 `vllm/qwen3-4b`，无需感知底层模型仓库路径。

## 使用 Docker 运行 vLLM

vLLM 使用 OpenAI-Compatible Server，由 Gateway 通过
`http://127.0.0.1:18000/v1/chat/completions` 调用。默认配置关闭 Qwen3 思考模式，
避免内部推理混入 Agent 的结构化 JSON；同时启用 Qwen3 reasoning parser 作为协议保护。

```powershell
cd ..
Copy-Item .env.example .env
# 正式环境请在 .env 中把 VLLM_API_KEY 换成随机值，并同步写入 sre-gateway/.env。
docker-compose -f compose.yml up -d vllm
docker-compose -f compose.yml ps
Invoke-RestMethod http://127.0.0.1:18000/health
```

镜像版本、模型仓库、served model name、上下文窗口与 GPU 显存比例分别由
`VLLM_IMAGE`、`VLLM_MODEL`、`VLLM_SERVED_MODEL_NAME`、`VLLM_MAX_MODEL_LEN` 和
`VLLM_GPU_MEMORY_UTILIZATION` 控制。模型与编译缓存使用独立命名 Volume，容器重建后可复用。

## Ollama 回滚路径

迁移稳定期内继续保留 Ollama 基础设施，但不再作为 Agent 默认 Provider。若 vLLM
出现模型兼容、驱动或显存故障，可启动 Ollama 并把 Agent 的 `GATEWAY_MODEL` 临时
改回 `ollama/<模型标签>`。

启动 Ollama：

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
