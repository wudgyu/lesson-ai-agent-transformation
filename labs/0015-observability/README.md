# 第 15 课 Lab：Tracing、Token 账本与延迟预算

这个 lab 不调用模型，也不依赖第三方包。它把“可观测性”拆成三个可以互相验证的问题：

1. **数据可信吗**：trace 树是否完整？usage 是否缺失？agent 汇总与子调用是否一致？
2. **钱花在哪**：是 unique token 还是重复计费？失败调用算不算？cache 省了多少？
3. **慢在哪**：wall、active、critical path 分别是多少？哪一段该设预算？

核心主张：

> 可观测性不是“多打日志”，而是把一次 run 拆成一棵可归属的树。
> 没有 trace，你只能看到“这次花了 $0.02”；有 trace，你能说出这 $0.02
> 里哪 62% 是重复输入、哪一次失败调用被计费、哪 88 秒在等人审批。

## 文件

| 文件 | 作用 |
|---|---|
| `traces.jsonl` | 10 条 RepoMedic trace、74 个 span；2 个版本 × 5 条 run |
| `pricing.json` | 两个 model 的示意价格；按 `effective_from` 版本化 |
| `budgets.json` | active / tool / approval / TTFT 与 cost / cache / retry 预算 |
| `trace_schema.py` | `Trace` / `Span` / `Usage` 契约；OTel GenAI 属性名；树与汇总校验 |
| `pricing.py` | 价格表、cache 读写、成本与 cache savings 计算 |
| `ledger.py` | 只从叶子 inference span 计费；失败/重试成本；cost per success；context growth |
| `latency.py` | wall / blocked / active；critical path DAG；stage union；percentile；尾延迟 |
| `dashboard.py` | 完整性与成本的发布视图：PASS / WARN / BLOCK |
| `obs_cli.py` | `audit / trace / cost / latency / dashboard / demo` |
| `test_observability.py` | 75 个离线测试，不需要 API key、网络或 sleep |
| `make_fixtures.py` | 确定性 fixture 生成器；记录数据是怎么造出来的 |

## 环境

Python ≥ 3.9，仅标准库。本 lab 在 Python 3.9.25 与 3.12.16 上都跑通 75 个测试。

```bash
cd labs/0015-observability
python3 --version
python3 -m unittest test_observability -v
```

应该看到：

```
Ran 75 tests in 0.04s

OK
```

## 快速开始

### 1. 审计数据

```bash
python3 obs_cli.py audit
```

输出：

```
traces: 10 · spans: 74
arms: v05-hotfix 5 · v06-candidate 5
success: 8/10 (80.0%)
usage: complete 9/10 · missing 1 · aggregate mismatch 1
  missing usage: R08:R08-l2
  mismatch: R09 (input +500, output +0)

audit: FAILED
```

`audit: FAILED` 是**预期结果**，不是你的环境坏了。数据里故意埋了两个 instrumentation 问题：

- `R08-l2` 没有 usage：成本只能算下界，不能估算。
- `R09` 的 agent 汇总比子 inference 之和高 500 input token：账本口径不一致。

审计的职责就是让这两个问题在发布前暴露，而不是在月底账单上才发现。

### 2. 看一条 trace

```bash
python3 obs_cli.py trace --id R04
```

输出：

```
trace R04 · case C04 · arm v05-hotfix · success
wall 95.0s · blocked 88.0s · active 7.0s · overhead 0ms
critical path 95.0s (active 7.0s)

span                   kind       start     end       dur      excl
R04-root               agent      0ms       95.0s     95.0s    0ms
  R04-l1               llm        0ms       3.0s      3.0s     3.0s
  R04-t1               tool       3.0s      4.0s      1.0s     1.0s
  R04-a1               approval   4.0s      92.0s     88.0s    88.0s
  R04-l2               llm        92.0s     95.0s     3.0s     3.0s

billed usage: input 2600 (cache_read 1500, 57.7%) · output 300 · reasoning 0
cost: $0.006125 · cache savings $0.003375 · failed+retry $0.000000

stage union: llm 6.0s · tool 1.0s · approval 88.0s · retrieval 0ms · guardrail 0ms
critical path: R04-root → R04-l1 → R04-t1 → R04-a1 → R04-l2
```

