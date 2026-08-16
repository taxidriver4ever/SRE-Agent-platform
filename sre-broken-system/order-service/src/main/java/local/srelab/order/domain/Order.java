package local.srelab.order.domain;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

/**
 * 订单聚合根。该记录同时承载订单主表与明细，避免 Controller 直接暴露 JDBC 行结构。
 * 不使用 JPA 是为了让 SRE-001 的 SQL 和 EXPLAIN 完全可见、可控。
 */
public record Order(
        long id,
        long userId,
        String customerEmail,
        String status,
        BigDecimal totalAmount,
        Instant createdAt,
        List<OrderItem> items) {
}
