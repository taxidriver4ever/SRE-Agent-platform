package local.srelab.order.domain;

import java.math.BigDecimal;

/** 订单明细值对象；价格保留为 BigDecimal，避免实验业务引入浮点金额误差。 */
public record OrderItem(long productId, String sku, int quantity, BigDecimal unitPrice) {
    /** 创建订单前执行最小领域校验，防止无效数量流入库存预占。 */
    public OrderItem {
        if (productId <= 0 || sku == null || !sku.matches("SKU-[0-9]{1,6}") || unitPrice == null || unitPrice.signum() <= 0 || unitPrice.scale() > 2 || quantity <= 0 || quantity > 100) {
            throw new IllegalArgumentException("quantity must be positive");
        }
    }
}
