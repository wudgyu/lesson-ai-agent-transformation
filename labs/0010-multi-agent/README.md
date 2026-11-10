# 第 10 课 Lab：同一张任务，三种拓扑

这个 lab 把 RepoMedic 的“日志 / 代码 / runbook”三个证据域做成确定性 fixture，
用同一组场景跑三种拓扑：

```
single     一个 Agent + 三个工具；所有观察进同一个上下文
supervisor lead 保持控制；worker 独立上下文，只回摘要 + 证据
handoff    triage 把控制权依次交给 logs → code → runbook，共享会话历史
```

模型决策是脚本化的，不需要 API key。所有测试离线运行。
目的不是模拟模型有多聪明，而是让**控制权、上下文边界、预算与失败模式**成为唯一变量。

## 环境要求：Python ≥ 3.10

LangGraph 1.2.x 要求 Python ≥ 3.10。先检查：

```bash
python3 --version
```

低于 3.10 时，用 3.12 建独立环境：

```bash
# 方案 A：系统已有 python3.12
python3.12 -m venv .venv

# 方案 B：用 uv（推荐）
uv venv --python 3.12 .venv

. .venv/bin/activate
pip install -r requirements.txt
```

## 文件

| 文件 | 作用 |
|---|---|
| `multi_agent.py` | fixtures、worker 子图、supervisor 图、handoff 图、单 Agent 图、CLI |
| `test_multi_agent.py` | 15 个离线测试：上下文隔离、降级、预算、覆盖/成本权衡、handoff |
| `requirements.txt` | `langgraph>=1.2,<2` |

## 快速开始

### Step 1 · 跑测试

```bash
python3 -m unittest test_multi_agent -v
```

15 个测试应该全绿。重点看四个名字：

- `test_worker_context_has_no_parent_state`
- `test_worker_retries_with_fallback_tool`
- `test_flat_agent_drops_oversized_observation`
- `test_breadth_tradeoff_costs_more_but_covers_more`

### Step 2 · 三种拓扑的对照表

```bash
python3 multi_agent.py eval
```

输出（本机 Python 3.12.16 + LangGraph 1.2.19）：

```
scenario   pattern     steps  tokens  coverage  stop      handoffs  dropped
---------------------------------------------------------------------------
simple     single      3      835     1.00      complete  0         -
simple     supervisor  3      1304    1.00      complete  0         -
simple     handoff     3      822     1.00      complete  1         -
breadth    single      7      6943    0.67      partial   0         logs
breadth    supervisor  7      7876    1.00      complete  0         -
breadth    handoff     5      6747    1.00      complete  3         -
retry      single      5      1403    1.00      complete  0         -
retry      supervisor  5      2336    1.00      complete  0         -
retry      handoff     4      1518    1.00      complete  2         -
dead_tool  single      5      1388    0.50      partial   0         -
dead_tool  supervisor  5      2295    0.50      partial   0         -
dead_tool  handoff     5      2292    0.50      partial   3         -
```

读法：

- `simple`：单 Agent 最便宜（835 token），supervisor 为了委派/汇总多花约 56%。简单任务不该拆。
- `breadth`：单 Agent 的上下文上限让它丢掉了 `logs` 观察（`dropped=logs`），覆盖降到 0.67；
  supervisor 用 7876 token 换到 1.00 覆盖。多 Agent 的价值是**主上下文更小**，不是总 token 更少。
- `retry`：日志工具第一次失败，worker 自动切到健康指标；supervisor 仍然拿到完整结论。
- `dead_tool`：日志工具彻底不可用，三种拓扑都诚实返回 `partial`，并在答案里标注缺失域。
- `handoff` 的 token 与 step 数取决于共享历史的长度；它不是“更便宜的 supervisor”，而是不同的控制模型。

### Step 3 · 看 supervisor 如何委派

```bash
python3 multi_agent.py demo  --pattern supervisor --scenario breadth
python3 multi_agent.py trace --pattern supervisor --scenario breadth
```

在 trace 里找：

- `worker_context_keys`：worker 只看到 `agent / task / scenario / mode / attempt ...`，
  没有父级的 `findings` 与 `question`；
