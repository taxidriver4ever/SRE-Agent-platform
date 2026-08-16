package local.srelab.order.domain;

import java.math.BigDecimal;

/** 订单明细值对象；价格保留为 BigDecimal，避免实验业务引入浮点金额误差。 */
public record OrderItem(long productId, String sku, int quantity, BigDecimal unitPrice) {
    /** 创建订单前执行最小领域校验，防止无效数量流入库存预占。 */
    public OrderItem {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive");
        }
    }
}
