# SRE Agent Frontend

基于 Vue 3 和 Vite 的 SRE 诊断控制台。它负责登录、会话列表、实时诊断进度、工具活动摘要、证据引用和结构化报告展示；模型调用、凭证与所有诊断工具均留在后端。

## 功能

- 使用 Agent Bearer Token 登录并恢复会话。
- 调用 `POST /api/agent/chat/stream`，消费 `conversation`、`intent`、`phase`、`tool`、`message`、`final` 和错误事件。
- 展示八阶段调查进度、根因、置信度、证据、影响范围与建议动作。
- 历史记录只渲染用户和助手的可见消息，不为内部 Tool Call / Tool Result 创建空 AI 气泡。
- 后端在 MySQL 中持久化会话、Evidence 和压缩状态，刷新页面不依赖浏览器内存恢复历史。

## 意图与 SSE 展示

前端不自行猜测意图，只消费后端已校验的 SSE 事件：

| 事件 | 页面行为 |
| --- | --- |
| `intent` | 保存本轮分类结果，不触发任何浏览器侧工具逻辑 |
| `phase: SYSTEM_SCAN` | 显示“系统整体扫描”进度 |
| `phase` / `tool` | 更新诊断阶段和只读工具摘要 |
| `message` | 直接展示澄清问题或非运维提示，不创建空报告卡片 |
| `final` | 渲染证据、根因链、置信度和修复建议 |
| `error` | 显示诊断失败原因 |

`NEED_CLARIFICATION` 和 `OUT_OF_SCOPE` 回复会作为普通 assistant 消息写入 Conversation Store。重新打开历史会话时，页面会恢复其正文，同时继续过滤内部 Tool Call / Tool Result，避免出现没有消息的 AI 头像。

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
```

修改 `.env.local` 后需要重启 Vite。Agent API 必须先启动，并允许来自前端地址的 CORS 请求。

## 构建

```powershell
npm run build
npm run preview
```

生产文件输出到 `dist/`。当前项目没有前端测试脚本，提交前至少执行 `npm run build` 完成模板、模块和打包校验。

## 安全与数据边界

- 浏览器只保存 Agent 登录 Token；Gateway Token、Provider Key、MySQL 和 Kubernetes 凭证不会进入前端。
- 第一版前端固定发送 `project_id=sre-lab`；它只用于选择服务端白名单项目，前端不能提交 namespace、repo、allowed paths 或 Tool 凭证。
- Conversation ID 由后端创建并按当前用户校验，前端不能借 ID 读取其他用户的会话。
- 页面只显示后端公开的阶段和工具摘要，不显示模型隐藏推理。
- 完整 Tool Result 和 Evidence 通过受保护的后端接口按需读取。

## 常见问题

- 登录后立即退出：检查 `GET /api/auth/me` 是否返回 200，以及 Agent `.env` 中的初始账号配置。
- 诊断停在“正在建立诊断范围”：查看浏览器 Network 中 SSE 连接及 Agent 日志。
- 页面出现空 AI 头像：历史数据应只渲染 `user` / `assistant` 且内容非空的消息；内部工具消息由进度区展示。
- 无法连接后端：确认 `VITE_AGENT_API_BASE_URL`、Agent 端口和 CORS 配置一致。

返回 [项目总览](../README.md)。
