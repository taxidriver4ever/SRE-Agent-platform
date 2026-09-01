<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import ServiceGraph from "./components/ServiceGraph.vue";
import { getService, services, statusLabel } from "./data/services";

const apiBaseUrl = import.meta.env.VITE_AGENT_API_BASE_URL || "http://127.0.0.1:8001";
const projectId = "sre-lab";
const uiPreview = import.meta.env.VITE_UI_PREVIEW === "true";
const token = ref(localStorage.getItem("sre_agent_token") || "");
const currentUser = ref(uiPreview ? { username: "preview" } : null);
const authLoading = ref(!uiPreview);
const loginForm = reactive({ username: "admin", password: "", error: "", submitting: false });
const serviceCatalog = ref(uiPreview ? services : []);
const serviceSearch = ref("");
const statusFilter = ref("all");
const servicePods = ref([]);
const podDetail = ref(null);
const resourceLoading = ref(false);
const conversations = ref([]);
const chatList = ref(null);
const route = reactive({ name: "services", serviceId: "", podName: "" });
const chat = reactive({ conversationId: "", messages: [], input: "", selectedServices: [], sending: false, error: "" });
const quick = reactive({ targetType: "SERVICE", targetName: "", running: false, phases: [], tools: [], result: null, error: "" });

const phaseLabels = {
  START: "准备诊断范围", SYSTEM_SCAN: "系统整体扫描", TRIAGE: "定位服务与症状",
  BASELINE_OBSERVATION: "Metrics 基线分析", ANALYZE: "生成候选根因",
  INVESTIGATE: "Trace / Logs / K8s 调查", VERIFY: "证据交叉验证",
  REPORT: "生成根因报告", END: "诊断完成",
};
const displayServices = computed(() => serviceCatalog.value);
const filteredServices = computed(() => {
  const keyword = serviceSearch.value.trim().toLowerCase();
  return displayServices.value.filter((service) => {
    const statusMatch = statusFilter.value === "all" || service.status === statusFilter.value;
    const textMatch = !keyword || `${service.name} ${service.description} ${service.owner}`.toLowerCase().includes(keyword);
    return statusMatch && textMatch;
  });
});
const statusSummary = computed(() => ({
  healthy: displayServices.value.filter((item) => item.status === "healthy").length,
  warning: displayServices.value.filter((item) => item.status === "warning").length,
  critical: displayServices.value.filter((item) => item.status === "critical").length,
}));
const currentService = computed(() => displayServices.value.find((item) => item.id === route.serviceId) || getService(route.serviceId));
const catalogGraph = computed(() => {
  const known = new Set(displayServices.value.map((item) => item.id));
  return {
    nodes: displayServices.value.map((item) => ({ id: `service:${item.id}`, type: "SERVICE", name: item.id, status: item.status === "healthy" ? "HEALTHY" : item.status === "critical" ? "AFFECTED" : "UNKNOWN" })),
    edges: displayServices.value.flatMap((item) => (item.dependencies || []).filter((id) => known.has(id)).map((id) => ({ id: `${item.id}-${id}`, source: `service:${item.id}`, target: `service:${id}`, relation: "DEPENDS_ON" }))),
  };
});
const quickReport = computed(() => quick.result?.report || null);
const quickRoot = computed(() => quick.result?.root_cause || null);
const quickGraph = computed(() => quick.result?.graph || { nodes: [], edges: [] });
const quickServices = computed(() => quick.result?.affected_services || [quick.targetName].filter(Boolean));

