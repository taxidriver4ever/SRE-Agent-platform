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
        Map<String, Object> user = clients.getUser(command.userId());
        if ("SUSPENDED".equals(user.get("status"))) {
            throw new IllegalStateException("suspended user cannot create orders");
        }
        String reservationId = "order-pending-" + System.nanoTime();
        command.items().forEach(item -> clients.reserve(item, reservationId));
        BigDecimal total = command.items().stream()
                .map(item -> item.unitPrice().multiply(BigDecimal.valueOf(item.quantity())))
                .reduce(BigDecimal.ZERO, BigDecimal::add);
        long orderId = orders.create(command, total);
        clients.pay(orderId, total.toPlainString());
        try {
            clients.notifyCreated(orderId, command.userId());
        } catch (RuntimeException notificationError) {
            log.warn("notification failed order_id={} error={}", orderId, notificationError.toString());
        }
        return orders.find(orderId);
    }

    /** 查询订单并在 retry_storm 模式复现无退避请求放大。 */
    public Order get(long orderId) {
        int attempts = faults.mode().equals("retry_storm") ? 5 : 1;
        RuntimeException last = null;
        for (int attempt = 1; attempt <= attempts; attempt++) {
            try {
                return orders.find(orderId);
            } catch (RuntimeException error) {
                last = error;
                retryCounter.increment();
            }
        }
        throw last == null ? new IllegalStateException("order not found") : last;
    }

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
