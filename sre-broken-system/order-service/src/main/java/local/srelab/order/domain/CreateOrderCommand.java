package local.srelab.order.domain;

import java.util.List;

/** API 层转换后的创建订单命令，明确区分外部请求与内部领域输入。 */
public record CreateOrderCommand(long userId, String customerEmail, List<OrderItem> items) {
    /** 订单必须至少包含一件商品，邮箱用于后续订单查询演示。 */
    public CreateOrderCommand {
        if (items == null || items.isEmpty()) {
            throw new IllegalArgumentException("order items cannot be empty");
        }
        if (customerEmail == null || customerEmail.isBlank()) {
            throw new IllegalArgumentException("customerEmail is required");
        }
    }
}
