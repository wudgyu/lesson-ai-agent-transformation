# Runbook: 部署与回滚

## 部署前

- 确认变更已通过集成测试
- 确认数据库迁移可向前兼容
- 记录当前版本与 commit，便于回滚
- 对高风险服务设置 30 分钟的观察窗口

## 回滚

```bash
kubectl rollout undo deploy/order-service
kubectl rollout status deploy/order-service
```

## 验证

1. 查看 `get_service_health` 的 `recent_deploy` 与 `status`
2. 用 `query_logs` 检查 `ERROR` 是否下降
3. 确认 p99 与 5xx 比例回到基线
4. 检查连接池 `db_pool.used` 是否回落

## 注意

- 回滚也是写操作，需要 on-call 负责人审批（审批单号必须记录）
- 回滚后不要立刻再次部署同一版本；先定位根因
- 如果数据库迁移不可逆，回滚前必须确认兼容性
