# LangChain + MCP 渐进式重构

## 分析基线

2026-09-24，修改业务代码前执行 Agent 全量测试：194 passed，253.57 秒；一个已有 Starlette/httpx 弃用警告。测试绑定独立 `sre_agent_test` MySQL 库。

### 实际调用链

```text
FastAPI
  /api/diagnoses
    -> DiagnosisService.create -> MySQL DiagnosisSession
    -> DiagnosisExecutionManager.submit / recover_stale_diagnoses
       -> Repository.claim (owner + state_version CAS)
       -> Heartbeat + TaskSecurityScope + ConversationMemoryScope
       -> DiagnosisOrchestrator.run
          -> DiagnosisWorkflow.run(resume_state, DurableWorkflowRuntime)
             -> TRIAGE / BASELINE / ANALYZE / INVESTIGATE / VERIFY / REPORT
             -> EvidencePlanner (确定性规则优先，LLM fallback)
                -> LLM.complete -> GatewayLLM -> sre-gateway -> Provider
             -> runtime.begin_tool -> tools.execute -> runtime.complete_tool
                -> FastMCPToolClient (Policy + Audit)
                   -> FastMCP Client (进程内) -> 本地只读工具
                   -> KubernetesMCPAdapter -> stdio 第三方 Kubernetes MCP
             -> Evidence Chain -> Evidence Gate -> Report
          -> Report projection -> runtime.finalize_session

  /api/diagnoses/quick/stream -> DiagnosisWorkflow + NoopWorkflowRuntime
  /api/agent/chat -> IntentWorkflowRouter -> IntentRouter -> DiagnosisWorkflow
  /v1/agent/run -> ToolAgent (有界 JSON-ReAct 循环)
```

不是所有入口都使用持久 Task：Quick 保持无记忆；旧聊天入口的工作流使用默认 Noop Runtime。此次不改变这些公开语义。

### 现有设计与问题

* 没有自研 MCP 协议需要推翻。FastMCP 已负责 Schema、验证、发现和传输；聚合 Client 的权限、审计与 Kubernetes 语义转换是业务能力，应保留。
* GatewayLLM 不属于过重抽象。它隔离自定义 `/v1/gateway/chat/completions`、密钥、异常与连接池所有权。网关只支持文本消息、不支持原生 tools/tool_calls 和流式生成。
* Prompt 与消息组装散落于 Agent、Intent、Planner、Memory、Code State、AI Test。结构化重试在多个模块重复，但各模块失败降级不同，不能统一成无差别重试。
* ToolAgent 有通用循环；DiagnosisWorkflow 中的循环还承担确定性证据策略、恢复和幂等边界。不能用自由运行 Agent 覆盖它。优先迁移每轮 Prompt/模型/解析/工具执行到 LangChain Runnable，并保留有界业务控制循环。
* 本地 MCP 每次调用创建短会话是有效的进程内隔离策略。Kubernetes 使用惰性长 stdio 会话，首次并发连接需测试与串行初始化。
* 现有 Step/Event/Evidence 和 Audit 提供大部分观测数据，但 Agent 决策原因、业务 Step、框架调用的关联需要补齐；不能自动开启外部 Trace 上报。
* 错误类别分散；保留现有 API 捕获的基类，添加兼容子类和本地观测，不把取消/所有权丢失吞成普通工具错误。
* complete_tool_step 已把 Step/Evidence/Checkpoint/Event 与 owner/version CAS 放进事务。另有既存边界：并发收集会吞掉 Runtime 异常，心跳异常不会立即终止执行，最终报告投影先于最终 CAS。需要对所有权丢失回归验证；不能声称现有每个写入均已被 fencing 保护。

## 目标架构

```text
                     FastAPI / Diagnosis API
                               |
                      Diagnosis Service
                               |
                 Task Reliability Layer (保留)
         +-------------------------------------------+
         | MySQL Task / Lease / Heartbeat / owner CAS |
         | Business Checkpoint / Step-Evidence-Event  |
         | Idempotency / Recovery / Resume           |
         +---------------------+---------------------+
                               |
                   有界 Evidence-driven Agent
         +-------------------------------------------+
         | 原阶段、确定性策略、Evidence Gate、预算      |
         | LangChain Prompt -> ChatModel -> Parser   |
         | LangChain Tool / Context / Tool Result    |
         +---------------------+---------------------+
                               |
                 Policy / Audit / Trace context
                               |
                 官方 LangChain MCP Adapter
                               |
                   FastMCP / MCP Client
                    /                  \
           本地进程内工具服务       Kubernetes stdio MCP
          /      |      |    \                 |
     Prometheus ES    MySQL   Git/SkyWalking       Kubernetes
```

本地服务已有职责明确的工具模块，本轮维持聚合部署和原工具名；不为了展示多 Server 而增加端口和进程。未来可独立部署各模块，Agent 接口无需变化。

### 职责

