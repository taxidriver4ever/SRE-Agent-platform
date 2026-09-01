/**
 * 仅供 VITE_UI_PREVIEW=true 的视觉评审数据。
 * 正式运行时从后端资源与 Diagnosis API 读取，不在此保存诊断结果。
 */

function previewService(id, description, status, p95, errorRate, cpu, memory, owner, runtime, version, dependencies = [], upstreams = []) {
  return {
    id, name: id, description, status, p95, errorRate, cpu, memory,
    updatedAt: "预览快照", owner, runtime, version,
    deployedAt: "预览数据", dependencies, upstreams,
  };
}

export const services = [
  previewService("gateway", "统一入口、鉴权、限流与推理后端路由", "healthy", "86 ms", "0.12%", 24, 41, "platform-team", "Python 3.12 / FastAPI", "gateway-preview", ["order-service"]),
  previewService("order-service", "订单创建、查询、取消与跨服务交易编排", "warning", "842 ms", "2.40%", 61, 68, "commerce-order-team", "Java 21 / Spring Boot 3", "order-preview", ["inventory-service", "user-service", "payment-service", "notification-service"], ["gateway"]),
  previewService("inventory-service", "库存查询、预占、释放与推荐预热", "healthy", "124 ms", "0.18%", 36, 47, "supply-chain-team", "Go 1.23 / net/http", "inventory-preview", ["recommendation-service"], ["order-service"]),
  previewService("payment-service", "支付授权、幂等查询、状态管理与退款", "critical", "1.82 s", "8.70%", 74, 81, "payment-platform-team", "Node.js 22 / TypeScript", "payment-preview", ["notification-service"], ["order-service"]),
  previewService("user-service", "用户资料、状态、会员等级与折扣", "healthy", "96 ms", "0.08%", 29, 52, "identity-team", "Python 3.12 / FastAPI", "user-preview", [], ["order-service"]),
  previewService("notification-service", "订单与支付异步通知、重试和状态查询", "warning", "436 ms", "1.90%", 48, 58, "engagement-team", "Go 1.23 / worker queue", "notification-preview", [], ["order-service", "payment-service"]),
  previewService("recommendation-service", "商品与用户推荐、缓存和排名", "healthy", "158 ms", "0.31%", 42, 63, "personalization-team", "Python 3.12 / FastAPI", "recommendation-preview", [], ["inventory-service"]),
];

export const allServices = services;

// 只用于预览模式的 Service Catalog 图；Diagnosis 图始终消费后端 Node/Edge。
export const graphNodes = [
  { id: "gateway", x: 304, y: 18 },
  { id: "order-service", x: 304, y: 96 },
  { id: "inventory-service", x: 76, y: 190 },
  { id: "user-service", x: 238, y: 190 },
  { id: "payment-service", x: 400, y: 190 },
  { id: "notification-service", x: 562, y: 190 },
  { id: "recommendation-service", x: 76, y: 284 },
];

export const graphEdges = [
  ["gateway", "order-service"],
  ["order-service", "inventory-service"],
  ["order-service", "user-service"],
  ["order-service", "payment-service"],
  ["order-service", "notification-service"],
  ["inventory-service", "recommendation-service"],
  ["payment-service", "notification-service"],
];

export function getService(serviceId) {
  return allServices.find((service) => service.id === serviceId) || {
    id: serviceId || "unknown", name: serviceId || "unknown", description: "暂无服务元数据",
    status: "unknown", p95: "—", errorRate: "—", cpu: 0, memory: 0,
    updatedAt: "暂无运行快照", owner: "unknown", runtime: "unknown",
    version: "—", deployedAt: "—", dependencies: [], upstreams: [],
  };
}

export function statusLabel(status) {
  return { healthy: "Healthy", warning: "Warning", critical: "Critical", unknown: "Unknown" }[status] || "Unknown";
}
