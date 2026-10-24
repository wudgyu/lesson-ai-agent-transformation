# 第 2 课 Lab：工具设计（ACI）

同一套 Agent 循环，两套工具设计：

- **baseline**：7 个薄包装，参数名缩写，描述只有一句话，日志不过滤，重启无审批；
- **refactored**：4 个高层工具，返回语义化上下文，日志过滤 + 截断，重启需要审批且幂等。

评测集是 10 个 on-call 场景。你要做的不是“让模型更聪明”，而是**让工具更容易被正确使用**。

## 文件

| 文件 | 作用 |
|---|---|
| `tools_baseline.py` | 对照组的 7 个薄包装 |
| `tools_refactored.py` | 重构后的 4 个高层工具 |
| `data_access.py` | 领域数据访问（服务、日志、runbook、审批） |
| `tool_base.py` | schema 暴露与错误转文本 |
| `agent.py` | 与第 1 课相同的循环，只换工具集 |
| `eval.py` | 10 例工具选择评测 |
| `eval_cases.json` | 评测问题、期望工具、答案关键词、安全规则 |
| `test_tools.py` | 15 个离线契约测试 |
| `sandbox/` | 固定场景数据（`NOW = 2026-10-24 20:40 +08:00`） |

## 快速开始

```bash
cd labs/0002-tool-design

# 1) 离线校验评测集与工具名
python3 eval.py --validate

# 2) 跑契约测试
python3 -m unittest -v

# 3) 看两种工具设计下的循环差异（不需要 API key）
python3 agent.py --demo --tools baseline
python3 agent.py --demo --tools refactored
```

`--demo` 使用脚本化的 `FakeLLM`：baseline 要 4 步（3 次工具 + 1 次回答），
refactored 只要 2 步（1 次工具 + 1 次回答）。

## 跑真实评测

```bash
cp .env.example .env
# 填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

python3 eval.py --tools baseline --out baseline.json
python3 eval.py --tools refactored --out refactored.json
```

报告会给出：

- `pass`：答案关键事实 + 安全规则都通过（任务完成且不闯祸）；
- `first-tool`：模型第一步是否选了合适的工具，作为诊断指标；
- `answer`：最终回答是否包含关键事实；
- `safety`：Q8 的重启是否被审批拦住；
- `avg steps` / `tokens`：同一任务要付多少步数与上下文成本。

**先跑 baseline，再跑 refactored，把两个报告放在一起看。**
不要只看 pass 率：baseline 可能也能答对一部分问题，但它通常要多花 2–3 倍步数，
并且在 Q8 上会把“没有审批的重启”当成成功。

## 五个设计原则（对照代码看）

1. **少而好**：`get_service_health` 合并了 baseline 的 `get_service` + `get_metrics` + `get_deploys`。
2. **命名与边界**：`service` / `level` / `since_minutes` / `limit`，而不是 `svc` / `p`。
3. **返回语义化上下文**：`summary` 直接说“degraded，48/50，v2.14.1”，而不是一串裸字段。
4. **Token 效率**：`query_logs` 有级别、时间窗口、条数上限、单行截断；`search_runbooks` 只返回 top-k。
5. **description 是 prompt**：写清何时用、何时不用、没有审批怎么办。

## 练习

### 练习 1：给 `search_runbooks` 加 `response_format`

增加一个参数 `response_format: "concise" | "detailed"`：

- `concise`：只返回 `source:line` 与一行摘要；
- `detailed`：返回当前格式。

然后用 `search_runbooks` 问“连接池”，比较两种格式的返回 token 估算。
思考：什么时候该让模型自己选格式？什么时候应该由调用方固定？

### 练习 2：补一个评测用例

在 `eval_cases.json` 里加 Q11：“payment-service 最近一次部署是什么版本？”
为 baseline 与 refactored 分别写 `expected_first_tools` 和 `answer_any`。
跑 `python3 eval.py --validate`，再跑真实评测。

### 练习 3：制造一次“工具描述导致的误选”

把 `query_logs` 的 description 改成“查询日志”，去掉“级别/时间窗口”的说明。
重跑 Q2 / Q7，观察 first-tool 与 answer 的变化。然后改回来。
这个练习的目的是让你亲眼看到：**description 不是文档，是行为控制**。

### 练习 4：审计写操作

阅读 `tools_refactored.py` 的 `restart_service`，回答：

1. 如果模型猜了一个 `approval_id`，会发生什么？
2. 如果同一个审批单被调用两次，会发生什么？
3. 如果把 `restart_service` 暴露给一个没有审批数据的租户，应该怎么改？

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `LLM_API_KEY is not set` | 没有 `.env` | `cp .env.example .env` 并填写 |
| `first-tool` 很低 | 工具描述太短或工具太多 | 先合并工具，再补“何时用/不用” |
| `answer` 很低但工具调用对 | 返回格式不利于引用 | 加 `summary` / 来源 / 行号 |
| Q8 安全失败 | 模型成功调用了重启 | 检查工具层是否有审批校验，不要只靠 prompt |
| `no logs matched` | 时间窗口是固定的 20:40 | 看 `data_access.NOW`，这是可复现场景数据 |

## 下一课

第 3 课进入 RAG：当工具需要“在大量私有文档里找到正确的那几段”，
检索质量就会成为 Agent 的上限。你会把 `search_runbooks` 换成一个真正的混合检索 + 重排管线。
