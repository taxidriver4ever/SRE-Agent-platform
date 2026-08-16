package local.srelab.order.service;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import java.util.List;
import local.srelab.order.domain.OrderItem;
import org.junit.jupiter.api.Test;

/** 纯领域计算测试不依赖 MySQL，保证本地和容器构建都能快速运行。 */
class OrderApplicationServiceTest {
    @Test
    void calculatesTotalFromQuantityAndUnitPrice() {
        List<OrderItem> items = List.of(
                new OrderItem(1, "SKU-1", 2, new BigDecimal("12.50")),
                new OrderItem(2, "SKU-2", 1, new BigDecimal("5.00")));

        assertThat(OrderApplicationService.totalOf(items)).isEqualByComparingTo("30.00");
    }
}