function authHeaders(json = false) { return { ...(json ? { "Content-Type": "application/json" } : {}), Authorization: `Bearer ${token.value}` }; }
function navigate(path) { window.location.hash = path; }
function openService(id) { navigate(`/services/${id}`); }
function openPod(name) { navigate(`/pods/${encodeURIComponent(name)}`); }
function openEventDiagnosis() { navigate("/diagnosis"); }
function parseRoute() {
  const segments = (window.location.hash.replace(/^#\/?/, "") || "services").split("/").filter(Boolean);
  route.serviceId = ""; route.podName = "";
  if (segments[0] === "services" && segments[1]) {
    route.name = "service-detail"; route.serviceId = segments[1]; resetQuick(); loadServicePods(route.serviceId);
  } else if (segments[0] === "pods" && segments[1]) {
    route.name = "pod-detail"; route.podName = decodeURIComponent(segments[1]); resetQuick(); loadPodDetail(route.podName);
  } else if (segments[0] === "diagnosis") route.name = "event-diagnosis";
  else route.name = "services";
  window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "auto" }));
}
function resetQuick() { quick.targetName = ""; quick.running = false; quick.phases = []; quick.tools = []; quick.result = null; quick.error = ""; }
function toggleService(id) { const index = chat.selectedServices.indexOf(id); if (index >= 0) chat.selectedServices.splice(index, 1); else chat.selectedServices.push(id); }
function normalizeHistoryMessage(item) {
  const content = item.content || {};
  if (item.message_type === "tool_call" || item.message_type === "tool_result") return null;
  if (item.role === "user") return { id: item.id, role: "user", text: content.message || String(content) };
  if (content.report) return { id: item.id, role: "assistant", report: content.report, text: content.report.decision_summary || content.report.conclusion };
  if (content.root_cause || content.decision_summary) return { id: item.id, role: "assistant", report: content, text: content.decision_summary || content.conclusion };
  return { id: item.id, role: "assistant", text: content.message || content.error || "诊断消息" };
}
async function consumeSse(response, handler) {
  if (!response.body) throw new Error("浏览器未提供可读取的 SSE 响应体");
  const reader = response.body.getReader(); const decoder = new TextDecoder("utf-8"); let buffer = "";
  while (true) {
    const packet = await reader.read(); buffer += decoder.decode(packet.value || new Uint8Array(), { stream: !packet.done });
    const frames = buffer.split(/\r?\n\r?\n/); buffer = frames.pop() || "";
    for (const frame of frames) { const data = frame.split("\n").find((line) => line.startsWith("data:")); if (data) handler(JSON.parse(data.slice(5).trim())); }
    if (packet.done) break;
  }
}
function applyChatEvent(event, message) {
  if (event.type === "conversation") chat.conversationId = event.conversation_id;
  else if (event.type === "intent") message.intent = event.intent;
  else if (event.type === "phase" && !message.phases.includes(event.phase)) message.phases.push(event.phase);
  else if (event.type === "tool") message.tools.push(event.record);
  else if (event.type === "final") { message.report = event.report; message.text = event.report?.decision_summary || event.report?.conclusion || "诊断完成"; }
  else if (event.type === "message") { message.text = event.message; message.intent = event.intent; }
  else if (event.type === "error") message.error = event.message;
}
async function sendChat() {
  const text = chat.input.trim(); if (!text || chat.sending) return;
  chat.error = ""; chat.messages.push({ id: crypto.randomUUID(), role: "user", text });
  const assistant = reactive({ id: crypto.randomUUID(), role: "assistant", text: "", intent: "", phases: [], tools: [], report: null, error: "" });
  chat.messages.push(assistant); chat.input = ""; chat.sending = true;
  await nextTick(); chatList.value?.scrollTo({ top: chatList.value.scrollHeight, behavior: "smooth" });
  if (uiPreview) { assistant.intent = "SPECIFIC_INCIDENT"; assistant.phases = ["TRIAGE", "BASELINE_OBSERVATION"]; assistant.text = "预览模式不会请求后端。登录实际环境后，Agent 会保留这段会话记忆并继续诊断。"; chat.sending = false; return; }
  try {
    const response = await fetch(`${apiBaseUrl}/api/agent/chat/stream`, { method: "POST", headers: authHeaders(true), body: JSON.stringify({ message: text, conversation_id: chat.conversationId || null, project_id: projectId, selected_services: chat.selectedServices }) });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
    await consumeSse(response, (event) => applyChatEvent(event, assistant)); await loadConversations();
  } catch (error) { assistant.error = error instanceof Error ? error.message : "诊断请求失败"; }
  finally { chat.sending = false; }
}
async function createConversation() {
  chat.messages = []; chat.input = ""; chat.error = "";
  if (uiPreview) { chat.conversationId = `preview-${Date.now()}`; return; }
  const response = await fetch(`${apiBaseUrl}/api/conversations`, { method: "POST", headers: authHeaders(true), body: JSON.stringify({ title: "新事件诊断" }) });
  if (!response.ok) { chat.error = "创建会话失败"; return; }
  const item = await response.json(); chat.conversationId = item.id; await loadConversations();
}
async function openConversation(id) {
  openEventDiagnosis(); if (uiPreview) return;
  const response = await fetch(`${apiBaseUrl}/api/conversations/${id}`, { headers: authHeaders() });
  if (!response.ok) { chat.error = "历史会话读取失败"; return; }
  const detail = await response.json(); chat.conversationId = id; chat.messages = (detail.messages || []).map(normalizeHistoryMessage).filter(Boolean);
}
async function loadConversations() {
  if (!token.value || uiPreview) return;
  const response = await fetch(`${apiBaseUrl}/api/conversations`, { headers: authHeaders() });
  if (response.status === 401) { await logout(false); return; }
  if (response.ok) conversations.value = await response.json();
}
function applyQuickEvent(event) {
  if (event.type === "phase" && !quick.phases.includes(event.phase)) quick.phases.push(event.phase);
  else if (event.type === "tool") quick.tools.push(event.record);
  else if (event.type === "final") quick.result = event.result;
  else if (event.type === "error") quick.error = event.message;
}
async function runQuickDiagnosis(type, name) {
  if (quick.running) return;
  quick.targetType = type; quick.targetName = name; quick.phases = []; quick.tools = []; quick.result = null; quick.error = ""; quick.running = true;
  if (uiPreview) {
    const related = type === "SERVICE" ? [name, ...(currentService.value.dependencies || []).slice(0, 2)] : [name];
    quick.phases = Object.keys(phaseLabels);
    quick.result = { affected_services: related, root_cause: { title: `${name} 下游依赖响应异常`, description: `${name} 请求延迟升高 → 下游调用超时 → 错误率上升`, confidence: .86, root_resource: { name: related.at(-1), type: "SERVICE" }, recommendations: ["检查下游连接池与超时配置", "核对最近部署及资源水位"] }, report: { decision_summary: "已完成一次性快速诊断。", root_cause_chain: [`${name} 延迟升高`, "下游调用超时", "错误率上升"], evidence: [] }, graph: catalogGraph.value };
    quick.running = false; return;
  }
  try {
    const response = await fetch(`${apiBaseUrl}/api/diagnoses/quick/stream`, { method: "POST", headers: authHeaders(true), body: JSON.stringify({ question: `快速诊断 ${type === "POD" ? "Pod" : "服务"} ${name} 的当前异常，并沿依赖链定位根因。`, target: { type, name, namespace: "sre-lab" }, project_id: projectId }) });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
    await consumeSse(response, applyQuickEvent);
  } catch (error) { quick.error = error instanceof Error ? error.message : "快速诊断失败"; }
  finally { quick.running = false; }
}
async function loadServices() {
  if (!token.value || uiPreview) return;
  const response = await fetch(`${apiBaseUrl}/api/services`, { headers: authHeaders() }); if (!response.ok) return;
  const payload = await response.json(); serviceCatalog.value = (payload.items || []).map((service) => ({ ...service, p95: service.metrics?.p95_ms == null ? "—" : `${service.metrics.p95_ms} ms`, errorRate: service.metrics?.error_rate == null ? "—" : `${service.metrics.error_rate}%`, cpu: service.metrics?.cpu_percent || 0, memory: service.metrics?.memory_percent || 0, updatedAt: service.updated_at || "暂无运行快照", version: service.version || "—", deployedAt: service.deployed_at || "暂无部署记录" }));
}
async function loadServicePods(serviceId) {
  servicePods.value = []; if (!token.value || !serviceId || uiPreview) return; resourceLoading.value = true;
  try { const response = await fetch(`${apiBaseUrl}/api/services/${serviceId}/pods`, { headers: authHeaders() }); if (response.ok) servicePods.value = (await response.json()).pods || []; }
  finally { resourceLoading.value = false; }
}
async function loadPodDetail(name) {
  podDetail.value = null; if (!token.value || !name || uiPreview) return; resourceLoading.value = true;
  try { const response = await fetch(`${apiBaseUrl}/api/pods/${encodeURIComponent(name)}`, { headers: authHeaders() }); if (response.ok) podDetail.value = await response.json(); }
  finally { resourceLoading.value = false; }
}
async function login() {
  loginForm.error = ""; loginForm.submitting = true;
  try { const response = await fetch(`${apiBaseUrl}/api/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: loginForm.username.trim(), password: loginForm.password }) }); const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.detail || "登录失败"); token.value = payload.access_token; localStorage.setItem("sre_agent_token", token.value); currentUser.value = payload.user; await Promise.all([loadServices(), loadConversations()]); }
  catch (error) { loginForm.error = error instanceof Error ? error.message : "登录失败"; }
  finally { loginForm.submitting = false; }
}
async function restoreSession() {
  if (!token.value) { authLoading.value = false; return; }
  try { const response = await fetch(`${apiBaseUrl}/api/auth/me`, { headers: authHeaders() }); if (!response.ok) throw new Error("expired"); currentUser.value = await response.json(); await Promise.all([loadServices(), loadConversations()]); }
  catch { await logout(false); } finally { authLoading.value = false; }
}
async function logout(callServer = true) { if (callServer && token.value) await fetch(`${apiBaseUrl}/api/auth/logout`, { method: "POST", headers: authHeaders() }).catch(() => {}); localStorage.removeItem("sre_agent_token"); token.value = ""; currentUser.value = null; conversations.value = []; }
function confidence(value) { return `${Math.round((Number(value) || 0) * 100)}%`; }

onMounted(() => { parseRoute(); window.addEventListener("hashchange", parseRoute); if (!uiPreview) restoreSession(); });
onBeforeUnmount(() => window.removeEventListener("hashchange", parseRoute));
</script>

<template>
  <section v-if="authLoading" class="auth-page"><p>正在连接 SRE Console…</p></section>
  <section v-else-if="!currentUser" class="auth-page"><form class="login-card" @submit.prevent="login"><div class="login-mark">S</div><p class="eyebrow">SRE OPERATIONS CONSOLE</p><h1>进入服务诊断平台</h1><p>浏览服务健康状态，基于证据链开展跨服务诊断。</p><label><span>用户名</span><input v-model="loginForm.username" autocomplete="username" required /></label><label><span>密码</span><input v-model="loginForm.password" type="password" autocomplete="current-password" required /></label><button type="submit" :disabled="loginForm.submitting">{{ loginForm.submitting ? "正在验证…" : "登录 Console" }}</button><p v-if="loginForm.error" class="login-error">{{ loginForm.error }}</p></form></section>
  <div v-else class="console-shell monochrome">
    <aside class="console-sidebar"><button class="brand" @click="navigate('/services')"><span class="brand-mark">S</span><span><strong>SRE Console</strong><small>Service Intelligence</small></span></button><nav class="primary-nav"><button :class="{ active: route.name === 'services' || route.name === 'service-detail' }" @click="navigate('/services')"><span class="nav-icon">□</span><span>服务目录</span><b>{{ displayServices.length }}</b></button><button :class="{ active: route.name === 'event-diagnosis' }" @click="openEventDiagnosis"><span class="nav-icon">◇</span><span>事件诊断</span><b>{{ conversations.length }}</b></button></nav><section class="environment-card"><div><span class="live-dot"></span><b>sre-lab</b><small>CONNECTED</small></div><dl><div><dt>Agent</dt><dd>:8001</dd></div><div><dt>Policy</dt><dd>READ ONLY</dd></div></dl></section><section class="recent-runs"><div class="sidebar-heading"><span>历史对话</span><b>{{ conversations.length }}</b></div><button v-for="item in conversations.slice(0, 6)" :key="item.id" @click="openConversation(item.id)"><span>{{ item.title }}</span><small>{{ item.message_count }} messages · {{ item.updated_at }}</small></button><p v-if="!conversations.length">暂无历史对话</p></section><div class="user-panel"><span>{{ currentUser.username?.slice(0, 1)?.toUpperCase() }}</span><div><b>{{ currentUser.username }}</b><small>Operator</small></div><button title="退出" @click="logout(true)">↗</button></div></aside>
    <main class="console-main">
      <header class="topbar"><div><p class="breadcrumb">SRE-LAB / {{ route.name.toUpperCase() }}</p><h1>{{ route.name === 'services' ? '服务目录' : route.name === 'service-detail' ? currentService.name : route.name === 'pod-detail' ? route.podName : '事件诊断' }}</h1></div><div class="topbar-meta"><span class="snapshot-dot"></span><span>READ ONLY</span><b>30s refresh</b></div></header>
      <section v-if="route.name === 'services'" class="page-content overview-page catalog-only"><div class="health-strip"><button :class="{ selected: statusFilter === 'all' }" @click="statusFilter = 'all'"><small>All Services</small><strong>{{ displayServices.length }}</strong><span>应用服务</span></button><button :class="{ selected: statusFilter === 'healthy' }" @click="statusFilter = 'healthy'"><small>Healthy</small><strong>{{ statusSummary.healthy }}</strong><span>运行正常</span></button><button :class="{ selected: statusFilter === 'warning' }" @click="statusFilter = 'warning'"><small>Warning</small><strong>{{ statusSummary.warning }}</strong><span>需要关注</span></button><button :class="{ selected: statusFilter === 'critical' }" @click="statusFilter = 'critical'"><small>Critical</small><strong>{{ statusSummary.critical }}</strong><span>立即处理</span></button></div><div class="catalog-toolbar"><label><span>⌕</span><input v-model="serviceSearch" placeholder="搜索服务、Owner 或职责" /></label><span>{{ filteredServices.length }} services</span></div><div class="service-grid"><button v-for="service in filteredServices" :key="service.id" class="service-card" @click="openService(service.id)"><div class="service-card-head"><span class="service-glyph">{{ service.name.slice(0, 2).toUpperCase() }}</span><span class="status-badge" :class="service.status"><i></i>{{ statusLabel(service.status) }}</span></div><h3>{{ service.name }}</h3><p>{{ service.description }}</p><div class="metric-grid"><div><small>P95 LATENCY</small><b>{{ service.p95 }}</b></div><div><small>ERROR RATE</small><b>{{ service.errorRate }}</b></div><div><small>CPU</small><b>{{ service.cpu }}%</b><i><span :style="{ width: service.cpu + '%' }"></span></i></div><div><small>MEMORY</small><b>{{ service.memory }}%</b><i><span :style="{ width: service.memory + '%' }"></span></i></div></div><footer><span>更新于 {{ service.updatedAt }}</span><b>查看服务 →</b></footer></button></div></section>
      <section v-else-if="route.name === 'service-detail'" class="page-content detail-page"><button class="back-button" @click="navigate('/services')">← 返回服务目录</button><div class="service-hero"><div class="service-identity"><span class="service-glyph large">{{ currentService.name.slice(0, 2).toUpperCase() }}</span><div><span class="status-badge" :class="currentService.status"><i></i>{{ statusLabel(currentService.status) }}</span><h2>{{ currentService.name }}</h2><p>{{ currentService.description }}</p></div></div><button class="primary-action" :disabled="quick.running" @click="runQuickDiagnosis('SERVICE', currentService.id)">{{ quick.running ? '诊断中…' : '开始快速诊断' }}</button></div>
        <section v-if="quick.running || quick.result || quick.error" class="panel quick-diagnosis"><div class="panel-heading"><div><p class="eyebrow">STATELESS QUICK DIAGNOSIS</p><h3>即时因果链</h3></div><span>无对话 · 无记忆</span></div><div v-if="quick.running" class="quick-progress"><b>正在分析 {{ quick.targetName }}</b><span>{{ phaseLabels[quick.phases.at(-1)] || '连接证据源' }}</span><i><span :style="{ width: Math.max(8, quick.phases.length / Object.keys(phaseLabels).length * 100) + '%' }"></span></i></div><div v-if="quick.error" class="incident-error"><b>快速诊断失败</b><span>{{ quick.error }}</span></div><div v-if="quick.result" class="quick-result"><div class="quick-summary"><article><small>SUSPECTED ROOT CAUSE</small><h3>{{ quickRoot?.title }}</h3><p>{{ quickRoot?.description }}</p></article><div><small>CONFIDENCE</small><strong>{{ confidence(quickRoot?.confidence) }}</strong></div></div><div class="cause-chain"><template v-for="(step, index) in quickReport?.root_cause_chain || []" :key="step"><span>{{ step }}</span><i v-if="index < quickReport.root_cause_chain.length - 1">→</i></template></div><div class="quick-grid"><div><p class="eyebrow">SERVICE DEPENDENCY GRAPH</p><ServiceGraph compact :graph="quickGraph" :involved-services="quickServices" :root-cause-service="quickRoot?.root_resource?.name" /></div><div><p class="eyebrow">EXECUTED EVIDENCE STEPS</p><ol class="quick-tools"><li v-for="(tool, index) in quick.tools" :key="index"><b>{{ tool.tool_name }}</b><span>{{ tool.result_summary || tool.error || '证据已采集' }}</span></li></ol><p class="eyebrow">RECOMMENDATIONS</p><ol class="quick-tools"><li v-for="item in quickRoot?.recommendations || []" :key="item"><span>{{ item }}</span></li></ol></div></div></div></section>
        <div class="detail-layout"><div class="detail-primary"><section class="panel metrics-panel"><div class="panel-heading"><div><p class="eyebrow">HEALTH SUMMARY</p><h3>Metrics 概览</h3></div><span>最近 30 分钟</span></div><div class="large-metrics"><div><small>P95 LATENCY</small><strong>{{ currentService.p95 }}</strong></div><div><small>ERROR RATE</small><strong>{{ currentService.errorRate }}</strong></div><div><small>CPU USAGE</small><strong>{{ currentService.cpu }}%</strong></div><div><small>MEMORY</small><strong>{{ currentService.memory }}%</strong></div></div></section><section class="panel pods-panel"><div class="panel-heading"><div><p class="eyebrow">KUBERNETES</p><h3>Pods</h3></div><span>{{ resourceLoading ? '读取中…' : servicePods.length + ' pods' }}</span></div><div v-if="servicePods.length" class="pod-list"><button v-for="pod in servicePods" :key="pod" @click="openPod(pod)"><span class="live-dot"></span><div><b>{{ pod }}</b><small>sre-lab · 实时发现</small></div><i>→</i></button></div><div v-else class="empty-state">{{ resourceLoading ? '正在读取 Pod…' : '当前未发现 Pod' }}</div></section><section class="panel graph-panel"><div class="panel-heading"><div><p class="eyebrow">SERVICE MAP</p><h3>依赖关系</h3></div></div><ServiceGraph :graph="catalogGraph" :focus-service="currentService.id" /></section></div><aside class="detail-aside"><section class="panel deployment-panel"><div class="panel-heading"><div><p class="eyebrow">DEPLOYMENT</p><h3>最近部署</h3></div></div><dl><div><dt>Version</dt><dd><code>{{ currentService.version }}</code></dd></div><div><dt>Runtime</dt><dd>{{ currentService.runtime }}</dd></div><div><dt>Owner</dt><dd>{{ currentService.owner }}</dd></div><div><dt>Deployed</dt><dd>{{ currentService.deployedAt }}</dd></div></dl></section><section class="panel dependency-list"><div class="panel-heading"><div><p class="eyebrow">DEPENDENCIES</p><h3>上下游服务</h3></div></div><div><small>UPSTREAM</small><button v-for="item in currentService.upstreams" :key="item" @click="openService(item)">{{ item }} →</button></div><div><small>DOWNSTREAM</small><button v-for="item in currentService.dependencies" :key="item" @click="openService(item)">{{ item }} →</button></div></section></aside></div></section>
      <section v-else-if="route.name === 'pod-detail'" class="page-content detail-page"><button class="back-button" @click="navigate('/services')">← 返回服务目录</button><div class="service-hero"><div class="service-identity"><span class="service-glyph large">PD</span><div><span class="status-badge warning"><i></i>Kubernetes Pod</span><h2>{{ route.podName }}</h2><p>Pod 只是快速诊断起点，证据可沿依赖关系扩展。</p></div></div><button class="primary-action" :disabled="quick.running" @click="runQuickDiagnosis('POD', route.podName)">{{ quick.running ? '诊断中…' : '快速诊断 Pod' }}</button></div><section v-if="quick.running || quick.result || quick.error" class="panel quick-diagnosis"><div class="panel-heading"><div><p class="eyebrow">STATELESS QUICK DIAGNOSIS</p><h3>即时诊断结果</h3></div><span>无对话 · 无记忆</span></div><div v-if="quick.running" class="empty-state">正在生成完整因果链…</div><div v-if="quick.error" class="incident-error">{{ quick.error }}</div><div v-if="quick.result" class="quick-summary"><article><small>ROOT CAUSE</small><h3>{{ quickRoot?.title }}</h3><p>{{ quickRoot?.description }}</p></article><strong>{{ confidence(quickRoot?.confidence) }}</strong></div></section><section class="panel"><div class="panel-heading"><div><p class="eyebrow">POD OVERVIEW</p><h3>运行时详情</h3></div></div><pre v-if="podDetail" class="pod-raw">{{ JSON.stringify(podDetail.data, null, 2) }}</pre><div v-else class="empty-state">Pod 数据暂不可用</div></section></section>
      <section v-else class="page-content event-page"><div class="event-intro"><div><p class="eyebrow">INCIDENT DIAGNOSIS</p><h2>描述现象，持续诊断</h2><p>可不选服务、选择一个或多个服务作为调查起点；Agent 会保留意图、记忆与压缩上下文，并可沿证据自动扩展范围。</p></div><button class="primary-action" @click="createConversation">＋ 创建新对话</button></div><div class="event-layout"><aside class="conversation-browser panel"><div class="panel-heading"><div><p class="eyebrow">HISTORY</p><h3>历史对话查询</h3></div><span>{{ conversations.length }}</span></div><button v-for="item in conversations" :key="item.id" :class="{ active: chat.conversationId === item.id }" @click="openConversation(item.id)"><b>{{ item.title }}</b><span>{{ item.message_count }} 条消息</span><small>{{ item.updated_at }}</small></button><div v-if="!conversations.length" class="empty-state">暂无历史对话</div></aside><section class="chat-workspace panel"><div class="service-scope"><div><p class="eyebrow">OPTIONAL SERVICE SCOPE</p><h3>选择调查起点</h3><span>{{ chat.selectedServices.length ? `已选择 ${chat.selectedServices.length} 个服务` : '不限制服务，由 Agent 自主识别' }}</span></div><div class="service-choices"><button :class="{ active: !chat.selectedServices.length }" @click="chat.selectedServices = []">不选择服务</button><button v-for="service in displayServices" :key="service.id" :class="{ active: chat.selectedServices.includes(service.id) }" @click="toggleService(service.id)">{{ service.name }}</button></div><div class="capability-row"><span>Intent Router</span><span>Conversation Memory</span><span>Context Compression</span><span>Tool Retrieval</span><span>Dynamic Scope</span></div></div><div ref="chatList" class="message-list"><div v-if="!chat.messages.length" class="chat-empty"><span>◇</span><h3>开始一次事件诊断</h3><p>例如：最近订单创建大量超时，请分析是否与支付服务有关。</p></div><article v-for="message in chat.messages" :key="message.id" class="chat-message" :class="message.role"><header><b>{{ message.role === 'user' ? '你' : 'SRE Agent' }}</b><span v-if="message.intent">{{ message.intent }}</span></header><p v-if="message.text">{{ message.text }}</p><div v-if="message.phases?.length" class="message-phases"><span v-for="phase in message.phases" :key="phase">✓ {{ phaseLabels[phase] || phase }}</span></div><details v-if="message.tools?.length"><summary>查看 {{ message.tools.length }} 个检索 / 工具步骤</summary><ol><li v-for="(tool, index) in message.tools" :key="index"><b>{{ tool.tool_name }}</b> — {{ tool.result_summary || tool.error }}</li></ol></details><div v-if="message.report" class="assistant-report"><div><small>ROOT CAUSE</small><b>{{ message.report.root_cause }}</b></div><div><small>CONFIDENCE</small><b>{{ confidence(message.report.confidence) }}</b></div><div class="cause-chain"><template v-for="(step, index) in message.report.root_cause_chain || []" :key="step"><span>{{ step }}</span><i v-if="index < message.report.root_cause_chain.length - 1">→</i></template></div></div><p v-if="message.error" class="chat-error">{{ message.error }}</p></article></div><form class="chat-composer" @submit.prevent="sendChat"><textarea v-model="chat.input" rows="3" placeholder="描述服务异常、告警、时间范围或希望继续追问的内容…" @keydown.ctrl.enter.prevent="sendChat"></textarea><div><span>{{ chat.conversationId ? '当前对话已启用记忆' : '发送后自动创建会话' }} · Ctrl + Enter</span><button class="primary-action" type="submit" :disabled="!chat.input.trim() || chat.sending">{{ chat.sending ? '诊断中…' : '发送诊断' }}</button></div></form></section></div></section>
    </main>
  </div>
</template>
