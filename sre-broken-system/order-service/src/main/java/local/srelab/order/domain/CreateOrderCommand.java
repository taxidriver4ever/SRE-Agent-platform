package local.srelab.order.domain;

import java.util.List;

/** API 层转换后的创建订单命令，明确区分外部请求与内部领域输入。 */
public record CreateOrderCommand(long userId, String customerEmail, List<OrderItem> items) {
    /** 订单必须至少包含一件商品，邮箱用于后续订单查询演示。 */
    public CreateOrderCommand {
        if (userId <= 0 || items == null || items.isEmpty() || items.size() > 20) {
            throw new IllegalArgumentException("order items cannot be empty");
        }
        if (items.stream().map(OrderItem::sku).distinct().count() != items.size()) throw new IllegalArgumentException("duplicate SKU; combine quantities");
        if (customerEmail == null || customerEmail.isBlank()) {
            throw new IllegalArgumentException("customerEmail is required");
        }
    }
}
