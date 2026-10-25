# 架构：订单链路

## 服务拓扑

- order-service (8081) 依赖 payment-service (8082) 与 inventory-service (8083)
- payment-service 依赖外部 bank-gateway
- 所有服务共享 PostgreSQL 集群 `pg-prod`，但各自使用独立数据库与连接池
- 每个服务都有独立的 `HikariPool`，一个服务的连接池耗尽不会直接耗尽另一个服务的连接池

## 订单查询链路

1. `OrderController` 接收 `GET /api/orders`
2. `OrderQueryService.findRecentOrders` 查询订单主表
3. 对每个订单查询订单明细
4. `OrderView` 聚合后返回

## 依赖与故障传播

- order-service 的连接池耗尽不会直接耗尽 payment-service 的连接池
- payment-service 的 `TS-999` 表示下游超时，不等于 order-service 故障
- inventory-service 的缓存击穿会打满它自己的数据库连接，但不会占用 order-service 的连接池
- 排障时先确认故障属于哪个服务、哪个连接池，不要被“连接池打满”这个共同症状误导
