import { expect, test } from "@playwright/test";

function response(body, status = 200, contentType = "application/json") {
  return { status, contentType, body: typeof body === "string" ? body : JSON.stringify(body) };
}

async function installFakeBackend(page, state = { conversations: [] }) {
  await page.route("**/mock-api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/mock-api/, "");
    if (request.method() === "OPTIONS") return route.fulfill(response("", 204, "text/plain"));
    if (path === "/api/auth/login") return route.fulfill(response({ access_token: "e2e-token", user: { id: "u1", username: "tester" } }));
    if (path === "/api/auth/me") return route.fulfill(response({ id: "u1", username: "tester" }));
    if (path === "/api/services") return route.fulfill(response({ items: [
      { id: "order-service", name: "order-service", description: "订单服务", owner: "order-team", status: "warning", metrics: { p95_ms: 420, error_rate: 2.1, cpu_percent: 50, memory_percent: 60 }, dependencies: ["payment-service"], upstreams: ["gateway"] },
      { id: "payment-service", name: "payment-service", description: "支付服务", owner: "payment-team", status: "critical", metrics: { p95_ms: 1800, error_rate: 8.7, cpu_percent: 70, memory_percent: 81 }, dependencies: [], upstreams: ["order-service"] },
    ] }));
    if (path === "/api/validations") return route.fulfill(response({ items: [] }));
    if (path === "/api/validation-test-suites") return route.fulfill(response({ items: [] }));
    if (path === "/api/conversations" && request.method() === "GET") return route.fulfill(response(state.conversations));
    if (path === "/api/conversations" && request.method() === "POST") {
      const item = { id: "conv-1", title: "新事件诊断", message_count: 0, updated_at: "2026-09-08T00:00:00Z" };
      state.conversations = [item];
      return route.fulfill(response(item, 201));
    }
    if (path === "/api/conversations/conv-1") return route.fulfill(response({
      id: "conv-1", title: "订单支付超时", messages: [
        { id: "m1", role: "user", message_type: "user", content: { message: "订单支付为什么超时" } },
        { id: "m2", role: "assistant", message_type: "assistant", content: { decision_summary: "历史诊断已恢复", root_cause: "payment-db pool exhausted", confidence: 0.93 } },
      ],
    }));
    if (path === "/api/agent/chat/stream") {
      if (state.failNextChat) {
        state.failNextChat = false;
        return route.abort("internetdisconnected");
      }
      state.conversations = [{ id: "conv-1", title: "订单支付超时", message_count: 2, updated_at: "2026-09-08T00:00:03Z" }];
      const events = [
        { type: "conversation", conversation_id: "conv-1" },
        { type: "intent", intent: "SPECIFIC_INCIDENT" },
        { type: "phase", phase: "TRIAGE" },
        { type: "tool", record: { tool_name: "query_trace", result_summary: "payment span slow" } },
        { type: "final", report: { decision_summary: "定位完成", root_cause: "payment-db pool exhausted", confidence: 0.93, root_cause_chain: ["order-service", "payment-service", "payment-db"] } },
      ];
      return route.fulfill(response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), 200, "text/event-stream"));
    }
    return route.fulfill(response({ detail: `unhandled ${request.method()} ${path}` }, 404));
  });
}

async function login(page) {
  await page.goto("/");
  await page.getByLabel("用户名").fill("tester");
  await page.getByLabel("密码").fill("correct-pass");
  await page.getByRole("button", { name: "登录 Console" }).click();
  await expect(page.getByRole("heading", { name: "服务目录" })).toBeVisible();
}

test("login -> service catalog", async ({ page }) => {
  await installFakeBackend(page);
  await login(page);
  await expect(page.getByText("order-service", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("420 ms")).toBeVisible();
});

test("multi-service diagnosis -> deterministic SSE result", async ({ page }) => {
  const state = { conversations: [] };
  await installFakeBackend(page, state);
  await login(page);
  await page.getByRole("button", { name: /事件诊断/ }).first().click();
  await page.locator(".service-choices").getByRole("button", { name: "order-service" }).click();
  await page.locator(".service-choices").getByRole("button", { name: "payment-service" }).click();
  await page.getByPlaceholder(/描述服务异常/).fill("订单支付为什么超时");
  await page.getByRole("button", { name: "发送诊断" }).click();
  await expect(page.getByText("payment-db pool exhausted")).toBeVisible();
  await page.getByText(/查看 1 个检索/).click();
  await expect(page.getByText("query_trace")).toBeVisible();
  await expect(page.getByText("93%")).toBeVisible();
});

test("refresh -> open persisted conversation history", async ({ page }) => {
  const state = { conversations: [{ id: "conv-1", title: "订单支付超时", message_count: 2, updated_at: "2026-09-08T00:00:03Z" }] };
  await installFakeBackend(page, state);
  await login(page);
  await page.getByRole("button", { name: /事件诊断/ }).first().click();
  await page.reload();
  await page.locator(".conversation-browser").getByRole("button", { name: /订单支付超时/ }).click();
  await expect(page.getByText("历史诊断已恢复")).toBeVisible();
  await expect(page.getByText("payment-db pool exhausted")).toBeVisible();
});

test("temporary network disconnect leaves UI retryable after recovery", async ({ page }) => {
  const state = { conversations: [], failNextChat: false };
  await installFakeBackend(page, state);
  await login(page);
  await page.getByRole("button", { name: /事件诊断/ }).first().click();
  state.failNextChat = true;
  await page.getByPlaceholder(/描述服务异常/).fill("第一次请求");
  await page.getByRole("button", { name: "发送诊断" }).click();
  await expect(page.getByText(/Failed to fetch|fetch failed|诊断请求失败/i)).toBeVisible();
  await page.getByPlaceholder(/描述服务异常/).fill("恢复后重试");
  await expect(page.getByRole("button", { name: "发送诊断" })).toBeEnabled();
  await page.getByRole("button", { name: "发送诊断" }).click();
  await expect(page.getByText("payment-db pool exhausted")).toBeVisible();
});
