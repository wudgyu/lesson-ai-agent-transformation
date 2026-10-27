# 事故复盘：2026-09-18 payment-service 超时

## 摘要

payment-service 在 09-18 15:20 出现 p99 超时，错误码 `TS-999`。根因是下游 bank-gateway 的 TLS 握手超时，不是 order-service 的连接池问题。

## 根因

bank-gateway 在 15:10 更换证书后，旧客户端未更新信任链，TLS 握手在 5s 后超时。payment-service 的重试放大了流量，进一步拉高延迟。

## 证据

- 日志：`bank-gateway handshake timeout`
- 错误码：`TS-999`（下游超时）
- 指标：payment-service p99 从 180ms 升到 4200ms
- payment-service 自身连接池正常（`db_pool.used=12/40`）

## 处置

- 更新 truststore，重启 payment-service（审批单 OPS-1841）
- 关闭对 bank-gateway 的指数退避重试，改为熔断
- 增加 TLS 证书过期告警
