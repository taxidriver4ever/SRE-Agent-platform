package local.srelab.order.client;

import java.time.Duration;
import java.util.Map;
import local.srelab.order.domain.OrderItem;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

/**
 * 集中封装订单服务的四个 HTTP 下游。OpenTelemetry Java Agent 会自动传播
 * traceparent，使 Java→Go/Python/Node 的调用出现在同一条 Trace 中。
 */
@Component
public class CommerceClients {
    private final RestClient inventory;
    private final RestClient users;
    private final RestClient payments;
    private final RestClient notifications;

    public CommerceClients(
            @Value("${dependencies.inventory}") String inventoryUrl,
            @Value("${dependencies.user}") String userUrl,
            @Value("${dependencies.payment}") String paymentUrl,
            @Value("${dependencies.notification}") String notificationUrl) {
        inventory = client(inventoryUrl, 800);
        users = client(userUrl, 500);
        payments = client(paymentUrl, 2000);
        notifications = client(notificationUrl, 500);
    }
    private RestClient client(String url, int readMs) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofMillis(200));
        factory.setReadTimeout(Duration.ofMillis(readMs));
        return RestClient.builder().baseUrl(url).requestFactory(factory)
            .requestInterceptor((request, body, execution) -> {
                String requestId = org.slf4j.MDC.get("request_id");
                if (requestId != null) request.getHeaders().set("x-request-id", requestId);
                return execution.execute(request, body);
            }).build();
    }

    /** 查询用户状态；被冻结用户不能创建订单。 */
    @SuppressWarnings("unchecked")
    public Map<String, Object> getUser(long userId) {
        return users.get().uri("/users/{id}/status", userId).retrieve().body(Map.class);
    }

    /** 通过 Kubernetes Service 预占库存，绝不直接访问 Pod IP。 */
    public void reserve(OrderItem item, String reservationId) {
        inventory.post().uri("/inventory/reserve")
                .body(Map.of("sku", item.sku(), "quantity", item.quantity(), "reservation_id", reservationId))
                .retrieve().toBodilessEntity();
    }

    /** 支付服务保存支付记录并返回受理状态。 */
    @SuppressWarnings("unchecked")
    public Map<String, Object> pay(long orderId, String amount) {
        return payments.post().uri("/payments")
                .header("X-Idempotency-Key", "order-" + orderId)
                .body(Map.of("order_id", orderId, "amount", amount, "idempotency_key", "order-" + orderId))
                .retrieve().body(Map.class);
    }

    /** 通知是创建订单后的非核心步骤；失败由上层记录但不回滚已完成支付。 */
    public void notifyCreated(long orderId, long userId) {
        notifications.post().uri("/notifications")
                .body(Map.of("type", "ORDER_CREATED", "order_id", orderId, "user_id", userId))
                .retrieve().toBodilessEntity();
    }

    public void release(String id) { inventory.post().uri("/inventory/release").body(Map.of("reservation_id", id)).retrieve().toBodilessEntity(); }
    public void commit(String id) { inventory.post().uri("/inventory/commit").body(Map.of("reservation_id", id)).retrieve().toBodilessEntity(); }
    public void confirm(String id) { payments.post().uri("/payments/{id}/confirm", id).retrieve().toBodilessEntity(); }
}
