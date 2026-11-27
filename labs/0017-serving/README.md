# 第 17 课 Lab：把 Agent 变成可部署的服务

这个 lab 不调用模型。它把“服务化”拆成五个可以独立验证的问题：

1. **入口**：谁可以创建 run？配额、并发、排队和拒绝分别保护什么？
2. **执行**：请求断开后 run 还活着吗？超时、取消和重试如何层层收敛？
3. **流式**：SSE 断线后从哪里恢复？事件被环形缓冲区淘汰后怎么办？
4. **完成**：最终结果存在哪里？为什么流不是数据库？
5. **关停**：SIGTERM 到来时，正在跑的 run 是丢失、重跑，还是从 checkpoint 恢复？

核心主张：

> 服务化不是给 Agent 套一层 HTTP，而是给它一个**并发与失败模型**：
> 请求会断、进程会死、下游会 503、客户端会重试。系统必须在这些前提下仍然给出
> 可解释、可恢复、不重复副作用的结果。

## 文件

| 文件 | 作用 |
|---|---|
| `models.py` | `RunRequest` / `Run` / `Event` / `Admission`；请求与 run 分离 |
| `errors.py` | 可重试 / 不可重试 / 预算耗尽 / 超时 / 取消的错误分类 |
| `clock.py` | 可注入时钟；`ControlledClock` 让关停测试停在确定 checkpoint |
| `limiter.py` | 令牌桶、重试预算、SRE 自适应限流公式 |
| `retry.py` | capped exponential backoff + full jitter、deadline、放大倍数 |
| `agent.py` | 确定性的 RepoMedic 5 阶段 Agent；R02/R03/R04 注入失败 |
| `store.py` | runs / idempotency / effect ledger；模拟可跨进程的持久层 |
| `run_service.py` | 核心：准入 → 队列 → worker → 事件日志 → checkpoint → 关停 |
| `sse.py` | SSE 线格式与解析器；不依赖框架 |
| `api.py` | FastAPI 适配层：202、Idempotency-Key、Last-Event-ID、429/503 |
| `service_cli.py` | `demo / load / retry / resume / shutdown / all` |
| `test_service.py` | 51 个离线核心测试，仅标准库 |
| `test_api.py` | 13 个可选 HTTP 测试，需要 FastAPI/httpx |

## 环境

核心 lab 只需要 Python ≥ 3.9 标准库。HTTP 层建议用 Python 3.12 建独立环境：
2026-11 的 FastAPI 0.165 / Starlette 1.10 已要求 Python ≥ 3.11，`pip` 在 3.9 上可能
回退到较旧版本，虽然 lab 的 API 代码兼容，但测试矩阵以 3.11/3.12 为准。

```bash
cd labs/0017-serving
python3 --version
python3 -m unittest test_service -v
```

应该看到：

```
Ran 51 tests in 0.18s

OK
```

> 如果系统 `python3` 不在 PATH，可用 `python3.12` / `python3.9`，或先建
> `python3.12 -m venv .venv`。lab 本身只要求 Python ≥ 3.9。

HTTP 层与 `test_api.py` 需要：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest test_api -v
```

本 lab 在 Python 3.11.2 + FastAPI 0.165.0 + Starlette 1.10.0 上跑通 64 个测试；
源码另用 `ast.parse(feature_version=(3, 9))` 检查过 3.9 语法兼容。

## 快速开始

### 1. 一次正常 run：202、SSE、结果、幂等重试

```bash
python3 service_cli.py demo
```

关键输出：

```
POST /v1/runs -> 202 accepted (run-0001)

GET /v1/runs/run-0001/events (SSE)
  #01 queued
  #02 started
  #03 phase_started  intake
  ...
  #12 effect         publish_report
  #13 phase_finished publish_report
  #14 done           succeeded

result: missing null guard in OrderMapper.toDto (confidence 0.86)

retry the same POST with Idempotency-Key: demo-1
  -> 200 duplicate, same run: True
```

注意两件事：

- `202` 返回的是 **run id**，不是最终答案；请求生命周期和 run 生命周期分离。
- 同一个 `Idempotency-Key` + 同一个 payload 重试，拿到的是原 run，不是第二个。

### 2. 入口控制：配额 429 与饱和 503

```bash
python3 service_cli.py load
```

关键输出：

```
A. per-tenant quota: 12 submissions, 3 per tenant, capacity 3
  accepted 202: 9
  rejected 429: 3
  max_inflight: 4
  completed: 9

B. saturation: workers=3, queue=4, 12 submissions, unlimited quota
  accepted 202: 7 (3 running + 4 queued)
  rejected 503: 5 (bounded queue full; quota token refunded)
  max_inflight: 3
  queue_depth: 4