要能解释三件事：

1. 为什么 `wall 95.0s`，但 `active` 只有 `7.0s`？因为 88 秒在等人审批。
2. 为什么 `critical path` 是 95 秒，而 `active critical path` 是 7 秒？审批在关键路径上，但不是模型/工具的活跃时间。
3. 为什么 usage 只有 2,600 input，而不是 agent + 子调用相加的 5,200？因为 agent span 的 usage 是汇总，不能再计一次。

### 3. 看成本

```bash
python3 obs_cli.py cost
```

输出（截取）：

```
run   case   arm            ok   cost        cache     retry       top driver
R06   C06    v06-candidate  yes  $0.078000   0.0%      $0.000000   medic-large $0.078000
R09   C01    v06-candidate  yes  $0.031775   43.0%     $0.000000   medic-large $0.031775
R01   C01    v05-hotfix     yes  $0.019300   62.2%     $0.000000   medic-large $0.019300
R03   C03    v05-hotfix     NO   $0.015900   0.0%      $0.009100   medic-large $0.015900
...
total $0.167829 · successes 8 · cost/success $0.020979 · cache hit 38.9% · retry share 5.4%
```

重点：

- `R06` 一条 run 占总成本 46.5%：8 轮长上下文，input 从 1,200 涨到 5,400，累计 26,400。
- `R03` 是失败 run，但 $0.0159 仍进分子；其中 $0.0091 来自失败调用与重试。
- `R08` 带 `*`：缺少 usage，成本是下界。
- 成本按 **成功任务** 算：$0.1678 / 8 = $0.0210；不是按 run 算。

### 4. 看延迟

```bash
python3 obs_cli.py latency
```

输出（截取）：

```
run   wall      blocked   active    critical    top stage
R01   14.0s     0ms       14.0s     14.0s       llm 10.9s
R04   95.0s     88.0s     7.0s      95.0s       approval 88.0s
R05   12.0s     0ms       12.0s     12.0s       tool 7.0s
R06   20.6s     0ms       20.6s     20.6s       llm 17.6s
R10   12.0s     0ms       12.0s     12.0s       tool 9.0s

active p50/p95/p99: 12.0s / 22.0s / 22.0s
tool p95 9.0s · approval p95 88.0s · ttft p95 900ms

budget violations
  global: active_p95_ms 22000.0 > 15000.0
  global: tool_p95_ms 9000.0 > 8000.0
  R03: active_ms 22000.0 > 20000.0
  R06: active_ms 20600.0 > 20000.0
```

要能解释：

- `R05` 两个工具各 7 秒并行，**不能相加成 14 秒**；stage union 是 7 秒，critical path 仍是 12 秒。
- `R10` 五个并行工具里只有一个 9 秒，其他 0.5 秒；平均值很好看，但 p95 被它拖到 9 秒。
- `R04` 的 approval p95 是 88 秒。审批等待要单独报，不要混进模型延迟。

### 5. 看面板

```bash
python3 obs_cli.py dashboard
```

输出：

```
integrity
  [BLOCK] missing_usage      1 inference span(s) without usage: R08:R08-l2
  [BLOCK] reconciliation     agent aggregate != sum(inference spans): R09 (input +500, output +0)
  [PASS] unknown_model      every billed model has a price
latency
  [BLOCK] latency_budget     global: active_p95_ms 22000.0 > 15000.0
  [BLOCK] latency_budget     global: tool_p95_ms 9000.0 > 8000.0
  [BLOCK] latency_budget     R03: active_ms 22000.0 > 20000.0
  [BLOCK] latency_budget     R06: active_ms 20600.0 > 20000.0
cost
  [WARN] cost_per_success   $0.0210 per success > $0.0200 budget
  [PASS] cache_hit_rate     38.9% of input tokens served from cache
  [WARN] retry_share        5.4% of cost is failure/retry > 5.0%

active p95 22.0s · cost/success $0.020979 · cache 38.9% · retry share 5.4%
verdict: BLOCK
```

