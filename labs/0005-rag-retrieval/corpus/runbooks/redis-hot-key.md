# Runbook: Redis 热 key

## 症状

- 单个 key 的 QPS 远高于其他 key
- Redis CPU 升高，但数据库连接池正常
- 延迟抖动，超时集中在少数请求
- 典型告警名：`RedisHotKey`

## 诊断

1. 用 `redis-cli --hotkeys` 或监控面板找热 key
2. 确认 key 的 TTL 是否集中过期
3. 检查是否存在缓存击穿或大 key
4. 对比数据库连接池指标；热 key 通常不会直接打满数据库连接

## 处置

- 热 key 拆分、本地缓存、逻辑过期
- 不要用重启数据库或扩大连接池来解决 Redis 热 key
- 对热点 key 增加随机 TTL 抖动，避免同时过期
