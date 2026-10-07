# Agent Runtime 架构收敛与验收

日期：2026-10-07。本文描述当前实现；旧迁移说明 `langchain-mcp-refactor.md` 已标为 Historical / superseded。

## 范围与调用关系

这次只移除框架包装，不重新设计 Agent，不更换模型 Provider，不修改业务数据模型。

```text
FastAPI / Diagnosis Service
  → Task Reliability（MySQL / Lease / Heartbeat / owner-version CAS / Checkpoint）
  → DiagnosisWorkflow / EvidencePlanner / ToolAgent / IntentRouter
  → LLM Protocol.complete(list[LLMMessage])
  → GatewayLLM → sre-gateway → 配置的 LLM

ConversationCompactionService / CodeStateService / AITestGenerator
  → 同一 LLM Protocol

Workflow / ToolAgent
  → FastMCPToolClient
  → FastMCP Client.list_tools / call_tool
  → 项目 MCP Server 或 KubernetesMCPAdapter → 第三方 Kubernetes MCP Server
```

业务继续依赖 Protocol，只有 main.py 负责注入 GatewayLLM。Gateway 请求路径、消息字段、temperature、max_tokens、stream、Token 计量和 HTTP 异常分类保持原有实现。

## 删除内容

- 文件：`app/llm/langchain.py`、`app/mcp_clients/langchain.py`。
- 类与函数：GatewayChatModel、LangChainLLM、to_messages、from_messages、StructuredOutputParser、LocalCallTrace、call_config，以及 MCP 转换器的 specifications/call_tool 和 interceptor。
- 直接依赖：langchain-core、langchain-mcp-adapters。langsmith 原为传递依赖；上述依赖链移除后不再需要。
- 本次项目虚拟环境同时卸载 langsmith 和框架专用的 langchain-protocol。FastMCP、httpx、Pydantic、FastAPI、PyMySQL、PyYAML、apache-skywalking 均保留。已检查已安装依赖元数据，没有剩余的生效框架依赖要求。
- 配置：SRE_AGENT_BACKEND、Settings.agent_backend、两个 MCP Client 构造函数的 backend 参数，以及所有 legacy/langchain 分支。
- 不再保留双后端切换测试；原三份框架测试的有效契约迁移到 `test_llm_contract.py`、`test_mcp_contract.py`、`test_runtime_reliability.py`，不是整体丢弃可靠性测试。

## 模块改动

| 模块 | 改动与保留的行为 |
| --- | --- |
| LLM / 注入 | main.py 直接构造 GatewayLLM；LLM Protocol、LLMMessage、LLMResponse 不变；无 SDK 或模型重试层 |
| Prompt | 消息对直接构造数据类；Agent 固定模板使用 Python 单次格式化；JSON 参数不会二次格式化，空格、Unicode 和角色保持原样 |
| Structured Output | 直接 await llm.complete，再调用 validate_structured_output；原 JSON repair、Pydantic 校验、schema_retry_message、template_refill_message 不变 |
| 重试与 fallback | `retries + 1` 次常规尝试，失败后最后一次 template refill；成功立即返回；调用者的 fallback/commit 决策不变；计量累计每次实际返回的 Token |
| MCP | 用官方 list_tools 返回的 name/description/inputSchema 构建内部规格；直接 call_tool；优先 result.data，仅当其为 None 时保留原 content 包装 |
| Kubernetes | 保留 lazy stdio、连接锁、长期 session、命名空间限制、工具映射、YAML 解码、结果整形与 source 字段；保留 read-only/core/disable-multi-cluster 参数 |
| 本地观测 | agent_call_context 的 ContextVar 保留；以简单 trace_call 上下文记录开始、结束、耗时和异常类型，取消原样传播；不记录 Prompt、源码、Evidence 或结果正文，不增加远程 tracing |
| CI | 镜像运行时检查改为导入当前 Gateway、Structured Output、FastMCP 模块；没有弱化测试/扫描门禁 |
| 文档 | 两份 README 更新为当前唯一调用链；旧架构文档明确标记历史状态 |

日志元数据的框架专用 `framework_run_id` 改为本地 `call_id`，不属于 HTTP/SSE 或数据库事件结构。已有业务 Timeline/SSE、Tool Audit 和 task/project/step/phase 关联上下文保留。

## 本次实际验证

以下均为本次执行，未引用历史通过结果。测试使用项目 Agent 虚拟环境，框架包已经卸载。Gateway 没有独立 `.venv`，使用同一现有环境运行其测试。

