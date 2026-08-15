<script setup>
/**
 * SRE 证据诊断问答页。
 * 浏览器只连接 Agent SSE；Gateway Token、Ollama、K8s 与数据库凭证均保留在后端。
 */
import { nextTick, onMounted, reactive, ref } from "vue";

// 允许通过 Vite 环境变量覆盖本地 Agent 地址。
const apiBaseUrl = import.meta.env.VITE_AGENT_API_BASE_URL || "http://127.0.0.1:8001";
const token = ref(localStorage.getItem("sre_agent_token") || "");
const currentUser = ref(null);
const authLoading = ref(true);
// 用户名可以提供本地默认值，但密码输入始终为空，避免源码或浏览器自动暴露密码。
const loginForm = reactive({ username: "admin", password: "", error: "", submitting: false });
const conversations = ref([]);
const currentConversationId = ref("");
const question = ref("");
const sending = ref(false);
const conversationRef = ref(null);
const messages = ref([{ id: 1, role: "assistant", content: "请描述故障现象。我会定位服务，并通过指标、日志、Trace、数据库和运行源码建立证据链。" }]);
const suggestions = [
  "为什么订单接口有时候很快，有时候特别慢？",
  "payment-service 为什么一直重启？",
  "为什么 order 很慢，但是 CPU 又不高？",
];
const phaseLabels = {
  START: "启动诊断", TRIAGE: "定位服务与症状", BASELINE_OBSERVATION: "采集健康、指标和日志基线",
  ANALYZE: "生成候选根因", INVESTIGATE: "专项调查与交叉验证", VERIFY: "检查独立证据数量",
  REPORT: "生成结构化报告", END: "诊断完成",
};

/** 等待 DOM 更新后滚动到底部，让流式事件始终可见。 */
async function scrollToBottom() {
  await nextTick();
  const element = conversationRef.value;
  if (element) element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
}

/** 把公开阶段、Tool 摘要和最终报告合并到消息；不显示隐藏思维链。 */
function applyEvent(message, event) {
  if (event.type === "conversation") currentConversationId.value = event.conversation_id;
  else if (event.type === "phase") message.phases.push(event.phase);
  else if (event.type === "tool") message.tools.push(event.record);
  else if (event.type === "final") { message.report = event.report; message.content = ""; }
  else if (event.type === "error") message.error = event.message || "诊断流发生未知错误";
}

/** 按空行拆分 SSE data 帧；stream 解码避免中文跨网络分片时乱码。 */
async function consumeEventStream(response, message) {
  if (!response.body) throw new Error("浏览器未提供可读取的 SSE 响应体");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const packet = await reader.read();
    buffer += decoder.decode(packet.value || new Uint8Array(), { stream: !packet.done });
    // ASGI/代理可能使用 CRLF，也可能使用 LF；同时兼容两种 SSE 帧分隔符。
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      applyEvent(message, JSON.parse(dataLine.slice(5).trim()));
      await scrollToBottom();
    }
    if (packet.done) break;
  }
}

/** 提交问题并创建一个由 SSE 持续更新的 assistant 消息。 */
async function sendQuestion() {
  const query = question.value.trim();
  if (!query || sending.value) return;
  sending.value = true;
  let diagnosis = null;
  try {
    messages.value.push({ id: Date.now(), role: "user", content: query });
    // 必须使用 reactive：若把普通对象 push 进 reactive 数组后仍修改原始引用，
    // Vue 不会逐帧触发渲染，只会在请求结束的其他状态更新时一次性显示结果。
    diagnosis = reactive({
      id: Date.now() + 1, role: "assistant", content: "正在建立诊断范围…",
      phases: [], tools: [], report: null, error: "",
    });
    messages.value.push(diagnosis);
    question.value = "";
    await scrollToBottom();
    const response = await fetch(`${apiBaseUrl}/api/agent/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token.value },
      body: JSON.stringify({
        message: query,
        conversation_id: currentConversationId.value || null,
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Agent 返回 HTTP ${response.status}`);
    }
    await consumeEventStream(response, diagnosis);
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : "未知请求错误";
    if (diagnosis) diagnosis.error = errorMessage;
  } finally {
    sending.value = false;
    await loadConversations();
    await scrollToBottom();
  }
}

