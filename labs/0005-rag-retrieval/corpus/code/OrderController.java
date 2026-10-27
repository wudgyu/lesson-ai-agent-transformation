package com.example.order.api;

import com.example.order.service.OrderQueryService;
import com.example.order.service.OrderView;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderQueryService queryService;

    public OrderController(OrderQueryService queryService) {
        this.queryService = queryService;
    }

    @GetMapping
    public List<OrderView> recent(@RequestParam Long userId,
                                  @RequestParam(defaultValue = "20") int limit) {
        return queryService.findRecentOrders(userId, limit);
    }

    @GetMapping("/{id}")
    public OrderView detail(@PathVariable Long id) {
        return queryService.findOrder(id);
    }
}
