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
const validations = ref([]);
const testSuites = ref([]);
const validation = reactive({
  repository: "", branches: [], baseRef: "", candidateRef: "", loadingBranches: false,
  runExistingTests: true, generateAiTests: false, uploadedText: "", submitting: false,
  testSuiteVersionIds: [], repositoryTestFiles: [], aiReferencePaths: [], projectType: "PYTHON",
  interfaces: [], loadingTestFiles: false, loadingInterfaces: false,
  generatingInterfaceId: "", generationMessage: "", selected: null, error: "",
});
const suiteManager = reactive({
  selected: null, repository: "", name: "", description: "", projectType: "PYTHON",
  changeNote: "initial version", filesText: "", versionNote: "", versionFilesText: "",
  moduleName: "", modulePath: "", interfaceName: "", httpMethod: "GET",
  routePath: "", sourceFile: "", symbol: "",
  saving: false, error: "",
});

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
const availableSuiteVersions = computed(() => testSuites.value
  .filter((suite) => !validation.repository || suite.repository === validation.repository)
  .flatMap((suite) => (suite.versions || []).map((version) => ({ ...version, suite }))));
const groupedTestSuites = computed(() => {
  const groups = new Map();
  for (const suite of testSuites.value) {
    const target = suite.target || {};
    const key = `${suite.repository}::${target.module_name || "unclassified"}`;
    if (!groups.has(key)) groups.set(key, { key, repository: suite.repository, module: target.module_name || "unclassified", suites: [] });
    groups.get(key).suites.push(suite);
  }
  return [...groups.values()];
});

