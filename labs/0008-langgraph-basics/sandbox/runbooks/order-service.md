# order-service 排障 runbook

## Connection pool / 连接池耗尽

症状：`db_pool_in_use` 接近 `db_pool_max`，P95 延迟上升，错误率升高。

排查顺序：

1. 调用 `get_service_health`，确认 connection pool 使用率与错误率。
2. 检查慢查询与 N+1；优先看 `OrderQueryService.findRecentOrders`。
3. 对照最近一次发布版本（当前 v2.14.1）。

处置：

- 不要只重启：重启只能短暂释放连接，N+1 会再次耗尽连接池。
- 优先回滚到上一个版本，或修复 N+1（批量查询）。
- 如果必须临时扩容，把 `db_pool_max` 从 50 调到 80，并记录变更单。

## Rollback / 回滚

回滚前必须确认：

- 当前版本与目标版本的 schema 兼容；
- 没有正在执行的批量任务；
- on-call 负责人已批准。
