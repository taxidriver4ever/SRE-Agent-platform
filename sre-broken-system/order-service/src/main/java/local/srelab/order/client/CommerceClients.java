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
        SimpleClientHttpRequestFactory timeouts = new SimpleClientHttpRequestFactory();
        timeouts.setConnectTimeout(Duration.ofMillis(500));
        timeouts.setReadTimeout(Duration.ofMillis(1200));
        inventory = RestClient.builder().baseUrl(inventoryUrl).requestFactory(timeouts).build();
        users = RestClient.builder().baseUrl(userUrl).requestFactory(timeouts).build();
        payments = RestClient.builder().baseUrl(paymentUrl).requestFactory(timeouts).build();
        notifications = RestClient.builder().baseUrl(notificationUrl).requestFactory(timeouts).build();
    }

    /** 查询用户状态；被冻结用户不能创建订单。 */
    @SuppressWarnings("unchecked")
    public Map<String, Object> getUser(long userId) {
        return users.get().uri("/users/{id}", userId).retrieve().body(Map.class);
    }

    /** 通过 Kubernetes Service 预占库存，绝不直接访问 Pod IP。 */
    public void reserve(OrderItem item, String reservationId) {
        inventory.post().uri("/inventory/reservations")
                .body(Map.of("sku", item.sku(), "quantity", item.quantity(), "reservation_id", reservationId))
                .retrieve().toBodilessEntity();
    }

    /** 支付服务保存支付记录并返回受理状态。 */
    @SuppressWarnings("unchecked")
    public Map<String, Object> pay(long orderId, String amount) {
        return payments.post().uri("/payments")
                .body(Map.of("order_id", orderId, "amount", amount))
                .retrieve().body(Map.class);
    }

    /** 通知是创建订单后的非核心步骤；失败由上层记录但不回滚已完成支付。 */
    public void notifyCreated(long orderId, long userId) {
        notifications.post().uri("/notifications")
                .body(Map.of("type", "ORDER_CREATED", "order_id", orderId, "user_id", userId))
                .retrieve().toBodilessEntity();
    }
}
