# Runbook: 数据库连接池耗尽

## 症状

- 5xx 比例上升，日志出现 `HikariPool-1 - Connection is not available`
- p99 延迟显著上升，`db_pool.used` 接近 `max`
- 下游重试增加，但重启未必能解决

## 诊断

1. 查看 `get_service_health` 中的 `db_pool.used` / `db_pool.max`
2. 用 `query_logs` 过滤 `ERROR`，确认是否存在 HikariPool timeout
3. 检查最近部署是否修改了连接池配置或查询逻辑
4. 用 `query_logs` 过滤 `WARN`，寻找 slow query 与连接泄漏线索

## 处置

- 短期：扩容副本，或临时上调连接池的 `max-active`（需要审批）
- 中期：修复慢查询 / N+1，确认连接池没有泄漏
- 回滚：`kubectl rollout undo deploy/order-service`（需要审批；回滚后连接池会重置）

## 注意

- 不要直接重启，先确认连接池指标与最近部署
- `order-service` 的连接池上限 `max-active` 当前为 50
- 重启只能暂时缓解；如果慢查询还在，连接池会再次耗尽