function authHeaders(json = false) { return { ...(json ? { "Content-Type": "application/json" } : {}), Authorization: `Bearer ${token.value}` }; }
function navigate(path) { window.location.hash = path; }
function openService(id) { navigate(`/services/${id}`); }
function openPod(name) { navigate(`/pods/${encodeURIComponent(name)}`); }
function openEventDiagnosis() { navigate("/diagnosis"); }
function openValidation() { navigate("/validations"); }
function parseRoute() {
  const segments = (window.location.hash.replace(/^#\/?/, "") || "services").split("/").filter(Boolean);
  route.serviceId = ""; route.podName = "";
  if (segments[0] === "services" && segments[1]) {
    route.name = "service-detail"; route.serviceId = segments[1]; resetQuick(); loadServicePods(route.serviceId);
  } else if (segments[0] === "pods" && segments[1]) {
    route.name = "pod-detail"; route.podName = decodeURIComponent(segments[1]); resetQuick(); loadPodDetail(route.podName);
  } else if (segments[0] === "diagnosis") route.name = "event-diagnosis";
  else if (segments[0] === "validations") { route.name = "validations"; loadValidations(); loadTestSuites(); }
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

async function loadValidationBranches() {
  validation.branches = []; validation.baseRef = ""; validation.candidateRef = "";
  validation.repositoryTestFiles = []; validation.aiReferencePaths = []; validation.testSuiteVersionIds = [];
  validation.interfaces = []; validation.generationMessage = "";
  if (!validation.repository || uiPreview) return;
  validation.loadingBranches = true; validation.error = "";
  try {
    const response = await fetch(`${apiBaseUrl}/api/repositories/${encodeURIComponent(validation.repository)}/branches`, { headers: authHeaders() });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    validation.branches = payload.branches || [];
    validation.baseRef = validation.branches.includes("main") ? "main" : validation.branches[0] || "";
    validation.candidateRef = validation.branches.find((item) => item !== validation.baseRef) || "";
    await Promise.all([loadTestSuites(validation.repository), loadRepositoryContext()]);
  } catch (error) { validation.error = error instanceof Error ? error.message : "Branch 加载失败"; }
  finally { validation.loadingBranches = false; }
}
async function loadRepositoryContext() {
  await Promise.all([loadRepositoryTestFiles(), loadRepositoryInterfaces()]);
}
async function loadRepositoryTestFiles() {
  validation.repositoryTestFiles = []; validation.aiReferencePaths = [];
  if (!validation.repository || !validation.candidateRef || uiPreview) return;
  validation.loadingTestFiles = true;
  try {
    const query = new URLSearchParams({ ref: validation.candidateRef });
    const response = await fetch(`${apiBaseUrl}/api/repositories/${encodeURIComponent(validation.repository)}/test-files?${query}`, { headers: authHeaders() });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    validation.repositoryTestFiles = payload.paths || []; validation.projectType = payload.project_type || "PYTHON";
  } catch (error) { validation.error = error instanceof Error ? error.message : "Candidate 测试文件加载失败"; }
  finally { validation.loadingTestFiles = false; }
}
async function loadRepositoryInterfaces() {
  validation.interfaces = [];
  if (!validation.repository || !validation.baseRef || !validation.candidateRef || validation.baseRef === validation.candidateRef || uiPreview) return;
  validation.loadingInterfaces = true;
  try {
    const query = new URLSearchParams({ base_ref: validation.baseRef, candidate_ref: validation.candidateRef });
    const response = await fetch(`${apiBaseUrl}/api/repositories/${encodeURIComponent(validation.repository)}/interfaces?${query}`, { headers: authHeaders() });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    validation.interfaces = payload.interfaces || []; validation.projectType = payload.project_type || validation.projectType;
  } catch (error) { validation.error = error instanceof Error ? error.message : "接口清单加载失败"; }
  finally { validation.loadingInterfaces = false; }
}
async function loadTestSuites(repository = "") {
  if (!token.value || uiPreview) return;
  const query = repository ? `?repository=${encodeURIComponent(repository)}` : "";
  const response = await fetch(`${apiBaseUrl}/api/validation-test-suites${query}`, { headers: authHeaders() });
  if (response.ok) testSuites.value = (await response.json()).items || [];
}
function parseTestFiles(text, label) {
  const parsed = JSON.parse(text || "[]");
  if (!Array.isArray(parsed) || !parsed.length) throw new Error(`${label}必须是非空 [{ path, content }] JSON 数组`);
  return parsed;
}
async function createManagedTestSuite() {
  suiteManager.error = ""; suiteManager.saving = true;
  try {
    const response = await fetch(`${apiBaseUrl}/api/validation-test-suites`, {
      method: "POST", headers: authHeaders(true), body: JSON.stringify({
        repository: suiteManager.repository, name: suiteManager.name,
        description: suiteManager.description, project_type: suiteManager.projectType,
        change_note: suiteManager.changeNote, files: parseTestFiles(suiteManager.filesText, "测试文件"),
        target: { module_name: suiteManager.moduleName, module_path: suiteManager.modulePath,
          interface_name: suiteManager.interfaceName, http_method: suiteManager.httpMethod,
          route_path: suiteManager.routePath, source_file: suiteManager.sourceFile,
          symbol: suiteManager.symbol || suiteManager.interfaceName },
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    suiteManager.selected = payload; suiteManager.versionFilesText = JSON.stringify(payload.versions?.[0]?.files?.map(({ path, content }) => ({ path, content })) || [], null, 2);
    await loadTestSuites(validation.repository);
  } catch (error) { suiteManager.error = error instanceof Error ? error.message : "测试集创建失败"; }
  finally { suiteManager.saving = false; }
}
async function selectManagedTestSuite(id) {
  const response = await fetch(`${apiBaseUrl}/api/validation-test-suites/${id}`, { headers: authHeaders() });
  if (!response.ok) return;
  const payload = await response.json(); suiteManager.selected = payload;
  suiteManager.name = payload.name; suiteManager.description = payload.description;
  suiteManager.moduleName = payload.target?.module_name || ""; suiteManager.modulePath = payload.target?.module_path || "";
  suiteManager.interfaceName = payload.target?.interface_name || ""; suiteManager.httpMethod = payload.target?.http_method || "GET";
  suiteManager.routePath = payload.target?.route_path || ""; suiteManager.sourceFile = payload.target?.source_file || "";
  suiteManager.symbol = payload.target?.symbol || "";
  const latest = payload.versions?.find((item) => item.version === payload.latest_version) || payload.versions?.[0];
  suiteManager.versionFilesText = JSON.stringify(latest?.files?.map(({ path, content }) => ({ path, content })) || [], null, 2);
  suiteManager.versionNote = `update from v${payload.latest_version}`;
}
async function createManagedTestSuiteVersion() {
  if (!suiteManager.selected) return; suiteManager.error = ""; suiteManager.saving = true;
  try {
    const response = await fetch(`${apiBaseUrl}/api/validation-test-suites/${suiteManager.selected.id}/versions`, {
      method: "POST", headers: authHeaders(true), body: JSON.stringify({
        change_note: suiteManager.versionNote,
        files: parseTestFiles(suiteManager.versionFilesText, "新版本文件"),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    suiteManager.selected = payload; await loadTestSuites(validation.repository);
  } catch (error) { suiteManager.error = error instanceof Error ? error.message : "测试集版本创建失败"; }
  finally { suiteManager.saving = false; }
}
async function saveManagedTestSuiteMetadata() {
  if (!suiteManager.selected) return; suiteManager.error = "";
  const response = await fetch(`${apiBaseUrl}/api/validation-test-suites/${suiteManager.selected.id}/metadata`, {
    method: "POST", headers: authHeaders(true), body: JSON.stringify({ name: suiteManager.name, description: suiteManager.description }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) { suiteManager.error = payload.detail || `HTTP ${response.status}`; return; }
  suiteManager.selected = payload; await loadTestSuites(validation.repository);
}
async function archiveManagedTestSuite() {
  if (!suiteManager.selected) return;
  const response = await fetch(`${apiBaseUrl}/api/validation-test-suites/${suiteManager.selected.id}/archive`, { method: "POST", headers: authHeaders() });
  if (!response.ok) { suiteManager.error = (await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`; return; }
  suiteManager.selected = null; await loadTestSuites(validation.repository);
}
async function generateInterfaceSuite(target) {
  if (validation.generatingInterfaceId) return;
  validation.error = ""; validation.generationMessage = ""; validation.generatingInterfaceId = target.id;
  try {
    const existing = testSuites.value.find((suite) => suite.repository === validation.repository && suite.target?.id === target.id);
    const response = await fetch(`${apiBaseUrl}/api/interface-test-suites/generate?project_id=${projectId}`, {
      method: "POST", headers: { ...authHeaders(true), "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ repository: validation.repository, base_ref: validation.baseRef,
        candidate_ref: validation.candidateRef, interface_id: target.id,
        suite_id: existing?.id || null, run_existing_tests: validation.runExistingTests }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    validation.generationMessage = payload.action === "CREATED"
      ? `已创建 ${target.module_name} / ${target.interface_name} 测试集 v${payload.version.version}，回归任务已启动。`
      : `已为 ${target.module_name} / ${target.interface_name} 追加不可变 v${payload.version.version}，回归任务已启动。`;
    await loadTestSuites(validation.repository);
    await selectManagedTestSuite(payload.suite.id);
    validation.testSuiteVersionIds = [payload.version.id];
    await openValidationResult(payload.validation.id); await loadValidations();
    const stream = await fetch(`${apiBaseUrl}${payload.validation.events_url}`, { headers: authHeaders() });
    if (stream.ok) await consumeSse(stream, (event) => {
      if (event.status && validation.selected) validation.selected.status = event.status;
    });
    await openValidationResult(payload.validation.id); await loadValidations();
  } catch (error) { validation.error = error instanceof Error ? error.message : "接口测试集生成失败"; }
  finally { validation.generatingInterfaceId = ""; }
}
function toggleAiReference(path) {
  const index = validation.aiReferencePaths.indexOf(path);
  if (index >= 0) validation.aiReferencePaths.splice(index, 1);
  else if (validation.aiReferencePaths.length < 10) validation.aiReferencePaths.push(path);
}
function toggleSuiteVersion(entry) {
  const selected = validation.testSuiteVersionIds.includes(entry.id);
  const sameSuiteIds = availableSuiteVersions.value
    .filter((item) => item.suite.id === entry.suite.id).map((item) => item.id);
  validation.testSuiteVersionIds = validation.testSuiteVersionIds.filter((id) => !sameSuiteIds.includes(id));
  if (!selected) validation.testSuiteVersionIds.push(entry.id);
}
async function loadValidations() {
  if (!token.value || uiPreview) return;
  const response = await fetch(`${apiBaseUrl}/api/validations?limit=50`, { headers: authHeaders() });
  if (response.ok) validations.value = (await response.json()).items || [];
}
async function openValidationResult(id) {
  if (uiPreview) return;
  const response = await fetch(`${apiBaseUrl}/api/validations/${id}`, { headers: authHeaders() });
  if (response.ok) validation.selected = await response.json();
}
function uploadedTests() {
  if (!validation.uploadedText.trim()) return [];
  return parseTestFiles(validation.uploadedText, "Uploaded Tests");
}
async function createValidation() {
  if (validation.submitting) return;
  validation.error = ""; validation.submitting = true;
  try {
    const files = uploadedTests();
    const response = await fetch(`${apiBaseUrl}/api/validations?project_id=${projectId}`, {
      method: "POST", headers: { ...authHeaders(true), "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ repository: validation.repository, base_ref: validation.baseRef,
        candidate_ref: validation.candidateRef, run_existing_tests: validation.runExistingTests,
        run_uploaded_tests: files.length > 0, generate_ai_tests: validation.generateAiTests,
        uploaded_tests: files, test_suite_version_ids: validation.testSuiteVersionIds,
        ai_reference_test_paths: validation.generateAiTests ? validation.aiReferencePaths : [] }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    await openValidationResult(payload.id); await loadValidations();
    const stream = await fetch(`${apiBaseUrl}${payload.events_url}`, { headers: authHeaders() });
    if (stream.ok) await consumeSse(stream, (event) => {
      if (event.status && validation.selected) validation.selected.status = event.status;
    });
    await openValidationResult(payload.id); await loadValidations();
  } catch (error) { validation.error = error instanceof Error ? error.message : "Validation 创建失败"; }
  finally { validation.submitting = false; }
}
async function diagnoseValidation() {
  if (!validation.selected) return;
  const response = await fetch(`${apiBaseUrl}/api/validations/${validation.selected.id}/diagnose?project_id=${projectId}`, { method: "POST", headers: authHeaders() });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) { validation.error = payload.detail || `HTTP ${response.status}`; return; }
  navigate("/diagnosis");
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
  try { const response = await fetch(`${apiBaseUrl}/api/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: loginForm.username.trim(), password: loginForm.password }) }); const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(payload.detail || "登录失败"); token.value = payload.access_token; localStorage.setItem("sre_agent_token", token.value); currentUser.value = payload.user; await Promise.all([loadServices(), loadConversations(), loadValidations(), loadTestSuites()]); }
  catch (error) { loginForm.error = error instanceof Error ? error.message : "登录失败"; }
  finally { loginForm.submitting = false; }
}
async function restoreSession() {
  if (!token.value) { authLoading.value = false; return; }
  try { const response = await fetch(`${apiBaseUrl}/api/auth/me`, { headers: authHeaders() }); if (!response.ok) throw new Error("expired"); currentUser.value = await response.json(); await Promise.all([loadServices(), loadConversations(), loadValidations(), loadTestSuites()]); }
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
    <aside class="console-sidebar"><button class="brand" @click="navigate('/services')"><span class="brand-mark">S</span><span><strong>SRE Console</strong><small>Service Intelligence</small></span></button><nav class="primary-nav"><button :class="{ active: route.name === 'services' || route.name === 'service-detail' }" @click="navigate('/services')"><span class="nav-icon">□</span><span>服务目录</span><b>{{ displayServices.length }}</b></button><button :class="{ active: route.name === 'event-diagnosis' }" @click="openEventDiagnosis"><span class="nav-icon">◇</span><span>事件诊断</span><b>{{ conversations.length }}</b></button><button :class="{ active: route.name === 'validations' }" @click="openValidation"><span class="nav-icon">↔</span><span>合并前验证</span><b>{{ validations.length }}</b></button></nav><section class="environment-card"><div><span class="live-dot"></span><b>sre-lab</b><small>CONNECTED</small></div><dl><div><dt>Agent</dt><dd>:8001</dd></div><div><dt>Policy</dt><dd>READ ONLY</dd></div></dl></section><section class="recent-runs"><div class="sidebar-heading"><span>历史对话</span><b>{{ conversations.length }}</b></div><button v-for="item in conversations.slice(0, 6)" :key="item.id" @click="openConversation(item.id)"><span>{{ item.title }}</span><small>{{ item.message_count }} messages · {{ item.updated_at }}</small></button><p v-if="!conversations.length">暂无历史对话</p></section><div class="user-panel"><span>{{ currentUser.username?.slice(0, 1)?.toUpperCase() }}</span><div><b>{{ currentUser.username }}</b><small>Operator</small></div><button title="退出" @click="logout(true)">↗</button></div></aside>
    <main class="console-main">
      <header class="topbar"><div><p class="breadcrumb">SRE-LAB / {{ route.name.toUpperCase() }}</p><h1>{{ route.name === 'services' ? '服务目录' : route.name === 'service-detail' ? currentService.name : route.name === 'pod-detail' ? route.podName : route.name === 'validations' ? '合并前验证' : '事件诊断' }}</h1></div><div class="topbar-meta"><span class="snapshot-dot"></span><span>CONTROLLED EXECUTION</span><b>immutable commits</b></div></header>
      <section v-if="route.name === 'services'" class="page-content overview-page catalog-only"><div class="health-strip"><button :class="{ selected: statusFilter === 'all' }" @click="statusFilter = 'all'"><small>All Services</small><strong>{{ displayServices.length }}</strong><span>应用服务</span></button><button :class="{ selected: statusFilter === 'healthy' }" @click="statusFilter = 'healthy'"><small>Healthy</small><strong>{{ statusSummary.healthy }}</strong><span>运行正常</span></button><button :class="{ selected: statusFilter === 'warning' }" @click="statusFilter = 'warning'"><small>Warning</small><strong>{{ statusSummary.warning }}</strong><span>需要关注</span></button><button :class="{ selected: statusFilter === 'critical' }" @click="statusFilter = 'critical'"><small>Critical</small><strong>{{ statusSummary.critical }}</strong><span>立即处理</span></button></div><div class="catalog-toolbar"><label><span>⌕</span><input v-model="serviceSearch" placeholder="搜索服务、Owner 或职责" /></label><span>{{ filteredServices.length }} services</span></div><div class="service-grid"><button v-for="service in filteredServices" :key="service.id" class="service-card" @click="openService(service.id)"><div class="service-card-head"><span class="service-glyph">{{ service.name.slice(0, 2).toUpperCase() }}</span><span class="status-badge" :class="service.status"><i></i>{{ statusLabel(service.status) }}</span></div><h3>{{ service.name }}</h3><p>{{ service.description }}</p><div class="metric-grid"><div><small>P95 LATENCY</small><b>{{ service.p95 }}</b></div><div><small>ERROR RATE</small><b>{{ service.errorRate }}</b></div><div><small>CPU</small><b>{{ service.cpu }}%</b><i><span :style="{ width: service.cpu + '%' }"></span></i></div><div><small>MEMORY</small><b>{{ service.memory }}%</b><i><span :style="{ width: service.memory + '%' }"></span></i></div></div><footer><span>更新于 {{ service.updatedAt }}</span><b>查看服务 →</b></footer></button></div></section>
      <section v-else-if="route.name === 'service-detail'" class="page-content detail-page"><button class="back-button" @click="navigate('/services')">← 返回服务目录</button><div class="service-hero"><div class="service-identity"><span class="service-glyph large">{{ currentService.name.slice(0, 2).toUpperCase() }}</span><div><span class="status-badge" :class="currentService.status"><i></i>{{ statusLabel(currentService.status) }}</span><h2>{{ currentService.name }}</h2><p>{{ currentService.description }}</p></div></div><button class="primary-action" :disabled="quick.running" @click="runQuickDiagnosis('SERVICE', currentService.id)">{{ quick.running ? '诊断中…' : '开始快速诊断' }}</button></div>
        <section v-if="quick.running || quick.result || quick.error" class="panel quick-diagnosis"><div class="panel-heading"><div><p class="eyebrow">STATELESS QUICK DIAGNOSIS</p><h3>即时因果链</h3></div><span>无对话 · 无记忆</span></div><div v-if="quick.running" class="quick-progress"><b>正在分析 {{ quick.targetName }}</b><span>{{ phaseLabels[quick.phases.at(-1)] || '连接证据源' }}</span><i><span :style="{ width: Math.max(8, quick.phases.length / Object.keys(phaseLabels).length * 100) + '%' }"></span></i></div><div v-if="quick.error" class="incident-error"><b>快速诊断失败</b><span>{{ quick.error }}</span></div><div v-if="quick.result" class="quick-result"><div class="quick-summary"><article><small>SUSPECTED ROOT CAUSE</small><h3>{{ quickRoot?.title }}</h3><p>{{ quickRoot?.description }}</p></article><div><small>CONFIDENCE</small><strong>{{ confidence(quickRoot?.confidence) }}</strong></div></div><div class="cause-chain"><template v-for="(step, index) in quickReport?.root_cause_chain || []" :key="step"><span>{{ step }}</span><i v-if="index < quickReport.root_cause_chain.length - 1">→</i></template></div><div class="quick-grid"><div><p class="eyebrow">SERVICE DEPENDENCY GRAPH</p><ServiceGraph compact :graph="quickGraph" :involved-services="quickServices" :root-cause-service="quickRoot?.root_resource?.name" /></div><div><p class="eyebrow">EXECUTED EVIDENCE STEPS</p><ol class="quick-tools"><li v-for="(tool, index) in quick.tools" :key="index"><b>{{ tool.tool_name }}</b><span>{{ tool.result_summary || tool.error || '证据已采集' }}</span></li></ol><p class="eyebrow">RECOMMENDATIONS</p><ol class="quick-tools"><li v-for="item in quickRoot?.recommendations || []" :key="item"><span>{{ item }}</span></li></ol></div></div></div></section>
        <div class="detail-layout"><div class="detail-primary"><section class="panel metrics-panel"><div class="panel-heading"><div><p class="eyebrow">HEALTH SUMMARY</p><h3>Metrics 概览</h3></div><span>最近 30 分钟</span></div><div class="large-metrics"><div><small>P95 LATENCY</small><strong>{{ currentService.p95 }}</strong></div><div><small>ERROR RATE</small><strong>{{ currentService.errorRate }}</strong></div><div><small>CPU USAGE</small><strong>{{ currentService.cpu }}%</strong></div><div><small>MEMORY</small><strong>{{ currentService.memory }}%</strong></div></div></section><section class="panel pods-panel"><div class="panel-heading"><div><p class="eyebrow">KUBERNETES</p><h3>Pods</h3></div><span>{{ resourceLoading ? '读取中…' : servicePods.length + ' pods' }}</span></div><div v-if="servicePods.length" class="pod-list"><button v-for="pod in servicePods" :key="pod" @click="openPod(pod)"><span class="live-dot"></span><div><b>{{ pod }}</b><small>sre-lab · 实时发现</small></div><i>→</i></button></div><div v-else class="empty-state">{{ resourceLoading ? '正在读取 Pod…' : '当前未发现 Pod' }}</div></section><section class="panel graph-panel"><div class="panel-heading"><div><p class="eyebrow">SERVICE MAP</p><h3>依赖关系</h3></div></div><ServiceGraph :graph="catalogGraph" :focus-service="currentService.id" /></section></div><aside class="detail-aside"><section class="panel deployment-panel"><div class="panel-heading"><div><p class="eyebrow">DEPLOYMENT</p><h3>最近部署</h3></div></div><dl><div><dt>Version</dt><dd><code>{{ currentService.version }}</code></dd></div><div><dt>Runtime</dt><dd>{{ currentService.runtime }}</dd></div><div><dt>Owner</dt><dd>{{ currentService.owner }}</dd></div><div><dt>Deployed</dt><dd>{{ currentService.deployedAt }}</dd></div></dl></section><section class="panel dependency-list"><div class="panel-heading"><div><p class="eyebrow">DEPENDENCIES</p><h3>上下游服务</h3></div></div><div><small>UPSTREAM</small><button v-for="item in currentService.upstreams" :key="item" @click="openService(item)">{{ item }} →</button></div><div><small>DOWNSTREAM</small><button v-for="item in currentService.dependencies" :key="item" @click="openService(item)">{{ item }} →</button></div></section></aside></div></section>
      <section v-else-if="route.name === 'pod-detail'" class="page-content detail-page"><button class="back-button" @click="navigate('/services')">← 返回服务目录</button><div class="service-hero"><div class="service-identity"><span class="service-glyph large">PD</span><div><span class="status-badge warning"><i></i>Kubernetes Pod</span><h2>{{ route.podName }}</h2><p>Pod 只是快速诊断起点，证据可沿依赖关系扩展。</p></div></div><button class="primary-action" :disabled="quick.running" @click="runQuickDiagnosis('POD', route.podName)">{{ quick.running ? '诊断中…' : '快速诊断 Pod' }}</button></div><section v-if="quick.running || quick.result || quick.error" class="panel quick-diagnosis"><div class="panel-heading"><div><p class="eyebrow">STATELESS QUICK DIAGNOSIS</p><h3>即时诊断结果</h3></div><span>无对话 · 无记忆</span></div><div v-if="quick.running" class="empty-state">正在生成完整因果链…</div><div v-if="quick.error" class="incident-error">{{ quick.error }}</div><div v-if="quick.result" class="quick-summary"><article><small>ROOT CAUSE</small><h3>{{ quickRoot?.title }}</h3><p>{{ quickRoot?.description }}</p></article><strong>{{ confidence(quickRoot?.confidence) }}</strong></div></section><section class="panel"><div class="panel-heading"><div><p class="eyebrow">POD OVERVIEW</p><h3>运行时详情</h3></div></div><pre v-if="podDetail" class="pod-raw">{{ JSON.stringify(podDetail.data, null, 2) }}</pre><div v-else class="empty-state">Pod 数据暂不可用</div></section></section>
      <section v-else-if="route.name === 'validations'" class="page-content validation-page">
        <div class="event-intro"><div><p class="eyebrow">AI-ASSISTED CI / PRE-MERGE VALIDATION</p><h2>用相同环境比较两个不可变 Commit</h2><p>管理可复用测试集、选择 AI 参考样本，并把同一套测试注入 Base 与 Candidate。Base 失败而 Candidate 通过会显示为 Possible Fix。</p></div></div>
        <section class="test-suite-workbench">
          <form class="panel suite-create" @submit.prevent="createManagedTestSuite">
            <div class="panel-heading"><div><p class="eyebrow">TEST SUITE LIBRARY</p><h3>创建持久化测试集</h3></div><span>每次编辑生成新版本</span></div>
            <div class="branch-pair"><label><span>Repository</span><select v-model="suiteManager.repository" required><option value="" disabled>选择授权仓库</option><option v-for="service in displayServices" :key="service.id" :value="service.id">{{ service.name }}</option></select></label><label><span>Project Type</span><select v-model="suiteManager.projectType"><option>PYTHON</option><option>MAVEN</option></select></label></div>
            <label><span>名称</span><input v-model="suiteManager.name" required placeholder="payment regression suite" /></label>
            <label><span>说明</span><input v-model="suiteManager.description" placeholder="覆盖连接池和支付超时修复" /></label>
            <div class="branch-pair"><label><span>模块名称</span><input v-model="suiteManager.moduleName" required placeholder="payment.api" /></label><label><span>接口名称</span><input v-model="suiteManager.interfaceName" required placeholder="create_payment" /></label></div>
            <div class="branch-pair"><label><span>HTTP Method</span><select v-model="suiteManager.httpMethod"><option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option><option>FUNCTION</option></select></label><label><span>Route</span><input v-model="suiteManager.routePath" placeholder="/api/payments" /></label></div>
            <label><span>源码文件 / Symbol</span><div class="branch-pair"><input v-model="suiteManager.sourceFile" placeholder="app/api/payment.py" /><input v-model="suiteManager.symbol" placeholder="create_payment" /></div></label>
            <label><span>模块路径</span><input v-model="suiteManager.modulePath" placeholder="app/api" /></label>
            <label><span>版本说明</span><input v-model="suiteManager.changeNote" required /></label>
            <label><span>测试文件 JSON</span><textarea v-model="suiteManager.filesText" rows="6" required placeholder='[{"path":"tests/test_payment_fix.py","content":"def test_fix():\n    assert True"}]'></textarea></label>
            <button class="primary-action" :disabled="suiteManager.saving">{{ suiteManager.saving ? '保存中…' : '创建测试集 v1' }}</button><p v-if="suiteManager.error" class="chat-error">{{ suiteManager.error }}</p>
          </form>
          <section class="panel suite-library"><div class="panel-heading"><div><p class="eyebrow">SAVED SUITES</p><h3>按模块 / 接口查看</h3></div><span>{{ testSuites.length }}</span></div><div class="suite-list"><section v-for="group in groupedTestSuites" :key="group.key" class="suite-module-group"><header><b>{{ group.module }}</b><small>{{ group.repository }} · {{ group.suites.length }} interfaces</small></header><button v-for="suite in group.suites" :key="suite.id" :class="{ active: suiteManager.selected?.id === suite.id }" @click="selectManagedTestSuite(suite.id)"><b><code>{{ suite.target?.http_method }}</code> {{ suite.target?.route_path || suite.target?.interface_name }}</b><small>{{ suite.target?.interface_name }} · {{ suite.target?.source_file || 'manual target' }}</small><span>v{{ suite.latest_version }} · {{ suite.versions?.[0]?.files?.length || 0 }} files</span></button></section><div v-if="!testSuites.length" class="empty-state">还没有持久化测试集</div></div></section>
          <section v-if="suiteManager.selected" class="panel suite-editor"><div class="panel-heading"><div><p class="eyebrow">SUITE DETAIL</p><h3>{{ suiteManager.selected.name }}</h3></div><button class="text-button" @click="archiveManagedTestSuite">归档</button></div><div class="suite-target"><span>MODULE</span><b>{{ suiteManager.selected.target?.module_name }}</b><span>INTERFACE</span><b><code>{{ suiteManager.selected.target?.http_method }}</code> {{ suiteManager.selected.target?.route_path || suiteManager.selected.target?.interface_name }}</b><small>{{ suiteManager.selected.target?.source_file }} · {{ suiteManager.selected.target?.symbol }}</small></div><div class="branch-pair"><label><span>名称</span><input v-model="suiteManager.name" /></label><label><span>说明</span><input v-model="suiteManager.description" /></label></div><button class="secondary-action" @click="saveManagedTestSuiteMetadata">保存元数据</button><div class="suite-version-editor"><label><span>新版本说明</span><input v-model="suiteManager.versionNote" /></label><label><span>新版本完整文件 JSON</span><textarea v-model="suiteManager.versionFilesText" rows="7"></textarea></label><button class="primary-action" :disabled="suiteManager.saving" @click="createManagedTestSuiteVersion">创建不可变新版本</button></div><details v-for="version in suiteManager.selected.versions" :key="version.id" class="suite-version"><summary><b>v{{ version.version }}</b><span>{{ version.change_note }} · {{ version.files.length }} files</span></summary><details v-for="file in version.files" :key="file.path" class="suite-file"><summary>{{ file.path }} · {{ file.size_bytes }} bytes</summary><pre>{{ file.content }}</pre></details></details></section>
        </section>
        <section class="panel interface-generation"><div class="panel-heading"><div><p class="eyebrow">MODULE / INTERFACE CHANGE MAP</p><h3>一键生成接口测试集并执行回归</h3></div><span>{{ validation.loadingInterfaces ? '分析中…' : validation.interfaces.length + ' interfaces' }}</span></div><p>选择 Base 与 Candidate 后，从冻结 Commit 发现接口。新增接口会创建测试集 v1；修改接口会在原测试集追加不可变版本，并立即让 Base/Candidate 使用完全相同的版本执行。</p><div class="interface-list"><article v-for="target in validation.interfaces" :key="target.id" :class="target.change_type.toLowerCase()"><div><span class="change-badge">{{ target.change_type }}</span><small>{{ target.module_name }}</small><h4><code>{{ target.http_method }}</code> {{ target.route_path || target.interface_name }}</h4><p>{{ target.interface_name }} · {{ target.source_file }}</p></div><button class="secondary-action" :disabled="validation.generatingInterfaceId || target.change_type === 'REMOVED'" @click="generateInterfaceSuite(target)">{{ validation.generatingInterfaceId === target.id ? '生成并回归中…' : testSuites.some((suite) => suite.target?.id === target.id) ? '更新测试集并回归' : '生成测试集并回归' }}</button></article><div v-if="!validation.loadingInterfaces && !validation.interfaces.length" class="empty-state">选择不同的 Base / Candidate Branch 后显示模块与接口变更</div></div><p v-if="validation.generationMessage" class="generation-success">{{ validation.generationMessage }}</p></section>
        <div class="validation-layout">
          <form class="panel validation-form" @submit.prevent="createValidation"><div class="panel-heading"><div><p class="eyebrow">NEW VALIDATION</p><h3>验证范围</h3></div><span>Base ↔ Candidate</span></div><label><span>Repository</span><select v-model="validation.repository" required @change="loadValidationBranches"><option value="" disabled>选择授权仓库</option><option v-for="service in displayServices" :key="service.id" :value="service.id">{{ service.name }}</option></select></label><div class="branch-pair"><label><span>Base Branch</span><select v-model="validation.baseRef" required @change="loadRepositoryContext"><option v-for="branch in validation.branches" :key="branch">{{ branch }}</option></select></label><label><span>Candidate Branch</span><select v-model="validation.candidateRef" required @change="loadRepositoryContext"><option v-for="branch in validation.branches" :key="branch">{{ branch }}</option></select></label></div><div class="validation-options"><label><input v-model="validation.runExistingTests" type="checkbox" />运行仓库已有测试</label><label><input v-model="validation.generateAiTests" type="checkbox" />生成受限 AI Tests</label></div>
            <fieldset class="suite-picker"><legend>持久化测试集版本（每套选择一个版本）</legend><label v-for="entry in availableSuiteVersions" :key="entry.id"><input type="checkbox" :checked="validation.testSuiteVersionIds.includes(entry.id)" @change="toggleSuiteVersion(entry)" /><span>{{ entry.suite.name }} · v{{ entry.version }}</span><small>{{ entry.files.length }} files · {{ entry.change_note }}</small></label><p v-if="!availableSuiteVersions.length">当前 Repository 暂无测试集</p></fieldset>
            <fieldset v-if="validation.generateAiTests" class="ai-reference-picker"><legend>AI 参考测试样本（最多 10 个）</legend><span>{{ validation.loadingTestFiles ? '正在读取冻结 Candidate…' : `已选 ${validation.aiReferencePaths.length} / ${validation.repositoryTestFiles.length}` }}</span><label v-for="path in validation.repositoryTestFiles" :key="path"><input type="checkbox" :checked="validation.aiReferencePaths.includes(path)" @change="toggleAiReference(path)" /><code>{{ path }}</code></label><p v-if="!validation.loadingTestFiles && !validation.repositoryTestFiles.length">Candidate 中没有可用的 Python/Maven 测试文件；留空时 AI 将没有代表性样本。</p></fieldset>
            <label><span>本次临时 Uploaded Tests（可选 JSON）</span><textarea v-model="validation.uploadedText" rows="5" placeholder='[{"path":"tests/test_regression.py","content":"def test_regression(): ..."}]'></textarea></label><p v-if="validation.error" class="chat-error">{{ validation.error }}</p><button class="primary-action" :disabled="validation.submitting || validation.baseRef === validation.candidateRef">{{ validation.submitting ? '正在隔离执行…' : '开始合并前验证' }}</button><small class="validation-note">每次创建都会刷新受信任远程 Branch、冻结 SHA 和测试集版本。默认断网、只读根文件系统，不接受任意 Shell。</small></form>
          <section class="panel validation-history"><div class="panel-heading"><div><p class="eyebrow">VALIDATION RUNS</p><h3>最近验证</h3></div><span>{{ validations.length }}</span></div><button v-for="item in validations" :key="item.id" :class="{ active: validation.selected?.id === item.id }" @click="openValidationResult(item.id)"><span class="validation-status" :class="item.status.toLowerCase()">{{ item.status }}</span><b>{{ item.repository }}</b><small>{{ item.base_ref }} ↔ {{ item.candidate_ref }}</small><em>{{ item.summary || '等待执行' }}</em></button><div v-if="!validations.length" class="empty-state">暂无 Validation Run</div></section>
        </div>
        <section v-if="validation.selected" class="panel validation-result"><div class="panel-heading"><div><p class="eyebrow">RESULT · {{ validation.selected.id }}</p><h3>{{ validation.selected.summary || validation.selected.status }}</h3></div><button v-if="validation.selected.regression_count" class="primary-action" @click="diagnoseValidation">Diagnose Regression</button></div><div class="validation-kpis"><div><small>REGRESSIONS</small><b>{{ validation.selected.regression_count }}</b></div><div><small>EXISTING FAILURES</small><b>{{ validation.selected.existing_failure_count }}</b></div><div><small>POSSIBLE FIXES</small><b>{{ validation.selected.possible_fix_count }}</b></div><div><small>CONFIDENCE</small><b>{{ validation.selected.comparison_confidence }}</b></div></div><div class="commit-pair"><code>{{ validation.selected.base_commit_sha }}</code><span>versus</span><code>{{ validation.selected.candidate_commit_sha }}</code></div><div class="execution-grid"><article v-for="execution in validation.selected.executions" :key="execution.id"><p class="eyebrow">{{ execution.side }}</p><h4>{{ execution.build_status }} / {{ execution.test_status }}</h4><span>{{ execution.duration_ms }} ms · {{ execution.tests.length }} tests</span><details><summary>查看执行测试</summary><div class="executed-test" v-for="test in execution.tests" :key="test.source + test.suite + test.name"><b>{{ test.status }}</b><code>{{ test.source }} · {{ test.suite }} · {{ test.name }}</code></div></details></article></div><div class="validation-test-inventory"><section><p class="eyebrow">FROZEN MANAGED TEST SUITES</p><details v-for="version in validation.selected.test_suite_versions" :key="version.id"><summary>{{ version.target?.module_name }} / {{ version.target?.interface_name }} · v{{ version.version }} · {{ version.files.length }} files</summary><small><code>{{ version.target?.http_method }}</code> {{ version.target?.route_path }} · {{ version.suite_name }}</small><details v-for="file in version.files" :key="file.path"><summary>{{ file.path }}</summary><pre>{{ file.content }}</pre></details></details><p v-if="!validation.selected.test_suite_versions?.length">未引用持久化测试集</p></section><section><p class="eyebrow">AI GENERATED TESTS</p><small>参考样本：{{ validation.selected.ai_reference_test_paths?.join(', ') || '自动选择' }}</small><details v-for="test in validation.selected.generated_tests" :key="test.target_file"><summary>{{ test.valid ? 'VALID' : 'INVALID' }} · {{ test.target_file }}</summary><p>{{ test.reason }}</p><pre>{{ test.test_code }}</pre><small v-if="test.validation_error">{{ test.validation_error }}</small></details><p v-if="!validation.selected.generated_tests?.length">本次没有 AI Generated Tests</p></section></div><div class="regression-table"><div v-for="item in validation.selected.regressions" :key="item.id"><span>{{ item.classification }}</span><b>{{ item.suite }} · {{ item.name }}</b><small>{{ item.base_status || '—' }} → {{ item.candidate_status || '—' }} · {{ item.confidence }}</small></div></div></section>
      </section>
      <section v-else class="page-content event-page"><div class="event-intro"><div><p class="eyebrow">INCIDENT DIAGNOSIS</p><h2>描述现象，持续诊断</h2><p>可不选服务、选择一个或多个服务作为调查起点；Agent 会保留意图、记忆与压缩上下文，并可沿证据自动扩展范围。</p></div><button class="primary-action" @click="createConversation">＋ 创建新对话</button></div><div class="event-layout"><aside class="conversation-browser panel"><div class="panel-heading"><div><p class="eyebrow">HISTORY</p><h3>历史对话查询</h3></div><span>{{ conversations.length }}</span></div><button v-for="item in conversations" :key="item.id" :class="{ active: chat.conversationId === item.id }" @click="openConversation(item.id)"><b>{{ item.title }}</b><span>{{ item.message_count }} 条消息</span><small>{{ item.updated_at }}</small></button><div v-if="!conversations.length" class="empty-state">暂无历史对话</div></aside><section class="chat-workspace panel"><div class="service-scope"><div><p class="eyebrow">OPTIONAL SERVICE SCOPE</p><h3>选择调查起点</h3><span>{{ chat.selectedServices.length ? `已选择 ${chat.selectedServices.length} 个服务` : '不限制服务，由 Agent 自主识别' }}</span></div><div class="service-choices"><button :class="{ active: !chat.selectedServices.length }" @click="chat.selectedServices = []">不选择服务</button><button v-for="service in displayServices" :key="service.id" :class="{ active: chat.selectedServices.includes(service.id) }" @click="toggleService(service.id)">{{ service.name }}</button></div><div class="capability-row"><span>Intent Router</span><span>Conversation Memory</span><span>Context Compression</span><span>Tool Retrieval</span><span>Dynamic Scope</span></div></div><div ref="chatList" class="message-list"><div v-if="!chat.messages.length" class="chat-empty"><span>◇</span><h3>开始一次事件诊断</h3><p>例如：最近订单创建大量超时，请分析是否与支付服务有关。</p></div><article v-for="message in chat.messages" :key="message.id" class="chat-message" :class="message.role"><header><b>{{ message.role === 'user' ? '你' : 'SRE Agent' }}</b><span v-if="message.intent">{{ message.intent }}</span></header><p v-if="message.text">{{ message.text }}</p><div v-if="message.phases?.length" class="message-phases"><span v-for="phase in message.phases" :key="phase">✓ {{ phaseLabels[phase] || phase }}</span></div><details v-if="message.tools?.length"><summary>查看 {{ message.tools.length }} 个检索 / 工具步骤</summary><ol><li v-for="(tool, index) in message.tools" :key="index"><b>{{ tool.tool_name }}</b> — {{ tool.result_summary || tool.error }}</li></ol></details><div v-if="message.report" class="assistant-report"><div><small>ROOT CAUSE</small><b>{{ message.report.root_cause }}</b></div><div><small>CONFIDENCE</small><b>{{ confidence(message.report.confidence) }}</b></div><div class="cause-chain"><template v-for="(step, index) in message.report.root_cause_chain || []" :key="step"><span>{{ step }}</span><i v-if="index < message.report.root_cause_chain.length - 1">→</i></template></div></div><p v-if="message.error" class="chat-error">{{ message.error }}</p></article></div><form class="chat-composer" @submit.prevent="sendChat"><textarea v-model="chat.input" rows="3" placeholder="描述服务异常、告警、时间范围或希望继续追问的内容…" @keydown.ctrl.enter.prevent="sendChat"></textarea><div><span>{{ chat.conversationId ? '当前对话已启用记忆' : '发送后自动创建会话' }} · Ctrl + Enter</span><button class="primary-action" type="submit" :disabled="!chat.input.trim() || chat.sending">{{ chat.sending ? '诊断中…' : '发送诊断' }}</button></div></form></section></div></section>
    </main>
  </div>
</template>
