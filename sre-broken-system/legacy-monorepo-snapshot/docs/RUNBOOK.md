# Runbook

## 启动

1. `cd D:\SRE-Agent-platform\sre-agent-backend\sre-gateway`
2. `docker-compose up -d`
3. `cd D:\SRE-Agent-platform\sre-broken-system`
4. `./scripts/deploy-lab.ps1`
5. `curl.exe -X POST http://127.0.0.1:8000/v1/auth/tokens`
6. 设置返回 Token 为 `GATEWAY_API_KEY`，在 `sre-agent` 执行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8001`。
7. 在 `sre-agent-frontend` 执行 `npm run dev`。

## 健康检查

- `kubectl get pods -n sre-lab`
- `http://127.0.0.1:18080/actuator/health`
- `http://127.0.0.1:18081/health`、`:18082/health`、`:18083/health`
- `http://127.0.0.1:19090/-/ready`、`:13100/ready`、`:13200/ready`
- `http://127.0.0.1:8001/health`

## 场景与恢复

`./scripts/run-scenario.ps1 -Scenario SRE-001` 到 `SRE-007`。每次脚本都会先调用 `reset-lab.ps1`，也可单独运行该脚本恢复 normal。停止集群执行 `stop-lab.ps1`；完全重建执行 `deploy-lab.ps1`。

## 排障

- rollout 失败：查看 `kubectl -n sre-lab get pods` 和 Events；部署脚本遇非零退出会立即停止。
- Loki 空结果：检查 Alloy DaemonSet 是否挂载 `/var/log/pods`。
- Trace 空结果：检查 otel-collector 和 Tempo ready。
- Agent 503：缺少 `GATEWAY_API_KEY`；502：Gateway/Ollama 上游失败。
