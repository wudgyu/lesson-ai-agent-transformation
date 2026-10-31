# 第 8 课 Lab：用 StateGraph 重建 mini-agent

这个 lab 把第 3 课手写的 ReAct 循环（`labs/0003-mini-agent/agent.py`）原样重建为
LangGraph 的 `StateGraph`。你不需要 API key：模型由脚本化的 `ScriptedLLM` 扮演，
所有测试均可离线运行。

重点不是“少写几行”，而是把三件事变成显式对象：

- **state channels**：每个字段是一条通道，节点只返回增量；
- **reducers**：决定增量如何合并（append、求和、按 id 更新）；
- **conditional edges**：路由函数决定下一步去 `tools`、`finalize` 还是 `budget_exceeded`。

## 环境要求：Python ≥ 3.10

当前 LangGraph 1.2.x 要求 **Python ≥ 3.10**（PyPI 上 1.2.19 的 `requires_python >= 3.10`）。
本 lab 已在 Python 3.12.16 + LangGraph 1.2.19 上验证；本课程的旧 lab 兼容 3.9，但这个 lab 不行。先检查：

```bash
python3 --version
```

如果低于 3.10，二选一：

```bash
# 方案 A：系统已有 python3.12
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 方案 B：用 uv 管理 Python（推荐，不需要改系统 Python）
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
. .venv/bin/activate
```

如果 `uv` 也没有：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> 为什么不在 3.9 上装旧版 LangGraph？因为课程目标是 v1 的 StateGraph / checkpointer /
  interrupt 语义。用旧版本会把框架差异混进学习变量里。

## 文件

| 文件 | 作用 |
|---|---|
| `graph_agent.py` | `AgentState`、四个节点、路由函数、图编译、CLI |
| `llm.py` | `ScriptedLLM`（离线脚本模型）与 `RepeatingLLM`（演示递归上限） |
| `tools.py` | 工具注册表；`get_service_health` / `search_runbooks` |
| `sandbox/` | 健康快照与 runbook，工具唯一可读的数据 |
| `test_graph_agent.py` | 13 个离线测试：协议、reducer、路由、预算、递归上限 |
| `requirements.txt` | `langgraph>=1.0,<2` |
| `.env.example` | 换成真实模型时才需要 |

## 快速开始

```bash
cd labs/0008-langgraph-basics
python3 -m unittest -v          # 13 个测试
python3 graph_agent.py --demo   # 跑完整图，打印消息轨迹
python3 graph_agent.py --mermaid
```

期望输出（节选）：

```text
[model] 先查 order-service 的健康状态，确认连接池与错误率。
        -> get_service_health({"service": "order-service"})
[tool:get_service_health] db_pool_in_use=47 | db_pool_max=50 | error_rate=0.8% | ...
[model] 健康数据里 db_pool_max=50、使用 47，错误率 0.8%。再查 runbook 找根因。
        -> search_runbooks({"query": "connection pool"})
[tool:search_runbooks] runbooks/order-service.md:3: ## Connection pool / 连接池耗尽 | ...
[model] final: 结论：order-service 连接池上限是 50 ...

rounds=3  stop_reason=done  tool_calls=2
```

`--mermaid` 会打印类似下面的结构（节选，省略了 `config` 与 `classDef` 样式）：

```text
graph TD;
    __start__ --> model;
    model -.-> tools;
    model -. final .-> finalize;
    model -. budget .-> budget_exceeded;
    tools --> model;
    finalize --> __end__;
    budget_exceeded --> __end__;
```

## 图长什么样

```text
START → model ──(no tool_calls)──────────────→ finalize → END
          │
          ├──(tool_calls, rounds < budget)──→ tools ──→ model
          │
          └──(tool_calls, rounds >= budget)─→ budget_exceeded → END
```

四个节点各做一件事：

| 节点 | 输入 | 输出（增量） |
|---|---|---|
| `model` | 当前消息 | `messages=[AIMessage]`, `rounds=1` |
| `tools` | 最后一条 AIMessage 的 `tool_calls` | 每条调用一个 `ToolMessage`，并追加 `tool_events` |
| `finalize` | 最后一条 AIMessage | `answer`, `stop_reason="done"` |
| `budget_exceeded` | 最后一轮未执行的调用 | 跳过用的 `ToolMessage` + 终止说明 |

## State 与 reducer

```python
class AgentState(TypedDict):
    messages: Annotated[List[AnyMessage], add_messages]   # append / 按 id 更新
    rounds: Annotated[int, operator.add]                  # 每次 model +1
    tool_events: Annotated[List[Dict], operator.add]      # 每次 tools append
    max_rounds: int                                       # 默认覆盖
    question: str                                         # 默认覆盖
    answer: str                                           # 默认覆盖
    stop_reason: str                                      # 默认覆盖
```