- `worker_saw_parent_findings` / `worker_saw_question`：都应为 `false`；
- `saw_other_findings`：supervisor 每轮看到多少条已有摘要；它只拿摘要，不拿原始日志。

### Step 4 · 看 handoff 如何转移控制

```bash
python3 multi_agent.py demo  --pattern handoff --scenario breadth
python3 multi_agent.py trace --pattern handoff --scenario breadth
```

trace 会显示：

```
triage -> logs_agent -> code_agent -> runbook_agent -> done
```

每个 specialist 的 `saw_history_len` 与 `saw_prior_findings` 都递增，说明它看到的是共享会话，
而不是干净窗口。对比 supervisor 的 worker：同样是 specialist，控制权与上下文边界完全不同。

### Step 5 · 注入失败

```bash
python3 multi_agent.py demo --pattern supervisor --scenario retry
python3 multi_agent.py demo --pattern supervisor --scenario dead_tool
python3 multi_agent.py demo --pattern handoff    --scenario dead_tool
```

- `retry`：`query_logs` 第一次超时，worker 的第二次 attempt 改用 `check_health`，`tool_calls=2`。
- `dead_tool`：两次都失败，worker 返回 `ok=false`；supervisor 继续跑其他域，最终 `coverage=0.50`、
  `stop=partial`。partial 不是异常，而是有预算、有证据边界的停止条件。

### Step 6 · 选做：改委派契约

打开 `multi_agent.py`，把 `QUERIES["logs"]` 改成一句模糊的“查一下日志”，再跑 `eval`。
这个离线 lab 不会因此自动变差（fixture 是固定的），但它让你看到契约字段在代码里的位置。
接真实模型时，模糊契约会直接改变 worker 的搜索范围、返回质量与 token 成本。

## 成功标准

- [ ] 15 个测试全绿；
- [ ] 能解释 worker 的 `context_keys` 为什么没有父级 `findings` / `question`；
- [ ] 能说出 supervisor 与 handoff 在控制权、上下文上的两个差别；
- [ ] 能解释 `breadth` 场景里单 Agent 的 `dropped=logs` 与 coverage 下降；
- [ ] 能说清多 Agent 为什么总 token 更高，却仍可能值得；
- [ ] 能写出一个包含 `objective / inputs / must_consider / output_schema / budget` 的委派契约；
- [ ] 能根据 trace 判断失败出在委派、worker 还是汇总。

## 哪些是模拟的，哪些是真的

**真的：** LangGraph 的图、`Command(goto=...)`、子图调用、state reducer、上下文隔离
（worker 的 state schema 确实没有父级字段）、预算终止、失败降级、trace 结构。

**模拟的：** 模型决策（`plan_tasks` / worker 的 `act` 是规则）、token 估算
（`estimate_tokens = len(text)//2`）、单 Agent 的上下文上限（`FLAT_MAX_CONTEXT`）、
fixture 里的日志与结论。因此不要把表中的绝对 token 数当作真实成本；
把它当作**架构变量之间的比例关系**。

## 接到真实模型时保留什么

把 `plan_tasks` 与 worker 的 `act` 换成模型决策，但保留这些边界：

1. worker 的输入只有委派契约，不塞父级全量 state；
2. worker 必须返回 `finding + evidence + confidence + open_questions`；
3. lead 只接收摘要与证据，不接收 worker 的原始轨迹；
4. 每次委派有稳定 task key、max_steps、max_tokens；
5. handoff 显式过滤历史，保留触发 handoff 的 AIMessage + ToolMessage；
6. trace 按 parent/worker 分层，能还原每次委派与返回。

## 生产提醒

- 多 Agent 的总 token 通常更高；不要用“省 token”作为拆分理由。
- 只在子任务可独立决策、结果可验收时拆；强耦合任务用单 Agent 或 handoff。
- 委派契约缺失会导致重复劳动、遗漏与冲突假设；MAST 的 14 种失败模式里，大量属于这一类。
- 并行 worker 之间不要写同一个外部资源；写操作要带幂等键。
- 没有单 Agent 基线的多 Agent 评测，不能证明架构更好。
