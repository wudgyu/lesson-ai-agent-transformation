# 第 5 课 Lab：状态与记忆

这一课把“上下文窗口”拆成三个不同的东西：

- **context**：这一次模型调用看到的 token；
- **state**：这次任务/会话里已知的事实、决策、未解决问题、副作用记录；
- **memory**：跨会话保留的偏好、事故经验、流程规则。

Lab 用一个确定性的 RepoMedic 会话 Agent 演示：压缩（compaction）、长期记忆、冲突覆盖、
checkpoint/resume，以及“不要重复执行写操作”。

## 文件

| 文件 | 作用 |
|---|---|
| `state.py` | `AgentState`、`Fact`、消息、事实冲突、幂等副作用、checkpoint |
| `compaction.py` | token 估算、阈值判断、结构化摘要、naive truncation 对照 |
| `memory.py` | 长期记忆：semantic / episodic / procedural、去重、TTL、检索 |
| `session.py` | 脚本化 RepoMedic 会话：health → logs → runbook → answer，支持 resume |
| `eval.py` | 4 个场景 × 不同记忆策略，输出对比报告 |
| `test_state_memory.py` | 23 个离线测试，不需要 API key |
| `reports/` | 评测报告（已加入 .gitignore） |

## 快速开始

```bash
cd labs/0005-state-memory
python3 -m unittest -v
python3 eval.py
```

你会看到类似：

```
scenario         mode          completed task   prompt  peak   comp  drop  mem   stale  resume  dup
long_incident    none          yes       yes    no      842    0     8     -     -      -       -
long_incident    compaction    yes       yes    yes     673    8     0     -     -      -       -
preference       no_longterm   yes       yes    -       392    -     -     no    -      -       -
preference       longterm      yes       yes    -       421    -     -     yes   -      -       -
stale_memory     no_longterm   yes       yes    -       -      -     -     no    no     -       -
stale_memory     longterm      yes       yes    -       -      -     -     yes   yes    -       -
resume           checkpoint   yes       yes    -       -      -     -     -     -      yes     0
```

几件事值得注意：

- `none`（不压缩、不长期记忆）也能答对，但把最初的用户约束丢掉了（`prompt=no`），并丢了 8 条消息。
- `compaction` 保留了任务提示与结构化事实，峰值从 842 降到 673；但它压缩了 8 次——**压缩本身也有成本**。
- `preference` 里长期记忆把第一个工具从 `get_service_health` 变成 `search_runbooks`，因为记住了“先看 runbook”。
- `stale_memory` 里旧记忆 `db_pool_max=40` 被新工具结果 `50` 覆盖（`stale=yes`）。
- `resume` 在 checkpoint 恢复后没有重复执行重启（`dup=0`）。

## 场景与指标

| 场景 | 验证什么 |
|---|---|
| `long_incident` | 18 轮排障；压缩是否保留事实、任务提示、上下文预算 |
| `preference` | 跨会话记忆：会话 1 写入偏好，会话 2 改变工具顺序 |
| `stale_memory` | 旧记忆与新工具证据冲突时，是否以新证据为准并标记 superseded |
| `resume` | checkpoint 恢复后事实是否还在，写操作是否幂等 |

| 指标 | 含义 |
|---|---|
| `completed` | 没有触发上下文溢出 |
| `task` | 最终答案包含期望事实（N+1、48/50、v2.14.1） |
| `prompt` | 最初的用户任务/约束是否还在 state 里 |
| `peak` / `final` | 上下文 token 峰值 / 结束时 |
| `comp` | 压缩次数；太高说明阈值或 `keep_recent` 需要调 |
| `drop` | naive truncation 丢掉的消息数 |
| `mem` | 是否命中长期记忆 |
| `stale` | 旧记忆是否被新证据 supersede |
| `resume` / `dup` | checkpoint 是否恢复成功 / 是否重复执行写操作 |

## 实验 1：压缩 vs 不压缩

```bash
python3 eval.py --scenario long
```

`none` 模式用 `truncate_oldest` 硬丢旧消息，能省 token，但会丢掉最初的用户约束。
`compaction` 把事实、决策、未解决问题、最近工具证据写成结构化摘要，保住任务意图。
**压缩不是免费午餐**：它需要一次 LLM 调用（生产里），也可能丢细节。

## 实验 2：调压缩阈值，别让它抖动

默认 `threshold=0.70`、`keep_recent=4`，在 18 轮里压缩了 8 次。把阈值调高、只保留最近 2 轮：

```bash
python3 eval.py --scenario long --threshold 0.9 --keep-recent 2
```

你会看到压缩次数降到 2，峰值升到约 741，但最终 token 更低。回答：
如果延迟预算很紧，你会选哪个配置？如果任务很容易丢细节呢？

## 实验 3：长期记忆的写入与读取

`preference` 场景里，会话 1 说“记住：以后排查先看 runbook，再看日志”。
会话 2 的第一句是“按我的偏好来”，记忆检索把偏好注入上下文，第一个工具因此改变。

尝试把 `memory.py` 的 `remember` 改成只在用户明确说“记住”时写入，
观察“隐性偏好”是否还能被记住。再尝试给记忆加 `ttl_days=7`，观察过期后的行为。

## 实验 4：旧记忆 vs 新证据

`stale_memory` 场景预置了 `db_pool_max=40` 的旧记忆。
新工具返回 50 时，`AgentState.set_fact` 会把旧值标记为 `superseded`，最终答案使用 50。

如果反过来让记忆覆盖工具结果，会发生什么？为什么在排障场景里这是危险的？

## 实验 5：崩溃恢复与幂等

`resume` 场景在 2 轮后“崩溃”，从 checkpoint 恢复，再执行两次重启：
第一次 `status=restarted`，第二次 `status=noop`，`side_effects` 只有一条。

把 `restart_service` 的幂等键从 `service + approval_id` 改成随机值，
观察 `dup` 指标如何变化。这就是第 2 课讲的工具幂等，在状态层的体现。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `ContextOverflow` | 没有压缩，且 truncation 后仍超预算 | 开启 compaction，或提高 `max_context_tokens` |
| `comp` 很高 | 阈值太低 / `keep_recent` 太大 | 调高 `threshold`，减小 `keep_recent` |
| 记忆检索不到 | query 与记忆文本/tag 没有词面重合 | 加 tags，或换成向量检索 |
| 旧记忆覆盖新事实 | 写入顺序错误 / 没有 supersede | 用 `set_fact`，并记录来源与置信度 |
| resume 后重复写操作 | 没有幂等键，或没有持久化 side effects | 用 `approval_id` 做 key，checkpoint 持久化 |

## 下一课

第 6 课进入 LangGraph：把这一课手写的状态、reducer、checkpoint 和条件边，
用 StateGraph 重新表达。你会看到框架替你做了什么，以及它没有替你做什么。
