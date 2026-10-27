# 第 1 课 Lab：LLM 工程心智模型

这一课不写 Agent，只把“一次模型调用”拆开：

- token 与上下文预算；
- 流式响应（delta / done / usage）；
- 结构化输出（JSON Schema + 校验 + 修复循环）。

## 文件

| 文件 | 作用 |
|---|---|
| `llm_client.py` | OpenAI-compatible client：流式文本、usage、非流式 tool calls；离线 `FakeStreamingLLM` |
| `structured.py` | 工单 schema、JSON 解析、字段校验、修复循环 |
| `test_llm_client.py` | 20 个离线测试，不需要 API key |
| `.env.example` | live 模式配置 |

## 快速开始（离线）

```bash
cd labs/0001-llm-client
python3 -m unittest -v

# 看流式 delta 与 usage
python3 llm_client.py --demo

# 看“第一次输出非法 → 修复循环 → 第二次通过”
python3 structured.py --demo
```

`structured.py --demo` 的第一次输出把 `needs_human` 写成了字符串 `"yes"`。
校验器拒绝它，把错误反馈给模型，第二次输出才是合法 JSON。
这就是“schema 是契约，不是建议”。

## 接真实模型

```bash
cp .env.example .env
# 填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

python3 llm_client.py --live --prompt "用一句话解释 token 预算。"
python3 structured.py --live --ticket "生产环境订单服务大量 500，用户无法下单。"
```

不同 OpenAI-compatible 供应商对 `stream_options`、`response_format` 的支持不同；
如果报错，先确认该端点是否支持流式 usage 与 JSON mode。

## 观察点

1. **流式事件**：`delta` 是增量，`done` 才带完整文本与 usage。
2. **usage**：prompt / completion / total tokens 是成本与预算的依据。
3. **schema 校验**：枚举、必填、类型、额外字段都要检查；不要 `json.loads` 之后直接用。
4. **修复循环**：把校验错误作为反馈发回去，最多重试 N 次；仍失败就降级或转人工。
5. **temperature**：Agent 任务通常用低温度；但 `temperature=0` 也不保证绝对确定。

## 练习

1. 给 `TICKET_SCHEMA` 增加 `confidence: number`，同步更新校验与测试。
2. 把 `max_repairs` 改成 0，观察非法输出如何直接失败。
3. 把 `llm_client.py` 的 `include_usage` 设为 false，观察 `usage` 为空时如何降级。
4. 用一个真实的兼容端点跑 `structured.py --live`，记录第一次失败与修复次数。

## 下一课

第 2 课进入上下文工程：system prompt、few-shot 与输出契约。
你会把这里的单次抽取升级成一个带 20 例评测的工单分类器。
