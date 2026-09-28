package local.srelab.order.service;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import local.srelab.order.client.CommerceClients;
import local.srelab.order.config.FaultState;
import local.srelab.order.domain.CreateOrderCommand;
import local.srelab.order.domain.Order;
import local.srelab.order.domain.OrderItem;
import local.srelab.order.repository.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/** 编排订单业务与跨服务调用，Controller 只负责 HTTP 输入输出。 */
@Service
public class OrderApplicationService {
    private static final Logger log = LoggerFactory.getLogger(OrderApplicationService.class);
    private final OrderRepository orders;
    private final CommerceClients clients;
    private final FaultState faults;
    private final Counter retryCounter;

    public OrderApplicationService(OrderRepository orders, CommerceClients clients, FaultState faults,
                                   MeterRegistry registry) {
        this.orders = orders;
        this.clients = clients;
        this.faults = faults;
        retryCounter = Counter.builder("sre_dependency_retries_total")
                .tag("service", "order-service").register(registry);
    }

    /** 创建订单前验证用户并预占每个 SKU，随后支付和发送通知。 */
    public Order create(CreateOrderCommand command) {
        // Optional fixed business processing time for the gateway budget experiment.
        int processingMs = Integer.parseInt(System.getenv().getOrDefault("ORDER_PROCESSING_DELAY_MS", "0"));
        if (processingMs > 0) try { Thread.sleep(Math.min(processingMs, 2000)); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IllegalStateException("order interrupted", e); }
        Map<String, Object> user = clients.getUser(command.userId());
        if (user == null || !"ACTIVE".equalsIgnoreCase(String.valueOf(user.get("status")))) throw new IllegalStateException("user is not allowed to order");
        long orderId = orders.create(command, totalOf(command.items()));
        java.util.List<String> holds = new java.util.ArrayList<>();
        try {
            for (OrderItem item : command.items()) {
                String key = "order-" + orderId + "-" + item.sku();
                holds.add(key); clients.reserve(item, key);
            }
        } catch (RuntimeException failure) {
            boolean released = true;
            for (String key : holds) {
                try { clients.release(key); }
                catch (RuntimeException e) { released = false; log.warn("inventory compensation requires review order_id={}", orderId); }
            }
            orders.setStatus(orderId, released ? "FAILED" : "REVIEW_REQUIRED");
            throw failure;
        }
        orders.setStatus(orderId, "PAYMENT_PENDING");
        return completePayment(orderId);
    }

    /** Repeat a durable payment intent using the original idempotency key. */
    public Order completePayment(long orderId) {
        Order order = orders.find(orderId);
        if ("PAID".equals(order.status())) return order;
        if (!"PAYMENT_PENDING".equals(order.status())) throw new IllegalStateException("order requires inventory reconciliation");
        int attempts = java.util.Set.of("retry_storm", "retry_amplification").contains(faults.mode()) ? 3 : 1;
        Map<String, Object> payment = null;
        for (int attempt = 0; attempt < attempts; attempt++) {
            try { payment = clients.pay(orderId, order.totalAmount().toPlainString()); break; }
            catch (org.springframework.web.client.RestClientException failure) {
                if (failure instanceof org.springframework.web.client.RestClientResponseException r && r.getStatusCode().is4xxClientError()) throw failure;
                if (attempt + 1 == attempts) throw failure;
                retryCounter.increment();
                try { Thread.sleep(50L * (attempt + 1)); } catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IllegalStateException("retry interrupted", e); }
            }
        }
        if (payment == null || payment.get("id") == null) throw new IllegalStateException("invalid payment response");
        // Timeout leaves PAYMENT_PENDING; confirm and commit are safe to repeat.
        clients.confirm(payment.get("id").toString());
        for (OrderItem item : order.items()) clients.commit("order-" + orderId + "-" + item.sku());
        orders.setStatus(orderId, "PAID");
        try { clients.notifyCreated(orderId, order.userId()); }
        catch (RuntimeException error) { log.warn("notification_partial_failure order_id={} error_type={}", orderId, error.getClass().getSimpleName()); }
        return orders.find(orderId);
    }

    public Order get(long orderId) { return orders.find(orderId); }

    public List<Map<String, Object>> search(String email, int limit) {
        if (faults.mode().equals("single_pod_slow")) {
            // SRE-008 只在被选中的 Pod 执行真实 CPU 密集质数计算。其他副本继续正常
            // 响应，因此 Service 负载均衡后会表现为“有时快、有时慢”。
            long primeCount = 0;
            for (int candidate = 2; candidate < 650_000; candidate++) {
                boolean prime = true;
                for (int divisor = 2; divisor * divisor <= candidate; divisor++) {
                    if (candidate % divisor == 0) { prime = false; break; }
                }
                if (prime) { primeCount++; }
            }
            log.warn("single pod degradation CPU work completed primes={}", primeCount);
        }
        orders.holdConnectionForPoolScenario();
        return orders.searchByEmail(email, Math.min(Math.max(limit, 1), 100));
    }

    public List<Map<String, Object>> list(long afterId, int limit) {
        return orders.listAfter(afterId, Math.min(Math.max(limit, 1), 100));
    }

    public boolean cancel(long orderId) {
        return orders.cancel(orderId);
    }

    /** 测试辅助：纯函数计算总价，不触发网络或数据库。 */
    public static BigDecimal totalOf(List<OrderItem> items) {
        return items.stream().map(item -> item.unitPrice().multiply(BigDecimal.valueOf(item.quantity())))
                .reduce(BigDecimal.ZERO, BigDecimal::add);
    }
}
