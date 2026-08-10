# SRE Agent Frontend

Vue 3 + Vite 编写的极简黑白 SRE 诊断问答页。页面调用受登录 Token 保护的
`POST /api/agent/chat/stream`，展示公开工作流、证据引用和结构化报告。
Gateway Token、MinIO 密钥、Ollama 与 K8s 凭证都不会进入浏览器。

## 启动

```powershell
npm install
npm run dev
```

默认访问 `http://127.0.0.1:3000`，Agent 默认地址为 `http://127.0.0.1:8001`。

如果后端地址不同，将 `.env.example` 复制为 `.env.local`，修改
`VITE_AGENT_API_BASE_URL`。如后端修改了超长文本阈值，同时修改
`VITE_LARGE_TEXT_THRESHOLD_BYTES`。

文件上传使用短时效预签名 URL 直接 PUT Docker MinIO，不经过 Agent 文件代理。
选中的普通文件会先上传；粘贴内容超过阈值时，再转换为 `.log` 上传；所有对象
完成校验后才提交诊断请求。页面只持久化登录 Token，不持久化预签名 URL。