| 层 | 责任 |
|---|---|
| Task | 创建、状态、owner/CAS、租约、心跳、接管、业务检查点、取消 |
| Agent | Prompt、LLM、有限循环、上下文、证据决策、工具预算、结果回填 |
| MCP | 官方适配、工具发现、标准 Schema、会话与传输 |
| Tool | 只读系统访问、SQL/路径/Namespace 边界、数据源超时、结果裁剪 |
| Persistence | MySQL 会话、Task、Step、Evidence、Event、审计；不使用框架 Memory 替代 |
| Observability | 保留 Timeline/SSE/Audit；关联 task/run/step/phase/工具及公开决策原因 |

## 分阶段实施（先分析，后按模块修改）

### Phase 1：模型与 Prompt

* 修改：requirements、app/llm、app/agent/prompt.py、配置及依赖组装。
* 新增：GatewayChatModel、LangChainLLM、消息/Prompt helper；保留 GatewayLLM 作为 transport 和回滚实现。
* 删除：无现有业务类；消除重复消息转换。
* 原因：标准化模型调用，不改变 Gateway 请求、Token 用量和异常。
* 风险：消息角色、JSON 大括号、隐式重试、usage 计量、异步取消。
* 回滚：运行时 backend 开关切回 legacy，重启生效；无数据库迁移。
* 测试：逐字请求对照、错误/空内容、Token、取消、资源关闭。

### Phase 2：官方 MCP 工具接入

* 修改：app/mcp_clients/client.py、kubernetes.py；新增官方适配边界模块。
* 新增：薄 MCP session bridge（仅在官方 adapter 不直接接受 FastMCP Client 时）和结果兼容转换。
* 删除：新路径的手写工具执行协议转换；保留 Kubernetes 业务名称/参数/结果映射。
* 原因：由官方 load_mcp_tools/转换函数生成 LangChain Tool，不自行重写 MCP-to-Tool 协议。
* 风险：structuredContent 与 FastMCP data 不同、ContextVar 丢失、工具名变化、错误包装、会话跨任务。
* 回滚：同一 backend 开关切回 FastMCP 直接调用。
* 测试：真实进程内 MCP、list/scalar/dict/text 结果对照、权限/记忆可见性、失败审计、并发、取消、Kubernetes 语义。

### Phase 3：Agent 调用链与上下文

* 修改：ToolAgent、Intent、planning/structured、Memory、Code State、AI Test。
* 新增：共用有界结构化调用 helper 和 LangChain Parser/Runnable；只抽取真正重复逻辑。
* 删除：Intent/Planner/Memory 中重复的常规修复循环；各自 fallback 留在业务层。
* 原因：统一模型/解析/工具入口，保持所有原始 Prompt、调用次数和降级结果。
* 风险：重试耗尽差异、上下文互串、恢复后重做已完成工具。
* 回滚：调用层 backend 开关；必要时按模块回退代码，无 Schema 变化。
* 测试：强制 LLM Planner/Synthesis、无效 JSON 修复、补填、失败摘要不提交、ToolAgent 最大轮数、Evidence Gate。

不采用 create_agent：当前网关没有原生 tool calling；新版高层 Agent 还会引入 LangGraph 运行时。用 Runnable 统一每轮操作，保留不可删除的业务控制循环。未来若网关支持原生工具调用，再独立评估 AgentExecutor/create_agent，不能暗改当前 JSON-ReAct 协议。

### Phase 4：观测、可靠性与验收

* 修改：必要的 Runtime 错误传播与框架调用关联；补充测试、迁移文档、依赖锁。
* 新增：观测 context（只存公开原因和标识，不存隐藏推理）；对照/故障测试。
* 删除：无 Task/Lease/CAS/Checkpoint 类。
* 风险：普通工具失败误终止整轮、框架 callback 越过业务持久化、旧 Worker 写入。
* 回滚：框架开关；可靠性边界修正独立于框架，不撤销原有 fencing。
* 测试：Agent 全量、Gateway 全量、现有场景评测（需确认服务运行与故障注入条件）；必须记录不可执行项而非引用历史结果。

## 验收与限制

固定响应下比较消息、请求、工具参数/结构、retry、Token、阶段与最终报告。排除时间戳、随机 ID、独立并发完成顺序。真实模型按功能与证据标准验收，不承诺逐字一致。

保留 max_steps、max_iterations、Gateway max_tokens、结构化重试、工具超时与整轮 deadline；不新增隐式重试。当前无货币成本硬上限，不虚构新增收费策略，保留 Token 计量与已有预算。

业务 Checkpoint 是恢复事实源；LangChain 消息/Runnable 是每次执行的短期上下文，不作为 Task checkpoint。未来适合评估 LangGraph 的位置是阶段图与调查循环；MySQL fencing/幂等仍必须独立存在。

依赖选择：验证 `langchain-core==1.6.4`、`langchain-mcp-adapters==0.3.2` 与 `fastmcp==3.4.5`。不引入厂商 SDK，不开启 LangSmith 远程跟踪。

