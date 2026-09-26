# 可观测性迁移记录

> CI/CD 后续调整：文中旧 `deploy/k8s/configmap.yaml` 已迁移至 GitOps Helm values；当前部署入口见 [GitOps 交付说明](gitops-delivery.md)。

> 本文记录前一阶段迁移。当前采集链路已增加 Logstash，History 检索与最新验收以 [History Search 架构报告](history-search-architecture.md) 为准。下文直接 Filebeat→ES 的验证属于历史记录。

## 修改前分析与方案

当前只读 HTTP Tool 在 `sre-agent-backend/sre-agent/app/mcp_servers/observability/tools.py`：Prometheus 使用 `/api/v1/query`；旧日志后端使用标签查询；旧 Trace 后端使用搜索与详情 REST API。配置来自 `app/core/config.py`，白名单在 `app/security/policy.py` 与 `config/tool-policy.yaml`，证据由 `app/evidence/tool_result.py`、`app/workflow/evidence_gate.py` 和 `runtime_extractor.py` 处理。现有 Evidence/DiagnosisEvidence 已包含来源、时间、摘要、引用和结构化扩展字段，无需数据库迁移。

基础设施实际入口是 `sre-broken-system/sre-lab-infra/k8s/observability/stack.yaml`、同目录部署脚本和 `observability/` 配置；后端现有 Compose 只启动模型与应用数据库。`legacy-monorepo-snapshot` 是归档副本，不能继续保留失效的旧观测部署入口。

迁移删除旧日志/Trace 服务、查询实现、采集器及配置；新增 Elasticsearch、Kibana、Filebeat、SkyWalking OAP/UI。Prometheus 的查询与抓取逻辑保留。保留 `query_logs` / `query_trace` 供现有检查点和规划器调用，同时新增 `search_logs(service_name,start_time,end_time,level,keyword,trace_id,exception_type,limit)`、`search_traces(service_name,start_time,end_time,min_duration_ms,error_only,limit)`、`get_trace(trace_id)`、`get_service_metrics(service_name,start_time,end_time)`。所有查询有时间/条数/响应大小上限，不暴露索引、DSL、GraphQL 或修改能力。

日志使用已有 JSON 输出 → Filebeat 归一化 → Elasticsearch；不引入 Logstash。Evidence 继续使用当前模型，增加可选统一关联字段及标准化 spans/logs；动态规划与 Evidence Gate 保留。Task、Checkpoint、Lease、Heartbeat、CAS、Step 幂等逻辑不在本次修改范围。

## 协议依据与兼容选择

