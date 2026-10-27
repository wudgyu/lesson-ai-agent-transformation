# 事故复盘：2026-08-02 inventory-service 缓存击穿

## 摘要

inventory-service 在 08-02 09:05 出现数据库 CPU 100%，大量请求绕过缓存直接打到数据库。根因是热门商品缓存同时过期，形成缓存击穿。

## 根因

热点 key `inventory:sku:10086` 的 TTL 在同一秒到期，数千个请求同时回源数据库。数据库连接数被打满，但 order-service 的连接池未受影响。

## 证据

- 日志：`cache miss for inventory:sku:10086`
- 指标：inventory-service 数据库 QPS 从 200 升到 8000，CPU 100%
- 连接池：inventory-service `db_pool.used=30/30`

## 处置

- 对热点 key 使用逻辑过期 + 互斥锁重建
- 增加随机 TTL 抖动，避免同时过期
- 为缓存击穿增加单独的告警