节点只返回自己改动的字段：

```python
def model_node(state):
    response = llm.invoke(state["messages"], registry.schemas())
    return {"messages": [response], "rounds": 1}
```

- 没有 reducer 的字段是 `LastValue`：后写覆盖先写。
- `messages` 用 `add_messages`：新 id 追加，同 id 更新，支持 `RemoveMessage`。
- `rounds` 用 `operator.add`：初始 0，每次模型调用加 1。
- **不要原地修改 `state`**；返回增量，让 reducer 负责合并。

## 实验

### 实验 1：拿掉 `messages` 的 reducer

把 `graph_agent.py` 里的

```python
messages: Annotated[List[AnyMessage], add_messages]
```

改成：

```python
messages: List[AnyMessage]
```

再跑测试。`test_messages_follow_the_tool_protocol` 会失败：默认通道把每条增量**替换**掉整个消息列表，
历史只剩最后一条消息。脚本化 demo 仍可能答对，因为它只读最后一条消息；但真实模型会丢掉此前的
对话与工具结果，行为不可复现。这个实验说明：**reducer 不是语法糖，它定义状态语义**。

### 实验 2：拿掉 `rounds` 的 reducer

改成 `rounds: int`。每次 model 返回 `{"rounds": 1}`，值永远是 1，
`rounds >= max_rounds` 永远不成立，预算保护失效。当前 demo 会在 3 轮后耗尽脚本；
如果换成一直返回 tool_calls 的 `RepeatingLLM`，最后只能靠 `recursion_limit` 抛
`GraphRecursionError`。这正好演示“应用层预算”和“框架层上限”的分工。

### 实验 3：把预算调成 1

```bash
python3 graph_agent.py --max-rounds 1
```

你会看到 `stop_reason=max_rounds`，并且第一轮的工具调用收到一条
`SKIPPED: max_rounds reached...` 的 `ToolMessage`。**未执行的 tool_call 也要有对应的
tool 消息**，否则消息协议不完整。

### 实验 4：让路由永远去 tools

把 `route_after_model` 改成永远 `return "tools"`，并保留 `max_rounds=100`。
App 预算不会在短时间内触发；把 `recursion_limit` 调低（或在测试里传 `6`），
你会得到 `GraphRecursionError`。这是框架的“防失控”底线，不是任务语义。

### 实验 5：和第 3 课逐行对照

打开 `labs/0003-mini-agent/agent.py`，把它的循环步骤映射到图：

| 第 3 课的代码 | 第 8 课的图 |
|---|---|
| `messages = [...]` | `initial_state()` |
| `response = llm.complete(...)` | `model` 节点 |
| `for call in tool_calls: registry.execute(...)` | `tools` 节点 |
| `if not tool_calls: return answer` | `route_after_model → finalize` |
| `for step in range(max_steps)` | `rounds` + `budget_exceeded` |
| `trace.append(...)` | `tool_events` 通道 |
| `raise AgentLoopError` | 终止节点返回结构化 `stop_reason` |

## 测试覆盖

13 个测试分成四组：

- **消息协议**：tool_call 与 ToolMessage 一一对应；最终答案不调用工具。
- **工具错误**：未知服务、未知工具都变成 observation，不崩图。
- **预算与终止**：`max_rounds` 触发终止；未执行的调用被标记 SKIPPED；
  `recursion_limit` 捕获永不停止的路由。
- **reducer**：默认覆盖 vs `operator.add`；`add_messages` 的追加与按 id 更新；
  图可渲染为 Mermaid。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `SyntaxError` / `ModuleNotFoundError: langgraph` | Python 3.9 或未激活 venv | 用 3.10+ 建 venv，`pip install -r requirements.txt` |
| `GraphRecursionError` | 路由永远不返回 `END`，或预算失效 | 检查 `route_after_model` 与 `rounds` reducer |
| `InvalidUpdateError` | 同一 super-step 里多个节点写同一通道且无 reducer | 给通道加 reducer，或拆成串行节点 |
| 下一轮模型看不到工具结果 | `tools` 节点没有返回 `ToolMessage`，或缺少 `tool_call_id` | 每个 tool_call 配一个 ToolMessage |
| `rounds` 永远是 1 | `rounds` 没加 `Annotated[..., operator.add]` | 恢复 reducer |
| 图能跑但答案不对 | 工具返回内容 / prompt / 模型问题 | 看图、看 `tool_events`，别先怀疑框架 |

## 下一课

第 9 课给这张图加 **checkpointer、interrupt 与流式输出**：进程崩溃后从 checkpoint 恢复，
在高风险工具前暂停等待人工审批，并把每个节点的增量流式推给前端。
这一课先确保你能徒手画出图、说清每条边的条件。