固定 SkyWalking OAP/UI 10.2.0。原生 Trace 使用官方 GraphQL 的 `searchService`、`queryBasicTraces`、`queryTrace`；APM 使用固定 MQE 表达式 `execExpression`。接口定义见 [trace](https://github.com/apache/skywalking-query-protocol/blob/v10.2.0/trace.graphqls)、[metadata](https://github.com/apache/skywalking-query-protocol/blob/v10.2.0/metadata.graphqls)、[metrics-v3](https://github.com/apache/skywalking-query-protocol/blob/v10.2.0/metrics-v3.graphqls)。

现有 Java/Go Demo 使用 OpenTelemetry 和 W3C TraceId。官方明确 OAP 将 OTLP Trace 转入其 Zipkin 存储，查询须走 OAP 自带 Zipkin Query API，而非原生 GraphQL。因此保留已有插桩与 Collector，将出口指向 OAP，并在同一个 SkyWalking Adapter 内归一化两类结果。不会部署额外 Zipkin 服务，也不会把 OTLP Trace 冒充为原生 APM 指标。参见 [OAP OTLP 支持](https://skywalking.apache.org/docs/main/v10.2.0/en/setup/backend/otlp-trace/) 与 [OAP Zipkin 查询](https://skywalking.apache.org/docs/main/v10.2.0/en/setup/backend/zipkin-trace/)。Python Agent 采用原生 SkyWalking 插桩，日志写当前真实 TraceId；不同传播协议不会自动合并为一条 Trace。

## 启动与配置

仓库根目录执行 `docker compose up -d --build`。可先复制根目录 `.env.example` 为 `.env` 并填写 Gateway Token、模型地址和本地密码。Compose 启动 Prometheus、Elasticsearch、Kibana、Filebeat、OAP/UI、Collector、应用 MySQL、实验 MySQL、Redis、Gateway、Agent、前端及六个现有 Demo Service。模型推理仍使用现有 Gateway 配置的 vLLM/Ollama，不默认另起占用 GPU 的模型实例；缺少模型服务或 Gateway Token 时应用健康检查可通过，LLM 诊断会报告配置/上游错误。

已有 Kind 与后端 MySQL 会占用 `18080/19090/13307/13308` 等端口，不应同时启动完整 Compose；现有集群用户使用 `sre-broken-system/sre-lab-infra/scripts/deploy-lab.ps1`。新的 Kind 映射需要新建集群才生效，已有集群可分别使用 `kubectl -n sre-lab port-forward svc/elasticsearch 19200:9200`、`svc/skywalking-oap 12800:12800` 和 `svc/skywalking-oap 19412:9412`。仓库不自动销毁用户集群或旧数据卷；已部署的旧观测资源需由环境维护者在新栈验收后退役。

Compose 没有虚构 Kubernetes 集群：其 Demo 以容器运行；需要 Kubernetes Evidence 的完整场景继续使用 Kind。Filebeat 读取 Docker Linux daemon 的 json-file 日志，Demo 与 Agent 显式配置该驱动，Docker Desktop 须能挂载 daemon 日志路径。Kind 使用 Filebeat DaemonSet 读取 CRI 日志。ES 与 OAP 共用单节点 ES，但分别使用 `sre-logs-*` 与 `sw_*` 索引。Agent 只查询配置的日志索引；OAP/Filebeat 的写入只属于采集基础设施，不是 Agent Tool 能力。

默认入口：前端 `3000`、Agent `8001`、Gateway `8000`、Prometheus `19090`、ES `19200`、Kibana `15601`、OAP GraphQL `12800`、OAP gRPC `11800`、OAP Zipkin Query `19412/zipkin`、SkyWalking UI `18088`。OTLP 数据在 SkyWalking UI 的 `/zipkin` 页面查看，原生 Python Trace 在常规 APM 页面查看。

本机 Agent 的观测环境变量见 `sre-agent-backend/sre-agent/config/observability.env.example`；把这些配置并入已有未提交 `.env`。`PROMETHEUS_BASE_URL` 保留兼容，`PROMETHEUS_URL` 优先。ES 用户应授予日志索引读取权限；凭据不进入 Tool Schema/结果。`SKYWALKING_OAP_URL` 是 HTTP 查询端点，`SKYWALKING_AGENT_COLLECTOR` 是 `host:11800` 插桩上报地址，二者不可混用。通过 `python -m app.serve` 启动 Agent 才会按开关提前加载 Python 插桩；原 `uvicorn app.main:app` 入口仍可使用但不自动开启插桩。

## 验证 Elasticsearch 与 SkyWalking

```powershell
docker compose config --quiet
docker compose run --rm --no-deps filebeat test config -e --strict.perms=false
Invoke-RestMethod http://127.0.0.1:19200/_cluster/health
Invoke-RestMethod -Method Post -ContentType application/json -Uri http://127.0.0.1:12800/graphql -Body '{"query":"{ version }"}'
Invoke-RestMethod http://127.0.0.1:19412/zipkin/api/v2/services
```

向 Demo 发出业务请求后，在 Kibana 创建 `sre-logs-*` Data View，时间字段选择 `@timestamp`；核对 `service_name`、`level` 和 `trace_id`。日志中的 ID 必须能通过对应的 SkyWalking Tool 查询到真实 Span。原生 Python 使用 SW8，已有 Java/Go 使用 W3C/OTLP；跨这两类传播协议不能自动形成同一 Trace，不能把不同 ID 人为拼成一条因果链。要得到全链路原生 APM，应另行统一所有语言插桩；当前低侵入迁移保留既有 OTLP 采集与查询能力。

开发环境 ES 无鉴权且只在 Compose 环回端口暴露；Kubernetes 清单面向本地 Kind。ES 512 MB 堆、OAP 512 MB 堆，单分片零副本，OAP 2 天 TTL（10.2.0 要求至少 2 天）；Compose 日志索引为开发数据卷，长期运行需要维护者配置日志保留策略。Kind ES 使用临时卷，场景重置会重新创建观测数据，不能用于需要持久保留证据的环境。

## 修改范围与清理

* Adapter/Tool：`app/mcp_servers/observability/adapters.py`、`tools.py`，复用 HTTP/FastMCP/LangChain 通路。
* 配置/权限/提示词：`app/core/config.py`、`app/security/policy.py`、`config/tool-policy.yaml`、`app/agent/prompt.py`。
* Evidence：`app/evidence/`、`app/workflow/models.py`、`runtime_extractor.py`、`evidence_gate.py`、`planning/`；只做新格式映射、来源选择和 Span 证据规则。Task/CAS/Heartbeat 实现未改。
* 采集：`app/core/telemetry.py`、`app/serve.py`；Python Demo 插桩与 JSON 日志；Java 使用 Spring Boot 内置 JSON encoder，正确转义异常、多行和引号。
* 基础设施：根 `compose.yaml`、`sre-lab-infra/observability/`、`k8s/observability/stack.yaml`、Kind 端口与部署脚本、`deploy/k8s/configmap.yaml`。
* 测试/说明：`tests/test_observability_migration.py`、既有 Evidence/安全/规划测试、评测用例要求、README、架构图与运行手册。

已删除旧日志/Trace HTTP 查询、原服务 YAML 与采集配置；历史快照的旧观测部署入口已禁用并转向现行实验环境。历史 `evals/results/` 的实测 JSON 保持原始内容，属于审计记录，绝不能批量改名冒充新栈结果。

## 测试命令与验证记录

在 `sre-agent-backend/sre-agent` 执行：

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q -o cache_dir=../../.cache/pytest-migration
```

2026-09-25 已完成以下真实协议验证，数据均为明确标记 `sre-migration-*` 的合成测试数据：

* ES 8.17.3 健康接口、SkyWalking 10.2.0 健康接口、UI GraphQL 代理均通过。
* Filebeat 8.17.3 `test config` 通过；使用实际容器日志 parser、JS 归一化和 JSON 索引模板将包含引号/换行的结构化日志写入 ES。Agent Elasticsearch Adapter 按 service_name + trace_id 查到原始消息和文档引用。
* Python 原生 SDK 上报的 Trace 能通过 Adapter 的详情、服务搜索、APM 查询返回；ES 中同一 TraceId 可关联实际 Span。FastAPI TestClient 请求验证了自动插桩确实建立 Entry Span。
* 合成 OTLP HTTP Trace 经实际 Collector → OAP 上报后，可由同一 Adapter 查询到原始 W3C ID、2800 ms 数据库 Span、SQL 与错误状态。
* 实机发现并修复：OAP TTL 最小值、UI health checker、Zipkin 查询拒绝零 minDuration、无数据的 APM 点过滤；全部记录被输出上限裁掉时标记 empty，避免被 Evidence Gate 当作有效证据。
* Compose 解析、Kubernetes 观测清单 server dry-run、修改过的 YAML 重复键/结构检查、Python 语法及重复定义检查、`git diff --check`、109 个已安装 Python 包的依赖兼容检查通过。Elasticsearch 使用独立 NodePort 30201，避免与旧观测 Service 冲突。
* user-service 与 recommendation-service 的既有单元测试各 1 项通过。

Python 插桩固定为 1.3.0，修复 1.2.0 在 Python 3.12 下未执行插件模块的问题（见[官方发行说明](https://skywalking.apache.org/events/release-apache-skywalking-python-1-3-0/)）。实测 1.3.0 wheel 缺少 `skywalking.protocol`，官方源码发行包构建正常，因此三个 Python 服务的 requirements 显式设置 `--no-binary=apache-skywalking`。请通过 requirements 安装，不要单独安装该版本 wheel。

最终 Agent 全量回归：**250 passed in 303.76s**。包括数据源超时/鉴权/空结果/截断、真实接口格式模拟、Metrics→Logs→Trace 数据库延迟证据链、观测元数据与来源引用的持久化恢复，以及原有任务租约、心跳、重试、恢复等业务测试。两个 Python Demo 测试合计 **2 passed**。历史实机评测不得作为新栈验收证据。

验证结束后，已移除本次两个临时采集器容器，停止本次创建的 ES/OAP/UI 验证服务；验证数据卷保留。原有 Kind、模型和 MySQL 容器未停止。所有代码变更保留在工作区，未自动提交。

## 尚未完成的环境验收与使用限制

1. 没有对已有 Kind 集群执行迁移或销毁旧观测数据，也没有重跑十个真实故障场景。当前实测覆盖真实采集/查询协议和自动化业务回归，不能替代整个实验系统的上线验收。
2. Kind 的 `build-images.ps1` 严格从各服务 `good` / `bad` 标签构建；工作区中新加的日志格式、Python 插桩不会自动进入这些历史镜像。正式场景验收前须将相应改动纳入各服务版本，更新场景所用 SHA / 镜像及 Deployment 注解后重新构建。不能把修改后的代码伪装成原始 SHA 镜像。根 Compose 直接构建当前工作区，可验证本次修改。
3. 原生 SW8 与既有 W3C 传播保持各自真实身份，尚未统一成跨所有语言的一条原生 SkyWalking Trace。OTLP 链路可查询，但不会产生原生服务 APM 指标。
4. 本次没有启动完整 Compose/Kibana 或构建所有语言的业务镜像；本机已有 Kind/MySQL 占用部分默认端口。完整启动前按上述两种部署方式择一配置，并提供可用模型与 Gateway Token。
