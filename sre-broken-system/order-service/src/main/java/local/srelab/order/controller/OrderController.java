package local.srelab.order.controller;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import local.srelab.order.domain.CreateOrderCommand;
import local.srelab.order.domain.Order;
import local.srelab.order.domain.OrderItem;
import local.srelab.order.service.OrderApplicationService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/** 电商订单 API：创建、详情、列表、搜索和取消均使用独立业务方法。 */
@RestController
@RequestMapping("/orders")
public class OrderController {
    private final OrderApplicationService service;

    public OrderController(OrderApplicationService service) {
        this.service = service;
    }

    @PostMapping
    public ResponseEntity<Order> create(@RequestBody CreateOrderRequest request) {
        List<OrderItem> items = request.items().stream()
                .map(item -> new OrderItem(item.productId(), item.sku(), item.quantity(), item.unitPrice()))
                .toList();
        return ResponseEntity.status(201).body(service.create(
                new CreateOrderCommand(request.userId(), request.customerEmail(), items)));
    }

    @GetMapping("/{id}")
    public Order detail(@PathVariable long id) {
        return service.get(id);
    }

    @GetMapping
    public List<Map<String, Object>> list(@RequestParam(defaultValue = "0") long afterId,
                                         @RequestParam(defaultValue = "20") int limit) {
        return service.list(afterId, limit);
    }

    @GetMapping("/search")
    public List<Map<String, Object>> search(@RequestParam String email,
                                           @RequestParam(defaultValue = "20") int limit) {
        return service.search(email, limit);
    }

    @PostMapping("/{id}/cancel")
    public ResponseEntity<Map<String, Object>> cancel(@PathVariable long id) {
        boolean cancelled = service.cancel(id);
        return cancelled ? ResponseEntity.ok(Map.of("order_id", id, "status", "CANCELLED"))
                : ResponseEntity.status(409).body(Map.of("order_id", id, "error", "order cannot be cancelled"));
    }

    /** 外部创建订单请求；嵌套记录保持 JSON Schema 清晰。 */
    public record CreateOrderRequest(long userId, String customerEmail, List<ItemRequest> items) {}
    public record ItemRequest(long productId, String sku, int quantity, BigDecimal unitPrice) {}
}
