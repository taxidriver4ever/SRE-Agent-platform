# SRE Agent Frontend

Vue 3 + Vite 编写的极简黑白 SRE 诊断问答页。页面调用受登录 Token 保护的
`POST /api/agent/chat/stream`，展示公开工作流、证据引用和结构化报告。
Gateway Token、Ollama 与 K8s 凭证都不会进入浏览器。

## 启动

```powershell
npm install
npm run dev
```

默认访问 `http://127.0.0.1:3000`，Agent 默认地址为 `http://127.0.0.1:8001`。

如果后端地址不同，将 `.env.example` 复制为 `.env.local`，修改
`VITE_AGENT_API_BASE_URL`。页面只持久化登录 Token；Agent 的 Active Context、
Evidence 和压缩状态由后端 RAM 模块管理。