参考：
* https://reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel
* https://reference.langchain.com/python/langchain-mcp-adapters/tools
* https://docs.langchain.com/oss/python/langchain/mcp

## 实施结果

已完成模型与 Prompt、官方 MCP Tool、共用结构化调用、观测关联的逐模块接入。
使用 `load_mcp_tools(client.session)`，不需要新 session bridge；官方 interceptor
复用 FastMCP 公共 `call_tool`，保留 typed/unwrapped data、原始 ToolError、会话管理。
LangChain Tool 负责标准调用，项目的薄边界仅恢复原有 Evidence 所需返回形状。

删除了 Intent、Planner、Memory 中重复的结构化重试循环。ToolAgent 的特殊
JSON-ReAct 协议恢复仍受原有最大轮数约束，未强行改成自由 Agent。
Code State 和 AI Test 共用 Prompt 及模型适配，保留原来的输出验证与降级。

可靠性修正：开始/恢复事件与报告投影在 owner/version/未过期 lease 校验及行锁保护下
使用事务；事务内的工具操作和投影统一先锁 Task 再锁 Step/Evidence，避免锁顺序倒置。
并发 Runtime 所有权错误会立即取消同组等待；心跳失败取消正在运行的 Agent；
过期租约不能通过 checkpoint/heartbeat/tool completion 重新续期，也不能写 FAILED。
没有改数据库 Schema。业务 Conversation/Audit 的原有独立存储边界保持不变。

本地回调输出 `agent.call.started/finished`，包含框架 run ID、Task scope、
诊断 correlation ID、phase、Step ID、logical step key、公开 reason、耗时及错误类型。
工具参数与完整结果仍分别在现有 Audit 和 Step/Evidence/Conversation Store 中，
不在普通日志复制原始日志、源码或隐藏推理；不自动上传到 LangSmith。

### 错误边界

| 信号 | 处理 |
|---|---|
| GatewayConfigurationError | 保留配置错误，健康检查仍可启动 |
| LLMTimeoutError / LLMRateLimitError | GatewayRequestError 兼容子类；不隐式重试 |
| GatewayRequestError | 保留网络/协议错误和 API 映射 |
| StructuredOutputError(json_format/schema_validation) | 有限本地修复、原预算重试、一次模板补填 |
| ToolPolicyError | Client 记录 denied 审计，并转 ToolExecutionError |
| MCP transport / schema / server failure | 保留 cause 与原异常名，转 ToolExecutionError；框架不把失败当成功结果 |
| AgentMaxIterationsError | JSON-ReAct 达到原最大轮数时终止 |
| insufficient_evidence | Evidence Gate 的业务结果，不用通用异常替代 |
| DiagnosisOwnershipLost(TaskOwnershipLostError) | 立即停止，不回填成模型可恢复的工具错误 |
| asyncio.CancelledError | 向上传播并关闭等待中的调用，保留 Task 中断语义 |

### 安装与回滚

在 `sre-agent-backend/sre-agent` 下执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

本次已在原 `.venv` 补齐固定版本的框架依赖；独立验证环境位于根目录
`.cache/langchain-venv`，不会提交。新增直接依赖和 FastMCP 精确锁在 requirements.txt；
其他既有依赖仍沿用原版本范围，不声称所有传递依赖均已锁定。

默认 `SRE_AGENT_BACKEND=langchain`；改成 `legacy` 并重启可以切回模型/MCP 后端。
两条后端不同时执行请求。共用 Prompt/Parser 和可靠性修正不随开关回退，
因此保留安装依赖；不存在数据库回滚或已有检查点格式转换。

### 验证记录

* 改造前 Agent：194 passed。
* 模型请求/取消/Prompt：8 passed。
* 官方进程内 MCP 与安全：19 passed。
* Intent/Memory/Agent/Planner/Code State：35 passed。
* 所有权、接管、回滚、心跳取消与 Durable：30 passed。
* Agent 原 `.venv` 全量回归：216 passed，253.29 秒。
* 最后补充的后端切换/会话隔离测试与相关可靠性回归：61 passed，96.25 秒；覆盖最终的恢复事件所有权保护和过期租约失败写入限制。此为最终补充回归，与上述全量测试有重叠，不应相加统计。
* Gateway：27 passed，7.76 秒；一个依赖弃用警告。首次运行因 Windows 缓存目录权限卡在 pytest 收尾，改用仓库 `.cache/pytest-gateway` 后正常退出。
* 对 Git HEAD 原实现的 10 组对照：Prompt 文本、消息角色/内容、重试请求、结果和 Token 计量一致；包含 JSON 修复、空响应、模板补填和失败降级。
* 原 `.venv` 依赖检查：102 个包，全部兼容。`git diff --check` 通过。
* 按当前配置重新探测：Agent/Gateway 连接超时，Prometheus ready=200，Loki ready=503。十场景真实联调未执行，不能以历史评测数据代替。
