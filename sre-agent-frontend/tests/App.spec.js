import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";

const service = {
  id: "order-service",
  name: "order-service",
  description: "订单创建与状态查询",
  owner: "order-team",
  status: "warning",
  metrics: { p95_ms: 420, error_rate: 2.1, cpu_percent: 51, memory_percent: 63 },
  dependencies: ["payment-service"],
  upstreams: ["gateway"],
  version: "v2",
  runtime: "Java 21",
  deployed_at: "2026-09-08T00:00:00Z",
  updated_at: "2026-09-08T00:01:00Z",
};

const paymentService = {
  ...service,
  id: "payment-service",
  name: "payment-service",
  description: "支付授权与状态管理",
  owner: "payment-team",
  status: "critical",
  dependencies: [],
  upstreams: ["order-service"],
};

function jsonResponse(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data, body: null };
}

function sseResponse(events) {
  const data = new TextEncoder().encode(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""));
  let sent = false;
  return {
    ok: true,
    status: 200,
    json: async () => ({}),
    body: { getReader: () => ({ read: async () => sent ? { done: true } : (sent = true, { done: false, value: data }) }) },
  };
}

function authenticatedFetch(overrides = {}) {
  return vi.fn(async (input, init = {}) => {
    const url = String(input);
    if (overrides[url]) return overrides[url](init);
    if (url.endsWith("/api/auth/me")) return jsonResponse({ id: "u1", username: "tester" });
    if (url.endsWith("/api/services")) return jsonResponse({ items: [service, paymentService] });
    if (url.includes("/api/conversations")) return jsonResponse([]);
    if (url.includes("/api/validations")) return jsonResponse({ items: [] });
    if (url.includes("/api/validation-test-suites")) return jsonResponse({ items: [] });
    return jsonResponse({}, 404);
  });
}

async function mountAuthenticated(fetchMock = authenticatedFetch()) {
  localStorage.setItem("sre_agent_token", "token-1");
  globalThis.fetch = fetchMock;
  const wrapper = mount(App);
  await flushPromises();
  return { wrapper, fetchMock };
}

describe("SRE Console", () => {
  it("leaves the loading state and shows login when no session exists", async () => {
    globalThis.fetch = vi.fn();
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("进入服务诊断平台");
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("shows a stable error state for a rejected login", async () => {
    globalThis.fetch = vi.fn(async () => jsonResponse({ detail: "用户名或密码错误" }, 401));
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get('input[autocomplete="username"]').setValue("bad-user");
    await wrapper.get('input[type="password"]').setValue("bad-pass");
    await wrapper.get("form.login-card").trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("用户名或密码错误");
    expect(wrapper.get("form.login-card button").attributes("disabled")).toBeUndefined();
  });

  it("logs in, persists the token and renders service health metrics", async () => {
    globalThis.fetch = authenticatedFetch({
      "http://127.0.0.1:8001/api/auth/login": () => jsonResponse({
        access_token: "new-token",
        user: { id: "u1", username: "tester" },
      }),
    });
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get('input[autocomplete="username"]').setValue("tester");
    await wrapper.get('input[type="password"]').setValue("correct-pass");
    await wrapper.get("form.login-card").trigger("submit");
    await flushPromises();
    expect(localStorage.getItem("sre_agent_token")).toBe("new-token");
    expect(wrapper.text()).toContain("order-service");
    expect(wrapper.text()).toContain("420 ms");
    expect(wrapper.text()).toContain("2.1%");
  });

  it("handles chat SSE once, preserves multi-service scope and renders the root cause", async () => {
    window.location.hash = "#/diagnosis";
    let requestBody;
    const fetchMock = authenticatedFetch({
      "http://127.0.0.1:8001/api/agent/chat/stream": (init) => {
        requestBody = JSON.parse(init.body);
        return sseResponse([
          { type: "conversation", conversation_id: "conv-1" },
          { type: "intent", intent: "SPECIFIC_INCIDENT" },
          { type: "phase", phase: "TRIAGE" },
          { type: "phase", phase: "TRIAGE" },
          { type: "tool", record: { tool_name: "query_metrics", result_summary: "error rate high" } },
          { type: "final", report: { decision_summary: "定位完成", root_cause: "payment-db pool exhausted", confidence: 0.93, root_cause_chain: ["order-service", "payment-service", "payment-db"] } },
        ]);
      },
    });
    const { wrapper } = await mountAuthenticated(fetchMock);
    const scopeButtons = wrapper.findAll(".service-choices button");
    await scopeButtons.find((button) => button.text() === "order-service").trigger("click");
    await scopeButtons.find((button) => button.text() === "payment-service").trigger("click");
    await wrapper.get(".chat-composer textarea").setValue("订单支付为什么超时");
    await wrapper.get(".chat-composer").trigger("submit");
    await flushPromises();

    expect(requestBody.selected_services).toEqual(["order-service", "payment-service"]);
    expect(wrapper.text()).toContain("payment-db pool exhausted");
    expect(wrapper.text()).toContain("93%");
    expect(wrapper.findAll(".message-phases span")).toHaveLength(1);
    expect(wrapper.text()).toContain("query_metrics");
  });

  it("keeps quick diagnosis on the service page and renders a stateless causal chain", async () => {
    const fetchMock = authenticatedFetch({
      "http://127.0.0.1:8001/api/services/order-service/pods": () => jsonResponse({ pods: [] }),
      "http://127.0.0.1:8001/api/diagnoses/quick/stream": () => sseResponse([
        { type: "phase", phase: "TRIAGE" },
        { type: "tool", record: { tool_name: "query_trace", result_summary: "payment span slow" } },
        { type: "final", result: {
          affected_services: ["order-service", "payment-service"],
          root_cause: { title: "payment-service 超时", description: "连接池耗尽", confidence: 0.88, root_resource: { name: "payment-service" }, recommendations: ["扩容连接池"] },
          report: { root_cause_chain: ["order-service 延迟", "payment-service 超时"] },
          graph: { nodes: [{ id: "order", name: "order-service", type: "SERVICE", status: "AFFECTED" }, { id: "payment", name: "payment-service", type: "SERVICE", status: "ROOT_CAUSE" }], edges: [{ id: "e", source: "order", target: "payment", relation: "HTTP" }] },
        } },
      ]),
    });
    const { wrapper } = await mountAuthenticated(fetchMock);
    await wrapper.get(".service-card").trigger("click");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await flushPromises();
    await wrapper.get(".service-hero .primary-action").trigger("click");
    await flushPromises();

    expect(window.location.hash).toBe("#/services/order-service");
    expect(wrapper.text()).toContain("无对话 · 无记忆");
    expect(wrapper.text()).toContain("payment-service 超时");
    expect(wrapper.text()).toContain("88%");
    expect(wrapper.text()).toContain("query_trace");
  });
});
