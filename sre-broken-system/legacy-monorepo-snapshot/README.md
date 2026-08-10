# Local SRE Agent Lab

## 1. 项目目标

这是用于学习、演示和评测 Agent/SRE 自动故障诊断的本地实验平台。目标不是让模型猜答案，而是让 Agent 通过 Kubernetes、Prometheus、Loki、Tempo、MySQL 与 Git 的真实只读证据建立因果链。

## 2. 总体架构

`Vue :3000 → Agent :8001 → LLM Gateway :8000 → Docker Ollama qwen3:4b`。Agent 同时只读访问 Kind 集群、Prometheus、Loki、Tempo、MySQL 和本地 Git。详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 3. 已实现功能

- 四个异构业务服务：Java order、Go inventory、Python user、Node payment，加 MySQL。
- Kind Kubernetes 与 Prometheus/Loki/Tempo/OpenTelemetry/Alloy/kube-state-metrics。
- 可控故障开关、真实 Metrics/Logs/Traces、慢查询与 GOOD..BAD Git 历史。
- 26 个只读 MCP Tool、硬性诊断状态机、Gateway/Ollama 摘要和 SSE 问答页。

## 4. 已实现 Scenario

SRE-001 Slow SQL、SRE-002 DB Pool Exhaustion、SRE-003 Dependency Timeout、SRE-004 CPU Saturation、SRE-005 Memory Leak/OOM、SRE-006 Retry Storm、SRE-007 Deployment Regression。详见 [docs/SCENARIOS.md](docs/SCENARIOS.md)。

## 5. MCP Tools 清单

Kubernetes 11 个、Observability 7 个、Git 8 个，全部为只读。详见 [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md)。

## 6. Skills 清单

incident-triage、java-service-debugging、database-troubleshooting、kubernetes-debugging、resource-exhaustion、dependency-timeout、deployment-regression、evidence-based-diagnosis。详见 [docs/SKILLS.md](docs/SKILLS.md)。

## 7. Workflows 清单

通用 START→TRIAGE→BASELINE_OBSERVATION→ANALYZE→INVESTIGATE→VERIFY→REPORT→END，并共享 latency、5xx、pod restart、dependency timeout、deployment regression 策略。详见 [docs/WORKFLOWS.md](docs/WORKFLOWS.md)。

## 8. 如何启动

1. 在 `sre-gateway` 执行 `docker-compose up -d`，确认 `sre-ollama` 已有 `qwen3:4b`。
2. 在本目录执行 `./scripts/deploy-lab.ps1`。
3. 生成 Gateway Token，并以 `GATEWAY_API_KEY` 启动 `sre-agent` 的 `uvicorn app.main:app --port 8001`。
4. 在 `sre-agent-frontend` 执行 `npm run dev`，浏览 `http://127.0.0.1:3000`。

完整命令和恢复步骤见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

## 9. 如何触发 SRE-001

在本目录执行：`./scripts/run-scenario.ps1 -Scenario SRE-001`。脚本先重置所有在线故障，再开启 `slow_sql` 并发出 8 个请求。当前实测 P95 约 2.13 秒，slow_log 每条扫描 100000 行，EXPLAIN 为 `type=ALL`。

## 10. 如何通过前端提问

打开 `http://127.0.0.1:3000`，输入“为什么订单模块最近这么慢？”。页面会流式显示公开阶段和 Tool Call，最后分区展示结论、根因链、证据、修复、置信度与时间线。

## 11. 当前尚未完成的部分

- 第一版不提供任何自动修复/重启/扩缩容能力，这是有意的只读安全边界。
- Kind 未安装 metrics-server，因此 `get_resource_usage` 可能返回结构化上游错误；容器 CPU/内存由 Prometheus cAdvisor 提供。
- 7 个 Eval Case 已创建；应在分别触发对应 Scenario 后逐案运行，不能把错误场景混在同一次评测中。

## 12. 实际执行过哪些测试

运行过 Gateway 21 项测试、Agent 原有与 MCP 安全共 20 项测试、Vue production build、四个 Docker 镜像构建、Kind rollout、所有 Prometheus targets、Loki labels/logs、Tempo 精确 Trace 查询、MySQL slow_log/EXPLAIN、SRE-001 与 `/api/agent/chat` 端到端调用。

## 13. 哪些测试成功

上述自动测试和构建均成功。SRE-001 端到端报告定位 `order-service`，置信度 0.94，工作流八阶段完整；Loki 日志中的 `trace_id` 精确关联到 Tempo 中约 2029.6 ms 的 HTTP 根 Span 和约 2027.0 ms 的 JDBC 子 Span；运行镜像与 Deployment 注解均为 BAD SHA `54dfd4fded40c0317b7f3f2336f175155cba96fc`。

## 14. 哪些因为环境原因未能测试

没有独立 GPU 直通 Kind 的需求；Ollama 已在 Docker 中使用本机 RTX 4060。未安装 metrics-server，因此仅 `kubectl top` 形式的资源查询未能成功，Prometheus 等价资源指标已验证。其余 Scenario 的完整证据评测需逐一触发，结果不得由文档预设为通过。