门禁规则：

| 检查 | 级别 | 为什么 |
|---|---|---|
| usage 缺失 | BLOCK | 成本不可解释；估算会掩盖 instrumentation bug |
| agent 汇总不一致 | BLOCK | 账本口径冲突；先修数据，再谈优化 |
| 未知 model 价格 | BLOCK | 无法计价，不能发布成本数字 |
| active p95 / 单 run active 超预算 | BLOCK | 延迟 SLO 是发布契约 |
| cost per success 超预算 | WARN | 成本是重要信号，但不单独阻断 |
| cache hit rate 低 | WARN | 优化项；先看负载是否可缓存 |
| retry share 高 | WARN | 可靠性/成本信号；查失败原因 |

`verdict: BLOCK` 时进程 exit 1，可以直接接 CI：

```bash
python3 obs_cli.py dashboard || echo "release blocked"
```

### 6. 跑完整 demo

```bash
python3 obs_cli.py demo
```

依次跑 audit、trace R04、cost、latency、dashboard。最后 exit 1 是预期的。

## 7 个可以动手改的点

1. 给 `R08-l2` 补上合理的 usage，跑 `audit` 与 `dashboard`，看哪些告警消失、哪些还在。补数之后成本从“下界”变成“可解释”，但不要忘记在真实系统里应该修 instrumentation，而不是在分析层补数。
2. 把 `R09` 的 `root_input_delta` 改成 0，重新生成 fixtures，观察 reconciliation 从 BLOCK 变 PASS。
3. 在 `pricing.json` 里给 `medic-large` 加一条 `effective_from` 更晚的涨价记录，用 `2026-11-18` 与 `2026-12-01` 两个日期分别算 R01，验证历史 trace 不会被新价格回填。
4. 给 `R06` 加 cache_read（例如每轮命中 80% 前缀），重算成本，观察 cache hit rate 与 cost per success 的变化。
5. 把 `R05` 的两个并行工具改成部分重叠（一个 2.5–9.5s，一个 4–8s），观察 stage union 与 critical path 的差别。
6. 把 `budgets.json` 的 `active_p95_ms` 从 15,000 放宽到 25,000，dashboard 的 latency 告警会减少；问自己：这是“达标了”，还是“把门禁调松了”？
7. 给 `latency.py` 加一个 `p99` 的单 run 预算，并补一个“合法慢路径不误伤”的测试。

## 成功标准

- [ ] 75 个测试全绿，能解释 schema / pricing / ledger / latency / dashboard 各抓什么。
- [ ] 能说出为什么 agent span 的 usage 不能与子 LLM span 相加。
- [ ] 能解释 `R08` 的成本为什么是下界，以及为什么不能估算。
- [ ] 能解释 `R03` 的失败调用与重试如何进入成本。
- [ ] 能解释 `R04` 的 wall / blocked / active / critical path 四个数。
- [ ] 能解释 `R05` 的 stage union 为什么不是两个工具时长之和。
- [ ] 能解释 `R10` 的 p95 为什么被一个慢工具拖高。
- [ ] 能说出 dashboard 每条 BLOCK / WARN 的依据。
- [ ] 能把这个 lab 的 trace / 账本 / 预算映射到 Java 的链路追踪、账单与 SLO。

## 设计说明：数据是故意“不干净”的

真实系统的 trace 很少是完美的。这个 lab 故意保留：

- 一条 trace 缺 usage（`R08`）；
- 一条 trace 的 agent 汇总与子调用不一致（`R09`）；
- 一条失败 trace（`R03`）；
- 一条审批等待 88 秒的 trace（`R04`）；
- 一条长上下文、成本占近一半的 trace（`R06`）。

如果所有数据都干净，`dashboard` 就只是一个“全绿”的演示，学不到 fail closed。
先把脏数据识别出来，再谈优化，这才是 observability 的顺序。
