# 第 12 课 Lab：把审批做成决策系统

这个 lab 不调用模型，也不依赖 LangGraph。它只做一件事：给 RepoMedic 的写操作加一层
**策略 + 审批 + 幂等执行**，并把 HITL 里最容易出错的边界全部写成可测试的代码：

```
policy.py     风险分级：同样的工具，参数不同，审批级别不同
gateway.py    审批生命周期：请求 → 人工决定 → 重新校验 → 执行 → 消费
store.py      JSON 持久化：请求、审计、外部副作用台账
models.py     Action / digest / ApprovalRequest / 结果类型
hitl_cli.py   命令行：policy / submit / inbox / approve / execute / metrics / demo
test_hitl.py  27 个离线测试，不需要 API key、网络或 sleep
```

**本 lab 的核心主张：审批不是按钮，而是一套决策系统。**
一个“Approve”按钮至少隐含了四个问题：谁来批、批的是什么、批到什么时候、批完怎么保证只执行一次。
这四个问题都能用代码回答，也都能用测试证明。

## 环境要求：Python ≥ 3.9，仅标准库

```bash
cd labs/0012-hitl-policy
python3 --version    # 3.9+ 即可
python3 -m unittest test_hitl -v
```

本 lab 在 Python 3.9.25 与 3.12.16 上验证，不安装任何第三方包。

## 先跑一遍完整剧情

```bash
python3 hitl_cli.py demo
```

它会用固定的 `FakeClock` 走十个场景，输出可复现：

1. **T0 auto**：`query_logs` 直接执行，不打扰人。
2. **T1 notify**：dev 环境重启直接执行，但发通知。
3. **T2 approval**：生产 `order-service` 重启被挂起；审批前执行被拦截；`bob` 批准后执行；重复执行返回 `duplicate`。
4. **T3 four-eyes**：`payment-service` 需要两位 senior；同一个人重复批准被拒；`carol` 不是 senior 被拒；第二个人批准后才执行。
5. **职责分离**：`carol` 不能批准自己发起的请求。
6. **动作绑定**：批准的是 `order-service`，拿 `payment-service` 的参数去执行会被 digest 校验拦截。
7. **过期**：超过 TTL 没人处理，执行被拒，状态变 `expired`。
8. **T4 deny**：`modify_firewall` 永远不经过这个 gateway。
9. **策略变更**：批准后策略收紧为 deny，旧审批不能继续执行。
10. **指标**：审批率、决策延迟、橡皮图章计数、待处理时长、效果数、审计事件数。

输出里重点看三处：

```
message: "request APR-0001 is pending (0/1 approvals)"       ← 审批前不能执行
message: "effect INC-44:... applied"                          ← 批准后只执行一次
message: "approval APR-0001 was already used; no second effect" ← 重放不产生第二次副作用
```

## 手动走一遍

```bash
# 看规则表
python3 hitl_cli.py policy

# 发起一个生产重启
python3 hitl_cli.py submit \
  --tool restart_service \
  --args '{"service":"order-service","env":"prod"}' \
  --requested-by agent/repomedic --task INC-42

python3 hitl_cli.py inbox
python3 hitl_cli.py approve --id APR-0001 --approver bob --note "pool metrics checked"
python3 hitl_cli.py execute --id APR-0001
python3 hitl_cli.py execute --id APR-0001    # 第二次：duplicate
python3 hitl_cli.py metrics
python3 hitl_cli.py audit --limit 20
```

`APR-0001` 是本次 state 文件的第一个请求；如果你已经跑过 demo，ID 会不同。用 `inbox` 看当前待办。

### 试几个“坏”操作

```bash
# 1. 自己批自己
python3 hitl_cli.py submit --tool restart_service \
  --args '{"service":"inventory-service","env":"prod"}' \
  --requested-by carol --task INC-43
python3 hitl_cli.py approve --id APR-0002 --approver carol

# 2. 越权批 four-eyes
python3 hitl_cli.py submit --tool restart_service \
  --args '{"service":"payment-service","env":"prod"}' --task INC-44
python3 hitl_cli.py approve --id APR-0003 --approver carol

# 3. 批准后换参数
python3 hitl_cli.py approve --id APR-0003 --approver alice
python3 hitl_cli.py execute --id APR-0003 \
  --args '{"service":"payment-service","env":"dev"}'
```

每一条都会给出具体的拒绝原因，而不是笼统的 `forbidden`。这也是 ACI（第 4 课）的延续：
**错误信息是给调用方看的 prompt。**

## 风险分级

| Tier | 规则例子 | 人工参与 | 本 lab 的行为 |
|---|---|---|---|
| T0 auto | `query_logs`、`check_health`、`search_runbook` | 无 | 直接执行并审计 |
| T1 notify | dev/staging 重启、小规模扩缩容 | 通知，不阻塞 | 执行 + `notification.sent` |
| T2 approval | 生产非支付服务重启、大规模扩缩容 | 1 位 on-call | 挂起请求，批准后执行 |
| T3 four-eyes | `payment-service` 重启、回滚、删卷、缩到 0 | 2 位 senior，且不能是发起人 | 两位都批准后才执行 |
| T4 deny | `modify_firewall`、`iam_attach_policy`、未知工具 | 不通过本 gateway | 直接拒绝，默认关闭 |

策略看的是**参数**，不只是工具名：`restart_service` 在 dev 是 T1，在 order-service 是 T2，
在 payment-service 是 T3。这是 HITL 设计里最容易被忽略的一点：
**把工具名当风险等级，等于把参数绕过留给攻击者。**

