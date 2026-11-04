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
| `postgres_demo.py` | 选修：同一张图改用 `PostgresSaver` 的 CLI |
| `test_postgres_optional.py` | 选修：5 个 Postgres 测试，未设置 `TEST_POSTGRES_URL` 时自动跳过 |
| `docker-compose.yml` | 选修：本地 Postgres 16.1 |
| `requirements-postgres.txt` | 选修：Postgres checkpointer 与 `psycopg` |

## 快速开始

### Step 1 · 跑测试

```bash
python3 -m unittest test_durability -v
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

### Step 6 · 选修：把 SQLite 换成 Postgres（20–30 分钟）

SQLite 证明了“状态可以跨进程”；Postgres 证明“状态可以跨实例”。生产里通常有多个 API
副本，它们必须看到同一条 thread。`PostgresSaver` 把 checkpoint 放进共享数据库，这正是
LangSmith 使用的后端。代码仍然是同一张图，只换 checkpointer。

```bash
# 1) 启动本地 Postgres 16.1（已有同版本镜像时不会重新拉取）
docker compose up -d --wait

# 如果 5432 上已经有现成的 Postgres 16.1，可跳过上面这一步，
# 直接把 DATABASE_URL 指向它。

# 2) 安装可选依赖（会带上 psycopg 与 langgraph-checkpoint-postgres）
pip install -r requirements-postgres.txt

# 3) 建表；setup() 是幂等的，生产里应作为一次 migration 执行
export DATABASE_URL=postgresql://repomedic:repomedic@localhost:5432/repomedic
python3 postgres_demo.py init

# 4) 两个进程完成一次审批：start 退出后，resume 在另一个进程继续
python3 postgres_demo.py start --thread pg-incident-1
python3 postgres_demo.py resume --thread pg-incident-1 \
  --decision approve --approver alice --approval-id APR-PG-7

# 5) 看 checkpoint 行数与最新状态
python3 postgres_demo.py stats --thread pg-incident-1
```

再跑一遍崩溃恢复：

```bash
python3 postgres_demo.py start --thread pg-crash-1
python3 postgres_demo.py resume --thread pg-crash-1 \
  --decision approve --approver alice --approval-id APR-PG-9 \
  --crash-after-effect
python3 postgres_demo.py continue --thread pg-crash-1
```

效果与 SQLite 版相同：`diagnose_runs=1`、`execute_runs=1`、`effects[0].status="noop"`。
区别在于 checkpoint 现在在 Postgres 里，任何持有同一个 `DATABASE_URL` 的实例都能恢复。

可以直接查看底层表：

```bash
docker compose exec postgres psql -U repomedic -d repomedic -c '\dt'
docker compose exec postgres psql -U repomedic -d repomedic \
  -c 'select thread_id, count(*) from checkpoints group by 1 order by 1;'
```

跑 5 个 Postgres 测试（不设置环境变量时会自动 skip）：

```bash
TEST_POSTGRES_URL="$DATABASE_URL" python3 -m unittest test_postgres_optional -v
```

清理一条 thread 的全部 checkpoint（演示保留策略时用）：

```bash
python3 postgres_demo.py reset --thread pg-incident-1
```

> 脚本用 `PostgresSaver.from_conn_string` 的上下文管理器，一次一个连接。多线程服务应改用
> `psycopg_pool.ConnectionPool`，并在启动时只跑一次 `setup()`；不要在每次请求里建表。
> 完整示例见 `postgres_demo.py` 的 `open_postgres_graph()`。

## 成功标准

- [ ] 9 个测试全绿；
- [ ] 能解释 `start` 进程退出后，`resume` 为什么还能继续；
- [ ] 能说出 resume 时哪些节点会重跑、哪些不会，以及为什么；
- [ ] 能解释 `approval_id` 为什么既是审计字段又是幂等键；
- [ ] 能区分 `custom` 事件与 `updates` 事件，并知道 resume 后为什么会重复。
- [ ] （选修）能用 PostgresSaver 跨连接恢复同一条 thread，并说出 `setup()` 与连接池的分工。

## 生产提醒

- SQLite checkpointer 适合本地开发；生产用 `PostgresSaver`，多实例共享同一个库。
- `PostgresSaver.setup()` 是建表 migration：启动时跑一次，不要在每个请求里跑；多实例并发启动要加锁或先由发布流程执行。
- 多线程 / 多请求共享一个 `ConnectionPool`，不要每个请求新建连接；`PostgresSaver` 接受 `psycopg_pool.ConnectionPool`。
- `thread_id` 保持在 255 字符以内；一条事故一个 thread，不要把多个事故塞进同一条时间线。
- 删除 thread 前先导出审计所需的历史；`delete_thread` 会删掉 checkpoints / blobs / writes。
- JSON 台账不是并发安全的；真实实现要用唯一约束 / 事务 / outbox。
- 审批接口必须做权限校验：能调用 `Command(resume=...)` 的人，等于能批准重启。
- checkpoint 里不要放密钥、HTTP client、文件句柄；只放可序列化的状态。
- 长线程要制定 checkpoint 保留策略，否则存储会随 super-step 线性增长。