| 检查 | passed | failed | skipped | errors | deselected | pytest duration |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 无数据库 Agent/LLM/MCP/Planner/Intent/Compaction/AI Test 定向回归（最终一次） | 83 | 0 | 0 | 0 | 7 | 3.16s |
| CI 契约测试 | 28 | 0 | 0 | 0 | 0 | 5.17s |
| 完整 Agent 测试尝试 | 0 | 0 | 0 | 3（收集错误） | 0 | 7.54s |
| Gateway 完整测试尝试 | 10 | 0 | 0 | 17（fixture 错误） | 0 | 40.53s |

`errors` 不是通过，也不是 skip。完整 Agent 收集阶段失败，不能据此声称 HTTP API、MySQL recovery 或完整业务回归通过。

定向测试命令（在 `sre-agent-backend/sre-agent` 执行）：

```powershell
.venv/Scripts/python.exe -m pytest --noconftest -q tests/test_llm_contract.py tests/test_gateway_llm.py tests/test_mcp_contract.py tests/test_runtime_reliability.py tests/test_agent.py tests/test_intent.py tests/test_evidence_workflow.py tests/test_planning_rules.py tests/test_baseline_concurrency.py tests/test_pre_merge_validation.py::test_ai_generator_passes_all_ten_user_selected_reference_samples -k 'not application_injects and not old_worker and not projection_failure and not expired_lease and not concurrent_baseline and not heartbeat_loss' -p no:cacheprovider --tb=short
```

这条命令仅运行不需要数据库的测试，临时绕过全局 MySQL autouse fixture，未修改 conftest，也未删除或 skip 数据库验收。7 项 deselected 包括应用组装与数据库可靠性测试，以及被名称条件选出的并发 baseline 测试；数据库恢复后需运行全量命令。

完整测试尝试命令：

```powershell
# Agent 目录
.venv/Scripts/python.exe -m pytest -q --tb=short -p no:cacheprovider
# Gateway 目录
../sre-agent/.venv/Scripts/python.exe -m pytest -q --tb=short -p no:cacheprovider
# 仓库根目录
sre-agent-backend/sre-agent/.venv/Scripts/python.exe -m pytest -q scripts/ci/tests -p no:cacheprovider --basetemp=.cache/runtime-ci-tests
```

其他检查：

- `python -m pip check`：No broken requirements found。
- 生产 Python 全量 AST 解析成功。
- 对比 HEAD 中原 Agent Prompt 固定模板：空工具列表、含 JSON 大括号/Unicode/空格的工具定义，完整输出逐字一致。
- 生产代码、CI 与 requirements 搜索框架 imports/调用和旧 backend 开关：零命中。
- `git diff --check` 通过。

## 未执行的联调与原因

- MySQL：`127.0.0.1:13308` 拒绝连接（WinError 10061）。Agent 的应用导入会初始化表，因此完整测试在收集时停止；Gateway 的 17 项数据库 fixture 同样失败。没有修改 fixture 来伪造通过。
- Docker：已尝试启动已安装的 Docker Desktop，Linux engine named pipe 仍不存在，无法启动测试数据库和容器联调。没有重置 Docker、WSL 或业务卷。
- Prometheus、Elasticsearch、SkyWalking、真实 Kubernetes MCP 和真实 Gateway/LLM：未启动可用外部环境，未执行端到端验证。MCP 回归使用真正的 in-process FastMCP Client/Server；Kubernetes 使用协议 fixture，Gateway 使用 httpx MockTransport，不等同于真实集群验证。
- owner/version fencing、过期 Lease、Heartbeat loss、Worker takeover、事务 rollback 等原测试保留，需在独立测试 MySQL 可用后跑全量验证。

## 风险检查

| 项目 | 结论 |
| --- | --- |
| 框架 import / backend switch 残留 | 生产代码、CI 和测试已清理；历史文档允许保留旧术语并已加标记 |
| 新 Agent Framework / 隐式 retry | 未引入；结构化重试预算与模型传输错误传播有测试覆盖 |
| Prompt / observation / Token | 契约回归通过；MCP dict/list/scalar/text/content fallback 均覆盖 |
| HTTP API / 数据库 Schema / Task 状态机 / Checkpoint | 没有修改这些协议、模型或持久化实现；真实数据库集成验证尚未完成 |
| Evidence Chain / Gate / Workflow | 没有替换工作流或 planner；相关纯逻辑回归通过 |
| 本地日志 | 去掉框架回调，改用本地上下文；仅日志关联 ID 字段更名，未上传诊断数据 |

验收状态：代码收敛与可独立运行的回归完成；全量数据库及外部系统联调仍待环境恢复后执行。未提交或推送本次改动。