```

两个数字对应两种保护：

- **429** 是租户问题：你的配额用完了，过 `Retry-After` 再来。
- **503** 是系统问题：所有租户共享的 worker 和队列满了，现在谁来都不接。

队列满时把刚才扣掉的配额 token 退回，因为客户端没有做错，是共享资源忙。

### 3. 重试：预算，而不是勇气

```bash
python3 service_cli.py retry
```

关键输出：

```
R02: the logs tool returns 503 on its first two attempts
  #06 retry          retrieve_logs retry=1 delay=22ms
  #07 retry          retrieve_logs retry=2 delay=45ms
  ...
  attempts: 7 · retries: 2 · retry tokens left: 7.00

Same run with a zero-capacity retry budget
  status: failed · code: retry_budget_exhausted

Layered retries multiply (3 attempts per layer):
  layers=1 -> 3 downstream calls
  layers=2 -> 9 downstream calls
  layers=3 -> 27 downstream calls
  layers=4 -> 81 downstream calls
  layers=5 -> 243 downstream calls
```

`R02` 的两条重试延迟分别是 22ms 和 45ms：base 50ms / 400ms cap，full jitter 后
每次落在 `[0, bound]`。两条延迟不同不是随机噪声，而是为了避免所有客户端在同一
时刻一起重试。5 层、每层 3 次尝试会把 1 次用户请求放大成 243 次下游调用。

### 4. SSE：Last-Event-ID、重放与 snapshot

```bash
python3 service_cli.py resume
```

关键输出：

```
client A reads four events, then disconnects:
  #01 queued
  #02 started
  #03 phase_started  intake
  #04 phase_finished intake
  disconnected after Last-Event-ID=4

client B reconnects with Last-Event-ID=4:
  #05 phase_started  retrieve_logs
  ...
  overlap: 0 events · final: done

Now shrink the ring buffer to 4 and ask from id=1:
  #10 snapshot
  #11 phase_started  publish_report
  ...
```

事件有**单调递增的 id**；客户端重连时带 `Last-Event-ID`。如果请求的 id 已经
被环形缓冲区淘汰，服务先发一个 `snapshot`（包含当前状态、最终结果、最后 id），
再从第一个仍保留的事件继续。结果始终在 store 里，不在流里。

### 5. 关停：checkpoint 与重启

```bash
python3 service_cli.py shutdown
```

关键输出：

```
before shutdown: 2 running, 4 queued
readiness: False
drain: timed_out=True · inflight=2 · checkpointed=2 · queued_for_restart=6

new process, same store:
  statuses: {'succeeded': 6}
  resumed: 6 · effects: 6 (each published exactly once)
  attempts are cumulative across restarts: [5, 5, 5, 5, 5, 5]
```

关停顺序是：

1. readiness 立刻变 false（负载均衡器停止导流）；
2. 不再接受新 run（503 `draining`）；
3. 在 grace period 内等在飞 run 完成；
4. 超时则取消 worker，并在最后一个完成的 phase 写 checkpoint；
5. 新进程从 store 里扫描 `queued` run，从 `phase_index` 继续。

效果账本（effect ledger）保证“comment 已发、checkpoint 还没写”的崩溃窗口不会
重复发评论。测试
`test_effect_is_not_replayed_after_crash_between_effect_and_checkpoint`
专门构造了这个窗口。

## 跑一个真的 HTTP 服务

```bash
. .venv/bin/activate
uvicorn api:app --host 127.0.0.1 --port 8000
```

创建 run：

```bash
curl -i -X POST http://127.0.0.1:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -H 'X-Tenant: acme' \
  -d '{"service":"checkout-api","incident":"R01"}'
```

流式读取（SSE）：

```bash
curl -N http://127.0.0.1:8000/v1/runs/run-0001/events
```

断线后从第 5 个事件恢复：

```bash
curl -N -H 'Last-Event-ID: 5' \
  http://127.0.0.1:8000/v1/runs/run-0001/events
```

查询最终结果：

```bash
curl http://127.0.0.1:8000/v1/runs/run-0001
```

取消：

```bash
curl -X POST http://127.0.0.1:8000/v1/runs/run-0001/cancel
```

就绪探针：

```bash
curl -i http://127.0.0.1:8000/readyz
```

> `X-Tenant` 只是 lab 的占位。生产里 tenant 必须来自认证主体（JWT / mTLS /
> API key 映射），不能由客户端随便声明，否则租户隔离就是摆设。

## 代码导读

### 入口顺序

`RunService.submit()` 的顺序是刻意设计：

```
校验 Idempotency-Key
  -> 幂等记录（重试不消耗配额）
  -> 租户令牌桶（429）
  -> 有界队列（503）
  -> 创建 run + 事件 + idempotency 记录
```

如果先扣配额再查幂等，客户端的正常重试会把自己限流掉；如果先入队再查队列
容量，拒绝的请求会留下孤儿 run。

### 状态机

```
queued ──worker──> running ──成功──> succeeded
   │                  │
   │                  ├──超时/不可重试──> failed
   │                  ├──用户取消──────> cancelled
   │                  └──进程关停──────> queued（resumable，等下一进程）
   └──用户取消────────────────────────> cancelled