## 审批请求里有什么

`ApprovalRequest` 绑定的是：

- **action digest**：`sha256(tool + canonical args + requested_by + task_id)`；
- **tier / rule / reason**：为什么需要人；
- **required_approvals**：1 或 2；
- **created_at / expires_at**：审批有时效；
- **approvals / rejections**：谁在什么时候做了什么决定；
- **status**：`pending → approved → consumed`，或 `rejected` / `expired`。

`digest` 不是安全签名，它只是**动作指纹**：确保“人批准的动作”和“实际执行的动作”是同一个。
真正的身份认证与签名在 gateway 之外（OIDC / mTLS / 审批系统），本 lab 用
`ApproverDirectory` 模拟“谁有权批哪一级”。

## 为什么重放不会执行两次

执行顺序是：

```
effect ledger 写入（幂等键）
        ↓
approval.status = consumed
        ↓
审计
```

如果进程在“写入 effect”之后、“标记 consumed”之前崩溃：

1. effect 已经在台账里；
2. 重试时 `_run()` 用同一个业务幂等键查到记录，返回 `duplicate`；
3. 然后才把 approval 标记为 `consumed`。

所以这个设计是 **at-least-once + 幂等去重**，不是“恰好一次”。真正的恰好一次需要目标系统
自己支持幂等键或事务，gateway 不能替它保证。

只读动作（T0）**不进入 effect ledger**：重复读是安全的，而且下次读可能需要最新数据。
只有写动作才生成幂等键；这是 `_run(use_ledger=...)` 的用途。

反过来，如果先标 `consumed` 再执行，崩溃就会**丢动作**。对重启这类操作，丢动作比重复动作更危险；
对支付类操作，则需要目标系统的对账/撤销机制。这个取舍必须按动作语义决定，不能一刀切。

## 测试覆盖（27 个）

```bash
python3 -m unittest test_hitl -v
```

重点看这些名字：

- `test_unknown_tool_fails_closed`：未知工具默认拒绝。
- `test_scale_thresholds_use_arguments`：同一个工具按参数分到 T1/T2/T3。
- `test_requester_cannot_approve_own_request`：职责分离。
- `test_four_eyes_requires_two_distinct_senior_approvers`：four-eyes 的双人约束。
- `test_action_substitution_is_blocked_by_digest`：批准 order，不能执行 payment。
- `test_policy_escalation_blocks_old_approval`：批准后策略收紧，旧审批失效。
- `test_crash_after_effect_retry_does_not_double_execute`：崩溃重试不重复副作用。
- `test_two_approvals_for_same_business_action_do_not_double_execute`：两次审批不等于两次效果。
- `test_metrics_flag_rubber_stamping`：审批太快会被指标抓到。

## 哪些是真的，哪些是模拟的

**真的：** 策略评估、digest 绑定、审批状态机、职责分离、four-eyes、TTL 过期、默认拒绝、
执行前重新校验、幂等台账、崩溃重试、JSON 原子写、审计事件、指标计算。

**模拟的：** `restart_service` 不会真的重启进程；`store.py` 是单进程 JSON 文件，不是事务数据库；
`ApproverDirectory` 是内存里的名字集合，不是 OIDC/IAM；通知只是审计事件，没有真的发 Slack/邮件。

## 接到生产系统时要换什么

1. **存储**：把 JSON 换成事务表；`request_id` 主键，`idempotency_key` 唯一约束；
   审批更新用 `UPDATE ... WHERE status='pending' AND version=?` 做乐观锁，避免并发批准丢更新。
2. **身份**：审批人来自认证后的 session/token，不是命令行参数；授权查询 IAM/on-call 系统。
3. **通知与等待**：审批请求进工单/Slack/邮件；等待不占线程（Temporal `wait_condition`、LangGraph `interrupt`）。
4. **时间**：统一用服务端 UTC 时间；TTL 与 SLA 由业务定，不写死在代码里。
5. **策略**：策略要有版本号、变更审计、灰度发布；执行时重新评估，不能只信请求时的结果。
6. **对账**：对不可逆动作增加目标系统幂等键、执行前后状态校验、人工复核抽样。
7. **可观测**：每次请求、决定、执行都产生 trace/span；审批延迟、通过率、橡皮图章率进仪表盘。

## 和前后课程的关系

- **第 9 课**给了你 `interrupt()` / checkpoint / resume 的机制；本 lab 补的是“暂停之后，人到底在决定什么”。
- **第 11 课**给了你 MCP 的 host 侧 allowlist / 审批；本 lab 把“审批”从一行 `approval == "approved"` 展开成完整状态机。
- **第 12 课**的 lesson 会把这些决策写成可复用的清单；lab 是清单的可执行版本。

## Java 后端对照

| 本 lab | Java 后端 |
|---|---|
| `Policy.evaluate` | 风控规则引擎 / 审批中间件 |
| `ApprovalRequest` | 审批单 / 工单（maker-checker） |
| `digest` | 请求指纹、幂等键、防篡改校验 |
| `FOUR_EYES` | 双人复核 / maker-checker |
| `expires_at` + 默认拒绝 | 审批超时自动拒绝 |
| `ApproverDirectory` | IAM / 角色 / on-call 组 |
| `Store` | 审批历史表 + 唯一约束 |
| `EffectLedger` | 幂等表 / outbox |
| `audit` | 审计日志、操作留痕 |
| `metrics` | 审批时效、驳回率、橡皮图章率 |
