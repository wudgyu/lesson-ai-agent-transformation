# 第 9 课 Lab：可恢复、可中断的审批流

这个 lab 把第 8 课的图升级成一条**能跨进程存活**的 RepoMedic 排障流程：

```
START → diagnose → approval ─(批准)→ execute → report → END
                         └─(拒绝/无效)→ report → END
```

它刻意只保留三个新机制，其他都复用第 8 课：

- **checkpointer + thread_id**：每个 super-step 把 state 落到 SQLite；
- **interrupt / Command(resume=...)**：高风险动作前暂停，等人类决定；
- **stream_mode**：把节点增量与进度事件推给调用方。

模型是脚本化的，不需要 API key。所有测试离线运行。

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
| `graph_agent.py` | state、四个节点、条件边、CLI、SQLite checkpointer |
| `effects.py` | 外部副作用台账；用 idempotency key 防重复重启 |
| `test_durability.py` | 9 个离线测试：中断、批准、拒绝、崩溃恢复、流式、历史 |
| `requirements.txt` | `langgraph` 与 `langgraph-checkpoint-sqlite` |

## 快速开始

### Step 1 · 跑测试

```bash
python3 -m unittest -v
```

9 个测试应该全绿。重点看两个名字：

- `test_crash_after_side_effect_resumes_without_duplicating_it`
- `test_history_exposes_the_interrupted_checkpoint`

### Step 2 · 两个进程完成一次审批

```bash
# 进程 1：跑到 interrupt，打印审批 payload 后退出
python3 graph_agent.py start \
  --db .lab/checkpoints.db \
  --effects .lab/effects.json \
  --thread incident-42

# 进程 2：用同一个 thread_id 恢复，并批准
python3 graph_agent.py resume \
  --db .lab/checkpoints.db \
  --effects .lab/effects.json \
  --thread incident-42 \
  --decision approve \
  --approver alice \
  --approval-id APR-7
```

观察：`start` 退出后，`.lab/checkpoints.db` 里仍有这条 thread；`resume` 不需要重新跑
`diagnose`。`diagnose_runs` 应该保持 1。

再跑一次拒绝路径（换一个 thread）：

```bash
python3 graph_agent.py start --thread incident-43
python3 graph_agent.py resume --thread incident-43 \
  --decision reject --approver bob --note "先修 N+1"
```

`execute_runs=0`，`effects` 为空。

### Step 3 · 在副作用之后崩溃

```bash
python3 graph_agent.py start --thread crash-1

# 故意在 restart_service 写入台账之后抛异常，模拟进程崩溃
python3 graph_agent.py resume --thread crash-1 \
  --decision approve --approver alice --approval-id APR-9 \
  --crash-after-effect
# 退出码 3；checkpoint 仍指向 execute

# 新进程从 checkpoint 继续
python3 graph_agent.py continue --thread crash-1
```

你应该看到：

- `diagnose_runs=1`：已完成的节点没有重跑；
- `execute_runs=1`：未完成的 `execute` 节点从头重跑；
- `effects[0].status="noop"`：外部台账命中了 `restart:order-service:APR-9`，没有重复重启。

把 `effects.py` 里的幂等检查去掉，再跑一遍，你会得到两条重启记录。这就是
“checkpoint 不是外部副作用的事务”具体长什么样。

### Step 4 · 观察流式事件

```bash
python3 graph_agent.py stream-demo --thread stream-1
```

输出会交替出现：

- `custom:`：节点通过 `get_stream_writer()` 发出的进度；
- `updates:`：每个节点返回的增量；
- `updates: __interrupt__`：图在 `approval` 暂停。

注意 `approval` 节点在 resume 时会重新执行，所以它的 custom 事件会再发一次。
事件消费者要能容忍重复（通常带 event id 去重）。

### Step 5 · 选做：时间旅行

在 Python REPL 里打开同一个 `.lab/checkpoints.db`，拿到 `graph` 与 `config` 后：

```python
history = list(graph.get_state_history(config))
for state in history:
    print(state.next, state.metadata["step"])

before_execute = next(s for s in history if s.next == ("execute",))
fork = graph.update_state(
    before_execute.config,
    {"approval": {"approved": False, "approver": "audit", "approval_id": "", "note": "撤销", "valid": True}},
    as_node="approval",
)
print(graph.invoke(None, fork, durability="sync")["stop_reason"])
```

`update_state` 不会删除或回滚旧 checkpoint，而是从旧 checkpoint 分叉出一条新分支；
之后用不带 `checkpoint_id` 的 config 读取，会看到新分支的最新状态。用它做
“如果当时拒绝了会怎样”的复盘，而不是生产环境的回滚。

## 成功标准

- [ ] 9 个测试全绿；
- [ ] 能解释 `start` 进程退出后，`resume` 为什么还能继续；
- [ ] 能说出 resume 时哪些节点会重跑、哪些不会，以及为什么；
- [ ] 能解释 `approval_id` 为什么既是审计字段又是幂等键；
- [ ] 能区分 `custom` 事件与 `updates` 事件，并知道 resume 后为什么会重复。

## 生产提醒

- SQLite checkpointer 适合本地开发；生产用 `PostgresSaver`，多实例共享同一个库。
- JSON 台账不是并发安全的；真实实现要用唯一约束 / 事务 / outbox。
- 审批接口必须做权限校验：能调用 `Command(resume=...)` 的人，等于能批准重启。
- checkpoint 里不要放密钥、HTTP client、文件句柄；只放可序列化的状态。
- 长线程要制定 checkpoint 保留策略，否则存储会随 super-step 线性增长。
