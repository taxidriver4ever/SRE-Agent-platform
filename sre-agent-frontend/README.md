# SRE Agent Frontend

基于 Vue 3 和 Vite 的服务浏览与事件诊断控制台。页面以 Service 为入口、以 Incident 为诊断视角，负责登录、服务健康概览、Finding/Incident 聚合、依赖拓扑、实时诊断进度、证据引用和根因报告展示；模型调用、凭证与所有诊断工具均留在后端。

## 功能

- Service Overview 展示状态、P95、Error Rate、CPU、Memory、Active Findings 和 Active Incidents。
- Service Detail 展示健康摘要、多个 Finding、关联 Incident、部署信息、上下游依赖和 Service Graph。
- 数据关系采用 `Service -> Finding -> Incident -> Root Cause`；一个 Incident 可关联多个 Service 和多个 Finding。
- 使用 Agent Bearer Token 登录并恢复 Diagnosis Session；Service、Pod 和自然语言问题统一调用 `POST /api/diagnoses`。
- Incident Detail 展示动态涉及服务、疑似根因服务、诊断进度、Related Findings、Evidence、Timeline 与 Recommendations。
- 历史记录只渲染用户和助手的可见消息，不为内部 Tool Call / Tool Result 创建空 AI 气泡。
- 后端在 MySQL 中持久化会话、Evidence 和压缩状态，刷新页面不依赖浏览器内存恢复历史。

## Diagnosis Session 与 SSE 展示

前端不自行推导根因或拼接故障链，只消费后端持久化领域事件：

| 事件 | 页面行为 |
| --- | --- |
| `diagnosis.started` | Session 进入 `INVESTIGATING` |
| `phase.changed` | 更新公开诊断阶段，不展示内部思维链 |
| `step.completed` / `step.failed` | 更新 Investigation Timeline；单 Tool 失败不会终止整个 Session |
| `evidence.created` | 提示 Evidence Store 已新增真实工具证据 |
| `graph.updated` | 使用后端 Node/Edge 重绘跨服务故障链 |
| `root_cause.generated` | 渲染结构化 Root Cause 与 Confidence |
| `diagnosis.completed` / `diagnosis.failed` | 刷新完整 Diagnosis 聚合 |

事件流支持 `Last-Event-ID`，刷新或断线后仍可从 MySQL 事件表继续回放。页面最终以 `GET /api/diagnoses/{id}` 返回的 Steps、Evidence、Graph 和 Root Cause 为准。

## 本地开发

要求 Node.js 22+。

```powershell
npm install
Copy-Item .env.example .env.local
npm run dev
```

默认地址为 `http://127.0.0.1:3000`。环境变量：

```text
VITE_AGENT_API_BASE_URL=http://127.0.0.1:8001
VITE_UI_PREVIEW=false
```

修改 `.env.local` 后需要重启 Vite。Agent API 必须先启动，并允许来自前端地址的 CORS 请求。

仅做本地 UI 评审时可以临时设置 `VITE_UI_PREVIEW=true`。该开关默认关闭，不应在正式环境启用；正式运行仍需通过 Agent 登录。

## 构建

```powershell
npm run build
npm run preview
```

生产文件输出到 `dist/`。当前项目没有前端测试脚本，提交前至少执行 `npm run build` 完成模板、模块和打包校验。

## 安全与数据边界

- 浏览器只保存 Agent 登录 Token；Gateway Token、Provider Key、MySQL 和 Kubernetes 凭证不会进入前端。
- 第一版前端固定发送 `project_id=sre-lab`；它只用于选择服务端白名单项目，前端不能提交 namespace、repo、allowed paths 或 Tool 凭证。
- Diagnosis ID 由后端创建并按当前用户校验，前端不能借 ID 读取其他用户的 Session。
- 页面只显示后端公开的阶段和工具摘要，不显示模型隐藏推理。
- 完整 Tool Result 和 Evidence 通过受保护的后端接口按需读取。

## 常见问题

- 登录后立即退出：检查 `GET /api/auth/me` 是否返回 200，以及 Agent `.env` 中的初始账号配置。
- 诊断停在“正在建立诊断范围”：查看浏览器 Network 中 SSE 连接及 Agent 日志。
- 页面出现空 AI 头像：历史数据应只渲染 `user` / `assistant` 且内容非空的消息；内部工具消息由进度区展示。
- 无法连接后端：确认 `VITE_AGENT_API_BASE_URL`、Agent 端口和 CORS 配置一致。

返回 [项目总览](../README.md)。