```

`done` 事件是 terminal 的最后一个事件。结果先写 `run.result`，再发 `done`；
客户端收到 `done` 后仍应 `GET` 一次，确认服务端结果是完整的。

### 事件日志

`run.events` 是一个有界环形缓冲区（默认 64）。每个事件有 `seq`，从 1 开始。
`subscribe()` 先注册订阅者，再复制 replay 快照；两者之间产生的事件会重复投递
一次，由 `seq <= last_yielded` 去重。

慢消费者不会拖垮服务：每个订阅者有一个 `subscriber_queue`（默认 16）。队列满
时丢弃最旧事件、投递 `overflow` 事件、断开订阅；客户端用最后真正收到的 id
重连。SSE 没有应用层流控，这是唯一安全的做法。

### 重试与超时

- `RetryableError`：timeout、429/503、连接重置。
- `NonRetryableError`：400/401/403/404/422、策略拒绝、校验失败。
- `RetryBudgetExhausted`：预算用完，立即失败，不再放大负载。
- `Deadline` 是**绝对截止时间**，不是每次调用重新计时；每层只拿到
  `deadline.remaining()`。
- 取消是协作式的：Agent 在 phase 边界检查 `should_stop()`；任务只在 `await`
  点能被取消，所以每个 phase 开头都有一个 `clock.sleep()` 作为 yield point。

### 幂等

- `(tenant, Idempotency-Key)` 唯一；payload 的 SHA-256 是 fingerprint。
- 同 key 同 payload：run 已完成 → 200 + 原 run；run 进行中 → 409 + Location。
- 同 key 不同 payload → 422，不返回原结果。
- key 有 TTL（默认 24h）。TTL 必须覆盖客户端可能的最长重试窗口。
- 这遵循 IETF `Idempotency-Key` draft 的语义；它仍是 Active Internet-Draft，
  不是 RFC。

## 改一改，看回归

1. **把 `per_tenant_capacity` 改成 1，refill 改成 0.1**：`load` 里 429 的数量
   会变；观察 `Retry-After` 是否仍然合理。
2. **把 `workers` 改成 1，`max_queue` 改成 1**：饱和场景的 503 会更早出现；
   `max_inflight` 应该始终是 1。
3. **把 `event_retention` 改成 2**：`resume` 里 snapshot 的 id 会提前；确认
   `snapshot.data.result` 仍然包含完整结果。
4. **把 `subscriber_queue` 改成 1**：慢消费者会更早 overflow；确认 overflow
   事件带的是最后送达的 id，而不是 run 的 last id。
5. **把 `retry_budget_capacity` 改成 0**：R02 从 succeeded 变成 failed，错误码
   是 `retry_budget_exhausted`，而不是继续重试。
6. **把 `drain_timeout_s` 改成 0**：`shutdown` 里 `timed_out=True`，两个在飞
   run 被 checkpoint；重启后 6 个 run 全部成功，effects 仍然是 6。
7. **加一个 `R07` 场景，让 `retrieve_code` 永久失败**：它应该走
   `non_retryable`，attempts 在失败 phase 停止，不产生 retry 事件。

## 成功标准

- 能解释为什么 `POST /v1/runs` 返回 202 + run id，而不是直接返回答案。
- 能画出一次 run 的状态机，并指出三个 terminal 状态。
- 能说出 429 和 503 的区别，以及为什么 queue full 时要退还配额 token。
- 能写出 full jitter 的公式，并解释为什么 5 层 × 3 次尝试是 243 而不是 15。
- 能解释 SSE 的 `Last-Event-ID`、snapshot 和 overflow 各自解决什么问题。
- 能解释为什么客户端断开不应取消 run，以及为什么 result store 才是真相。
- 能按顺序描述 graceful shutdown，并说明 checkpoint 与 effect ledger 如何
  避免重复副作用。
- 能指出哪些状态在多 worker 部署时必须外置（run store、事件日志、幂等表、
  effect ledger、订阅广播）。

## 这个 lab 模拟了什么

| 真实系统 | lab 里的替身 |
|---|---|
| Postgres / Redis | `Store`（同一进程内，跨 service 实例复用） |
| Kafka / Redis Streams | `run.events` 环形缓冲区 + `subscribe()` |
| Celery / Arq / 自建 worker | `asyncio.Queue` + N 个 `_worker()` |
| 模型与工具调用 | 确定性 `RepoMedicAgent` + 注入失败 |
| Prometheus / OTel | `service.metrics` + `snapshot_metrics()` |
| Kubernetes SIGTERM + preStop | `begin_shutdown()` + `drain(timeout)` |
| API gateway 限流 | 租户 `TokenBucket` + 有界队列 |

真实部署时，最重要的变化不是换库，而是把**所有跨请求状态**搬出进程：
run store、idempotency、event log、effect ledger、订阅广播。单个进程内的
`asyncio.Queue` 只适用于单 worker；`--workers 4` 会得到四个互不共享的队列。
