# 事故复盘：2026-10-24 order-service 连接池耗尽

## 摘要

2026-10-24 20:10，order-service 发布 v2.14.1。20:12 起 5xx 从 0.2% 升到 18%，p99 从 180ms 升到 2400ms，连接池 `db_pool.used` 达到 48/50。20:40 回滚到 v2.14.0，20:46 指标恢复。

## 时间线

- 20:10 v2.14.1 部署完成，commit a1b2c3d
- 20:12 监控出现 `DBPoolExhausted` 告警
- 20:13 日志出现 `HikariPool-1 - Connection is not available, request timed out after 3000ms`
- 20:18 on-call 确认 `db_pool.used=48`，`db_pool.max=50`
- 20:25 定位到最近部署；申请回滚审批
- 20:40 执行 `kubectl rollout undo deploy/order-service`
- 20:46 p99 回落到 210ms，5xx 回落到 0.3%

## 根因

v2.14.1 在 `OrderQueryService.findRecentOrders` 中把批量查询改成了循环查询：每个订单调用一次 `orderItemRepository.findByOrderId`，形成 N+1。单个请求从 2 次 SQL 变成 1 + N 次 SQL；默认 `limit=20` 时就是 21 次，连接持有时间变长，连接池被迅速占满。

## 证据

- `OrderQueryService.findRecentOrders` 中存在 `for (Order order : orders)` 循环查询
- 日志：`HikariPool-1 - Connection is not available`
- 指标：`db_pool.used=48/50`，p99=2400ms
- 部署：v2.14.1 / a1b2c3d

## 处置

- 20:40 回滚到 v2.14.0
- 后续修复：恢复批量查询 `findByOrderIdIn`，并加一条慢查询告警
- 对 `orders.batch-fetch` 开关做灰度验证

## 后续

- 在 CI 中加入查询次数断言，防止 N+1 回归
- 为 order-service 增加连接池使用率告警（>80% 持续 5 分钟）