/** 推荐问题和键盘发送都复用统一提交逻辑。 */
function useSuggestion(text) { if (!sending.value) { question.value = text; sendQuestion(); } }
function handleKeydown(event) { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendQuestion(); } }
function confidencePercent(value) { return `${Math.round((Number(value) || 0) * 100)}%`; }
/** 判断 Source Reference 是否能由浏览器直接打开；内部协议 URI 只展示文本。 */
function isHttpReference(reference) {
  return reference?.repository_url?.startsWith("https://");
}

/** 按 evidence_id 读取完整原文；再次点击同一按钮会折叠已经加载的结果。 */
async function loadEvidence(report, evidence) {
  if (evidence.raw) { evidence.raw = null; return; }
  if (evidence.loading) return;
  evidence.loading = true;
  try {
    const url = apiBaseUrl + "/api/agent/evidence/" + report.run_id + "/" + evidence.evidence_id;
    const response = await fetch(url, { headers: { "Authorization": "Bearer " + token.value } });
    if (!response.ok) throw new Error("Evidence Store 返回 HTTP " + response.status);
    evidence.raw = await response.json();
  } catch (error) {
    evidence.rawError = error instanceof Error ? error.message : "原始证据读取失败";
  } finally {
    evidence.loading = false;
  }
}
/** 登录成功后只把随机 Token 放入 localStorage，绝不缓存用户密码。 */
async function login() {
  loginForm.error = "";
  loginForm.submitting = true;
  try {
    const response = await fetch(apiBaseUrl + "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: loginForm.username.trim(), password: loginForm.password }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "登录失败");
    token.value = payload.access_token;
    localStorage.setItem("sre_agent_token", token.value);
    currentUser.value = payload.user;
    loginForm.password = "";
    await loadConversations();
  } catch (error) {
    loginForm.error = error instanceof Error ? error.message : "登录失败";
  } finally {
    loginForm.submitting = false;
  }
}

/** 页面刷新时必须调用 /me 重新验证 Token，不能只相信浏览器缓存。 */
async function restoreSession() {
  if (!token.value) { authLoading.value = false; return; }
  try {
    const response = await fetch(apiBaseUrl + "/api/auth/me", {
      headers: { "Authorization": "Bearer " + token.value },
    });
    if (!response.ok) throw new Error("Token 已失效");
    currentUser.value = await response.json();
    await loadConversations();
  } catch {
    token.value = "";
    currentUser.value = null;
    localStorage.removeItem("sre_agent_token");
  } finally {
    authLoading.value = false;
  }
}

/** 登录或诊断完成后加载轻量会话摘要，形成当前页面的持久化缓存。 */
async function loadConversations() {
  if (!token.value) return;
  const response = await fetch(apiBaseUrl + "/api/conversations", {
    headers: { "Authorization": "Bearer " + token.value },
  });
  if (response.status === 401) { await logout(false); return; }
  if (response.ok) conversations.value = await response.json();
}

/** 从服务端读取一个历史会话，并恢复用户问题与结构化诊断报告。 */
async function openConversation(conversationId) {
  if (!conversationId || sending.value) return;
  const response = await fetch(apiBaseUrl + "/api/conversations/" + conversationId, {
    headers: { "Authorization": "Bearer " + token.value },
  });
  if (!response.ok) return;
  const detail = await response.json();
  currentConversationId.value = detail.id;
  messages.value = detail.messages.flatMap((item) => {
    if (item.role === "user") {
      const content = item.content.message || "";
      return content ? [{ id: item.id, role: "user", content }] : [];
    }
    if (item.content.report) {
      return [{ id: item.id, role: "assistant", content: "", phases: [], tools: [], report: item.content.report, error: "" }];
    }
    // Tool Call/Tool Result 是 Conversation Store 中供上下文恢复和 Evidence
    // 回查使用的内部记录，不应该各自渲染成一个没有正文的 AI 消息。
    const error = item.message_type === "assistant" ? (item.content.error || "") : "";
    return error
      ? [{ id: item.id, role: "assistant", content: "", phases: [], tools: [], report: null, error }]
      : [];
  });
  await scrollToBottom();
}

/** 新建诊断只清空当前选择；首条问题由服务器自动创建并返回会话 ID。 */
function newConversation() {
  if (sending.value) return;
  currentConversationId.value = "";
  messages.value = [{ id: Date.now(), role: "assistant", content: "请描述新的故障现象。" }];
}

/** 注销时撤销服务端 Token，再清理浏览器身份与会话缓存。 */
async function logout(callServer = true) {
  if (callServer && token.value) {
    await fetch(apiBaseUrl + "/api/auth/logout", {
      method: "POST", headers: { "Authorization": "Bearer " + token.value },
    }).catch(() => {});
  }
  localStorage.removeItem("sre_agent_token");
  token.value = "";
  currentUser.value = null;
  conversations.value = [];
  currentConversationId.value = "";
}

onMounted(restoreSession);
</script>

<template>
  <section v-if="authLoading" class="auth-page">
    <p>正在恢复会话…</p>
  </section>

  <section v-else-if="!currentUser" class="auth-page">
    <form class="login-card" @submit.prevent="login">
      <p class="eyebrow">SRE AGENT</p>
      <h1>登录</h1>
      <p>登录后会自动恢复你的诊断会话。</p>
      <label>
        <span>用户名</span>
        <input v-model="loginForm.username" autocomplete="username" maxlength="80" required />
      </label>
      <label>
        <span>密码</span>
        <input v-model="loginForm.password" type="password" autocomplete="current-password" minlength="6" maxlength="256" required />
      </label>
      <button type="submit" :disabled="loginForm.submitting">
        {{ loginForm.submitting ? "登录中…" : "登录" }}
      </button>
      <p v-if="loginForm.error" class="login-error">{{ loginForm.error }}</p>
    </form>
  </section>

  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">S</span>
        <div><strong>SRE Agent</strong><small>Evidence Lab</small></div>
      </div>

      <section class="runtime-card">
        <div class="status-line"><span class="status-dot"></span><span>本地只读诊断链路</span></div>
        <dl>
          <div><dt>Frontend</dt><dd>:3000</dd></div>
          <div><dt>Agent</dt><dd>:8001</dd></div>
          <div><dt>Gateway</dt><dd>:8000</dd></div>
          <div><dt>Ollama</dt><dd>:11434</dd></div>
          <div><dt>Kind</dt><dd>sre-lab</dd></div>
        </dl>
      </section>

      <section class="tool-list">
        <p class="eyebrow">READ-ONLY EVIDENCE</p>
        <div><span class="tool-icon">K</span><span><b>Kubernetes</b><small>Pod · Event · Image · SHA</small></span></div>
        <div><span class="tool-icon">P</span><span><b>Prometheus</b><small>P95 · Error · Resource</small></span></div>
        <div><span class="tool-icon">L</span><span><b>Loki / Tempo</b><small>Logs · Trace</small></span></div>
        <div><span class="tool-icon">D</span><span><b>MySQL / Git</b><small>Slow SQL · Diff · Source</small></span></div>
      </section>
      <p class="sidebar-note">Tool 仅提供 GET / READ / SELECT / EXPLAIN。页面只展示调查步骤，不展示隐藏思维链。</p>
    </aside>

    <main class="chat-panel">
      <header class="chat-header">
        <div><p class="eyebrow">SRE AGENT</p><h1>故障诊断</h1></div>
        <div class="header-actions">
          <select
            :value="currentConversationId"
            aria-label="历史会话"
            :disabled="sending"
            @change="openConversation($event.target.value)"
          >
            <option value="">当前新会话</option>
            <option v-for="item in conversations" :key="item.id" :value="item.id">
              {{ item.title }} · {{ item.message_count }}
            </option>
          </select>
          <button :disabled="sending" @click="newConversation">新建</button>
          <button @click="logout(true)">退出 {{ currentUser.username }}</button>
        </div>
      </header>

      <section ref="conversationRef" class="conversation" aria-live="polite">
        <article v-for="message in messages" :key="message.id" class="message-row" :class="message.role">
          <div class="avatar">{{ message.role === "user" ? "你" : message.role === "error" ? "!" : "AI" }}</div>
          <div class="message-content diagnosis-content">
            <div v-if="message.content" class="bubble">{{ message.content }}</div>
            <section v-if="message.phases?.length && !message.report" class="progress-card">
              <p class="eyebrow">INVESTIGATION PROGRESS</p>
              <div v-for="phase in message.phases" :key="phase" class="progress-row"><span>✓</span><b>{{ phaseLabels[phase] || phase }}</b></div>
              <div v-if="sending" class="progress-row active"><span>·</span><b>正在读取下一项证据…</b></div>
            </section>

            <div v-if="message.report" class="report-grid">
              <section class="report-card conclusion-card">
                <div class="card-heading"><span>诊断结论</span><b>{{ confidencePercent(message.report.confidence) }}</b></div>
                <h2>{{ message.report.conclusion }}</h2>
                <p class="decision-summary">{{ message.report.decision_summary }}</p>
                <!-- 运行身份字段来自 K8s Pod 与 Service Catalog，前端不猜测任何版本。 -->
                <dl class="runtime-identity">
                  <div><dt>Affected Service</dt><dd>{{ message.report.service }}</dd></div>
                  <div><dt>Affected Pod</dt><dd>{{ message.report.affected_pod || "未定位到单 Pod" }}</dd></div>
                  <div><dt>Language</dt><dd>{{ message.report.language }}</dd></div>
                  <div><dt>Running Version</dt><dd><code>{{ message.report.running_version || "unknown" }}</code></dd></div>
                  <div><dt>Git SHA</dt><dd><code>{{ message.report.git_sha || "unknown" }}</code></dd></div>
                  <div><dt>Repository</dt><dd><code>{{ message.report.repository_url || "本地只读镜像" }}</code></dd></div>
                  <div><dt>Source</dt><dd><code>{{ message.report.source_code_location || "未定位" }}</code></dd></div>
                </dl>
                <p>{{ message.report.environment }} · {{ message.report.time_range }} · {{ message.report.context_compaction.strategy }}</p>
              </section>
              <section class="report-card chain-card">
                <div class="card-heading"><span>根因链</span></div>
                <div class="cause-chain">
                  <template v-for="(step, index) in message.report.root_cause_chain" :key="step">
                    <strong>{{ step }}</strong><i v-if="index < message.report.root_cause_chain.length - 1">↓</i>
                  </template>
                </div>
              </section>
              <section class="report-card evidence-section">
                <div class="card-heading"><span>证据</span><b>{{ message.report.evidence.length }} items</b></div>
                <div class="evidence-grid">
                  <article v-for="(evidence, index) in message.report.evidence" :key="evidence.tool_name + index" class="evidence-card">
                    <div><span>{{ evidence.source }}</span><code>{{ evidence.evidence_id }}</code></div>
                    <h3>{{ evidence.title }}</h3><p>{{ evidence.detail }}</p>
                    <div class="source-references">
                      <template v-for="reference in evidence.source_references" :key="reference.uri">
                        <a v-if="isHttpReference(reference)" :href="reference.repository_url" target="_blank" rel="noreferrer">{{ reference.label }}</a>
                        <code v-else>{{ reference.uri }}</code>
                      </template>
                    </div>
                    <button class="evidence-button" @click="loadEvidence(message.report, evidence)">
                      {{ evidence.loading ? "读取中…" : evidence.raw ? "收起原始证据" : "查看原始证据" }}
                    </button>
                    <pre v-if="evidence.raw" class="raw-evidence">{{ JSON.stringify(evidence.raw.result, null, 2) }}</pre>
                    <p v-if="evidence.rawError" class="evidence-error">{{ evidence.rawError }}</p>
                  </article>
                </div>
              </section>
              <section class="report-card fixes-card">
                <div class="card-heading"><span>修改方案</span></div>
                <ol><li v-for="fix in message.report.recommended_fix" :key="fix">{{ fix }}</li></ol>
              </section>
              <details class="report-card timeline-card">
                <summary>调查过程 · {{ message.report.investigation_timeline.length }} 次 Tool Call</summary>
                <div v-for="(record, index) in message.report.investigation_timeline" :key="record.tool_name + index" class="timeline-row">
                  <div><span>{{ index + 1 }}</span><code>{{ record.tool_name }}</code><b :class="{ failed: record.error }">{{ record.error ? "失败" : record.duration_ms + "ms" }}</b></div>
                  <pre>参数 {{ JSON.stringify(record.arguments, null, 2) }}
摘要 {{ record.error || record.result_summary }}</pre>
                </div>
              </details>
            </div>
            <div v-if="message.error" class="error-banner">诊断失败：{{ message.error }}</div>
          </div>
        </article>
      </section>

      <footer class="composer-area">
        <div class="suggestions">
          <button v-for="item in suggestions" :key="item" :disabled="sending" @click="useSuggestion(item)">{{ item }}</button>
        </div>
        <form class="composer" @submit.prevent="sendQuestion">
          <textarea v-model="question" rows="1" maxlength="20000" placeholder="描述故障现象" aria-label="问题" :disabled="sending" @keydown="handleKeydown"></textarea>
          <button type="submit" :disabled="sending || !question.trim()" aria-label="发送问题"><span>{{ sending ? "处理中" : "发送" }}</span><b>↗</b></button>
        </form>
        <p class="composer-hint">Enter 发送 · Shift + Enter 换行</p>
      </footer>
    </main>
  </div>
</template>
