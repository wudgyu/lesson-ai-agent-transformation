# 第 1 课 Lab：mini-agent

一个不依赖任何 Agent 框架的 ReAct 循环。约 150 行，包含工具注册、消息协议、
最大步数保护、错误恢复与离线测试。

## 文件

| 文件 | 作用 |
|---|---|
| `agent.py` | 循环本体：模型决策 → 执行工具 → 观察 → 再决策 |
| `llm.py` | 模型适配层：真实 API 与 `FakeLLM` 输出同一种结构 |
| `tools.py` | 工具注册表 + 三个工具：`list_files` / `read_file` / `calculator` |
| `test_agent.py` | 7 个离线测试，不需要 API key |
| `sandbox/notes.txt` | 唯一允许读取的目录，用来演示路径边界 |
| `.env.example` | 任意 OpenAI-compatible 端点的配置模板 |

## 快速开始

```bash
cd labs/0001-mini-agent

# 1) 离线跑通循环（不需要 API key）
python3 agent.py --demo

# 2) 跑测试
python3 -m unittest -v
```

`--demo` 使用脚本化的 `FakeLLM`，固定走两步工具调用。你应该看到：

```text
[step 1] model → tool_calls
  thought: 我先看看 sandbox 里有哪些文件。
  action: list_files({})
  observation: notes.txt

[step 2] model → tool_calls
  thought: 找到了 notes.txt，我读取它的内容。
  action: read_file({"path": "notes.txt"})
  observation: # 部署笔记 · order-service | ... | - 端口: 8081 | ...

[step 3] model → final
  thought: notes.txt 记录的部署端口是 8081。...
```

## 接入真实模型

```bash
cp .env.example .env
# 编辑 .env，填入 key / base_url / model
python3 agent.py "sandbox/notes.txt 里记录的连接池上限是多少？"
```

支持任何 OpenAI-compatible 端点：DeepSeek、Qwen（DashScope 兼容模式）、GLM、
OpenAI、Ollama、vLLM。`.env.example` 里有示例配置。

> 模型必须支持 tool calling。纯对话模型（例如未开启 function calling 的小模型）
> 会忽略 `tools` 参数，循环会在第一步直接给出最终答案。

## 练习

### 练习 1：加一个 `search_files` 工具

在 `tools.py` 的 `build_registry` 里实现：

```python
def search_files(query: str) -> str:
    hits = []
    for path in sorted(sandbox.rglob("*")):
        if not path.is_file():
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if query.lower() in line.lower():
                hits.append("%s:%d: %s" % (path.relative_to(sandbox), lineno, line.strip()))
    return "\n".join(hits) if hits else "(no matches)"
```

注册时想清楚三件事：

1. `description` 要告诉模型**什么时候用**它，而不只是它做什么；
2. `query` 的描述要说明大小写、是否支持中文；
3. 返回结果要带文件名和行号，方便模型引用。

然后加一个测试，并用真实模型问：

```bash
python3 agent.py "沙箱里哪个文件提到了连接池？给出文件名和行号。"
```

### 练习 2：亲手触发一次停止条件

```bash
python3 agent.py --demo --max-steps 1
```

观察 `AgentLoopError`。然后思考：生产环境里，除了 `max_steps`，还应该加哪些预算？
（提示：token、金额、墙钟时间、连续工具错误次数。）

### 练习 3：让工具失败

把 `sandbox/notes.txt` 临时改名为 `notes.bak`，再跑真实模型的问题。
观察模型在收到 `ERROR: file not found` 后是否会换一种方法，还是直接编造答案。
这就是第 2 课要解决的“工具错误恢复”。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `LLM_API_KEY is not set` | 没有 `.env` 或没导出 | `cp .env.example .env` 并填写 |
| `404 model not found` | `LLM_MODEL` 与端点不匹配 | 查供应商文档，改成可用模型名 |
| 模型从不调用工具 | 模型不支持 tool calling | 换支持 function calling 的模型 |
| `400 ... tool_calls` | 消息历史里的 tool_call id 不匹配 | 检查 `agent.py` 里 assistant/tool 消息的配对 |
| 中文乱码 | 终端编码 | `export LANG=C.UTF-8` |

## 下一课

第 2 课会把同一套工具接到真实任务上，重点讲**工具设计（ACI）**：
粒度、命名、错误信息、幂等与权限。你会看到“工具写得好，Agent 就聪明了一半”。
