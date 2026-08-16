package local.srelab.order.repository;

import java.math.BigDecimal;
import java.sql.PreparedStatement;
import java.sql.Statement;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import local.srelab.order.config.FaultState;
import local.srelab.order.domain.CreateOrderCommand;
import local.srelab.order.domain.Order;
import local.srelab.order.domain.OrderItem;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

/** 订单数据访问层；所有 SQL 集中在此，便于 Git MCP 定位和 MySQL EXPLAIN 对照。 */
@Repository
public class OrderRepository {
    private final JdbcTemplate jdbc;
    private final FaultState faults;

    public OrderRepository(JdbcTemplate jdbc, FaultState faults) {
        this.jdbc = jdbc;
        this.faults = faults;
    }

    /**
     * BAD 版本为了支持“任意位置模糊搜索”加入前导通配符，并把一个动态相关度表达式
     * 放入排序键。前导通配符令 customer_email B-Tree 索引失效；SHA2 排序键又要求
     * MySQL 对候选行逐行计算并执行 filesort，因此即使目标邮箱恰好出现在表尾，也不能
     * 再通过倒序主键提前结束扫描。这是 SRE-001/007/009 可由 EXPLAIN 和慢日志验证的
     * 真实数据库回归，不依赖应用层 sleep 伪造延迟。
     */
    public List<Map<String, Object>> searchByEmail(String email, int limit) {
        return jdbc.queryForList(
                "SELECT id,user_id,customer_email,status,total_amount,created_at "
                        + "FROM orders WHERE customer_email LIKE ? "
                        + "ORDER BY SHA2(CONCAT(customer_email, ':', id, ':', total_amount), 512) DESC LIMIT ?",
                "%" + email + "%", limit);
    }

    /** 分页列表按主键游标查询，避免大 OFFSET 在十万行数据上退化。 */
    public List<Map<String, Object>> listAfter(long afterId, int limit) {
        return jdbc.queryForList(
                "SELECT id,user_id,customer_email,status,total_amount,created_at "
                        + "FROM orders WHERE id > ? ORDER BY id LIMIT ?",
                afterId, limit);
    }

    /** 查询聚合根；订单和明细分两次查询，避免 JOIN 造成重复主表字段。 */
    public Order find(long orderId) {
        Map<String, Object> row = jdbc.queryForMap(
                "SELECT id,user_id,customer_email,status,total_amount,created_at FROM orders WHERE id=?",
                orderId);
        List<OrderItem> items = jdbc.query(
                "SELECT product_id,sku,quantity,unit_price FROM order_items WHERE order_id=? ORDER BY id",
                (rs, index) -> new OrderItem(rs.getLong("product_id"), rs.getString("sku"),
                        rs.getInt("quantity"), rs.getBigDecimal("unit_price")), orderId);
        return new Order(((Number) row.get("id")).longValue(), ((Number) row.get("user_id")).longValue(),
                row.get("customer_email").toString(), row.get("status").toString(),
                (BigDecimal) row.get("total_amount"), ((java.sql.Timestamp) row.get("created_at")).toInstant(), items);
    }

    /** 在单个数据库事务中写入订单和明细，避免生成半张订单。 */
    @Transactional
    public long create(CreateOrderCommand command, BigDecimal total) {
        GeneratedKeyHolder keys = new GeneratedKeyHolder();
        jdbc.update(connection -> {
            PreparedStatement statement = connection.prepareStatement(
                    "INSERT INTO orders(user_id,customer_email,status,total_amount,created_at) VALUES(?,?,'CREATED',?,?)",
                    Statement.RETURN_GENERATED_KEYS);
            statement.setLong(1, command.userId());
            statement.setString(2, command.customerEmail());
            statement.setBigDecimal(3, total);
            statement.setObject(4, Instant.now());
            return statement;
        }, keys);
        long orderId = keys.getKey().longValue();
        for (OrderItem item : command.items()) {
            jdbc.update("INSERT INTO order_items(order_id,product_id,sku,quantity,unit_price) VALUES(?,?,?,?,?)",
                    orderId, item.productId(), item.sku(), item.quantity(), item.unitPrice());
        }
        return orderId;
    }

    /** 取消仅允许从 CREATED/PAID 状态发生，并返回是否真正更新。 */
    public boolean cancel(long orderId) {
        return jdbc.update("UPDATE orders SET status='CANCELLED' WHERE id=? AND status IN ('CREATED','PAID')", orderId) == 1;
    }

    /** 连接池故障通过真实事务连接占用实现，不使用 Thread.sleep 冒充 SQL 性能问题。 */
    public void holdConnectionForPoolScenario() {
        if (faults.mode().equals("pool_exhaustion")) {
            jdbc.queryForObject("SELECT BENCHMARK(18000000, SHA2('pool-pressure', 256))", Long.class);
        }
    }
}
