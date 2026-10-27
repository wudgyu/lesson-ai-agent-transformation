package com.example.order.service;

import com.example.order.model.Order;
import com.example.order.model.OrderItem;
import com.example.order.repository.OrderItemRepository;
import com.example.order.repository.OrderRepository;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class OrderQueryService {

    private final OrderRepository orderRepository;
    private final OrderItemRepository itemRepository;

    public OrderQueryService(OrderRepository orderRepository,
                             OrderItemRepository itemRepository) {
        this.orderRepository = orderRepository;
        this.itemRepository = itemRepository;
    }

    /**
     * v2.14.0: 批量查询，2 次 SQL。
     * v2.14.1: 循环查询，1 + N 次 SQL，导致连接池耗尽。
     */
    public List<OrderView> findRecentOrders(Long userId, int limit) {
        List<Order> orders = orderRepository.findRecentByUserId(userId, limit);
        for (Order order : orders) {
            // N+1: 每个订单单独查一次明细
            List<OrderItem> items = itemRepository.findByOrderId(order.getId());
            order.setItems(items);
        }
        return orders.stream().map(OrderView::from).toList();
    }

    public OrderView findOrder(Long orderId) {
        Order order = orderRepository.findById(orderId)
                .orElseThrow(() -> new OrderNotFoundException(orderId));
        return OrderView.from(order);
    }
}
