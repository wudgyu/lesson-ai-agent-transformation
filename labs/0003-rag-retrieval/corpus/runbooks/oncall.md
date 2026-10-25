# Runbook: On-call 通用处置

## 原则

1. 先止血，再定位根因
2. 任何重启、回滚、扩缩容都必须有审批单号
3. 结论必须引用证据：指标、日志行、部署版本
4. 15 分钟内无法止血，升级到 on-call lead

## 常见告警

- `HighErrorRate`：先看 `get_service_health` 的 `error_rate_5m`
- `HighLatency`：看 `p99_latency_ms` 与 slow query
- `DBPoolExhausted`：按 `runbook-db-pool` 处理
- `DeployRegression`：按 `runbook-deploy` 回滚

## 升级路径

- 15 分钟内无法止血：升级到 on-call lead
- 涉及资金/订单数据：同步业务负责人
- 需要改配置或重启：先申请审批，再执行
- 事故结束后 48 小时内完成复盘，并把新发现写回 runbook
