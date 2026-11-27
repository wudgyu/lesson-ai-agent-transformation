# AI Agent 开发 Resources

> 最后核验：2026-11-23。优先一手资料；中文材料仅作术语对照与补充。
> 使用方式：每节课会指定「本课精读」；其余条目按需查阅，不必通读。

## Knowledge

### LLM 基础与上下文工程

- [OpenAI — Text generation](https://developers.openai.com/api/docs/guides/text)
  **第 1 课精读（消息角色）**。文档已迁移并按 Responses / Chat Completions 拆分；该页只保留消息角色与指令优先级，采样参数在 API reference。
- [OpenAI — Create chat completion](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
  **第 1 课精读**。`messages`、temperature、top_p、max_tokens / max_completion_tokens、stream、response_format 的参数定义。
- [OpenAI — Streaming API responses](https://developers.openai.com/api/docs/guides/streaming-responses)
  **第 1 课精读**。SSE 流式事件；Responses 与 Chat Completions 的事件形态不同。
- [OpenAI — Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
  **第 1 课精读**。JSON Schema 约束、严格模式、拒绝与失败处理；第 1 课 lab 的 schema 契约来源。页面可切换 `?api-mode=chat`。
- [Anthropic — Token counting](https://docs.anthropic.com/en/docs/build-with-claude/token-counting)
  token 计数与上下文预算的官方说明；配合第 1 课的预算拆解。
- [Anthropic — Prompt engineering overview](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview)
  **第 2 课精读（前半）**。Prompt engineering 的定位、先写成功标准、何时不该靠 prompt 解决。
- [Anthropic — Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
  **第 2 课精读（后半）**。clear and direct、system prompt/roles、few-shot、XML 结构、长上下文布局；原 system-prompts 与 few-shot-examples 页面已合并到这里。
- [OpenAI — Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering)
  输出契约、分隔符、少样本与迭代方法；选读。
- [李博杰 — 《深入理解 AI Agent》第 1 章](https://github.com/bojieli/ai-agent-book/blob/main/book/chapter1.md)
  第 2 课 sidenote 来源。AI 应用工程范式变迁：提示工程 → 上下文工程 → Harness 工程 → Loop 工程 → Graph 工程。

### Responses API 兼容性与形态对照

> 2026-10 核验。Responses API 已不是 OpenAI 专属，但各家的实现深度不同；先看供应商文档，再决定是否依赖 `previous_response_id`、内置工具等能力。

- [OpenAI — Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
  官方默认接口：`instructions`、`max_output_tokens`、`text.format`、语义化流式事件。
- [Open Responses](https://www.openresponses.org)
  2026-01 发布的跨厂商开放规范，基于 Responses API；OpenRouter、Vercel、Hugging Face、LM Studio、Ollama、vLLM 等参与，2026-10 捐给 Agentic AI Foundation。
- [阿里云百炼 — OpenAI Responses 接口兼容](https://help.aliyun.com/zh/model-studio/compatibility-with-openai-responses-api)
  Qwen 系列及托管的 DeepSeek / GLM / Kimi；端点 `/compatible-mode/v1/responses`。
- [DeepSeek — Create response](https://api-docs.deepseek.com/api/create-response/)
  官方 `POST /responses`；无状态，多轮需回传完整 `input`。
- [Kimi — 在 Codex 中使用](https://platform.kimi.com/docs/guide/codex-kimi)
  原生 `/v1/responses`，Codex 配置 `wire_api = "responses"`。
- [智谱 — Response API 兼容](https://docs.bigmodel.cn/cn/guide/develop/responses/introduction)
  原生 Responses 协议；默认 `store=false`，流式结束不发 `[DONE]`。
- [MiniMax — Responses create](https://platform.minimax.cn/docs/api-reference/responses-create)
  OpenAI Responses 兼容主接口。
- [火山方舟 — 工具调用（Responses API）](https://ai.volcengine.com/docs/ark-aihub/2647006?lang=zh)
  `function_call` → `previous_response_id` → `function_call_output` 的完整流程；端点 `/api/v3/responses`。
- [腾讯 TokenHub — Responses API 兼容模式](https://cloud.tencent.cn/document/product/1823/133813)
  对仅支持 Chat Completions 的模型做服务端协议转换；不支持 `previous_response_id` / background / 内置工具。

### 状态、记忆与上下文工程

- [Anthropic — Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  **第 7 课精读**。context rot、attention budget、compaction（保留决策/未解决 bug/实现细节，丢弃冗余工具输出）、structured note-taking、sub-agent。
- [LangGraph — Memory](https://docs.langchain.com/oss/python/langgraph/memory)
  短期记忆 = thread-scoped state + checkpointer；长期记忆 = 跨会话 namespace/store；semantic / episodic / procedural；热路径 vs 后台写入。
- [OpenAI Agents SDK — Sessions](https://openai.github.io/openai-agents-python/sessions/)
  Session 自动维护对话历史；`session_input_callback` 裁剪历史；SQLite / Redis / SQLAlchemy / MongoDB 等实现；`pop_item` 纠正。
- [Sumers et al. — Cognitive Architectures for Language Agents (CoALA, arXiv:2309.02427)](https://arxiv.org/abs/2309.02427)
  工作记忆、情景记忆、语义记忆、程序记忆的模块化分类，以及结构化动作空间与决策过程。
- [Packer et al. — MemGPT (arXiv:2310.08560)](https://arxiv.org/abs/2310.08560)
  虚拟上下文管理：把主上下文当 RAM、外部存储当磁盘，通过分页/换入换出扩展可用记忆。
- [Park et al. — Generative Agents (arXiv:2304.03442)](https://arxiv.org/abs/2304.03442)
  memory stream + reflection；按 recency / importance / relevance 动态检索记忆。
- [LangChain — Context Engineering for Agents](https://blog.langchain.com/context-engineering-for-agents/)
  write / select / compress / isolate 四个动作；context poisoning / distraction / confusion / clash。

### Human-in-the-loop 与审批

- [OpenAI Agents SDK — Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
  **第 12 课精读**。`needs_approval`、`ToolApprovalItem`、`RunState.approve/reject`、序列化与恢复；重点：参数缺失时 fail closed、客户端审批的安全边界、“approval is not exactly-once execution”的警告。
- [LangChain — Human-in-the-loop](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)
  **第 12 课精读（选段）**。`HumanInTheLoopMiddleware`、`interrupt_on`、approve/edit/reject/respond 四种决定、checkpointer + thread_id、batched vs per_call。
- [LangGraph — Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
  第 9 / 12 课机制回顾。`interrupt()` / `Command(resume=...)` 的暂停恢复协议；第 12 课把它当作审批状态的底层机制。
- [12-Factor Agents — Factor 7: Contact humans with tool calls](https://github.com/humanlayer/12-factor-agents/blob/main/content/factor-07-contact-humans-with-tools.md)
  把“找人”做成结构化工具调用：`question`、`context`、`options{urgency, format, choices}`；保存 thread、跳出循环、webhook 恢复。
- [Temporal — Human-in-the-loop](https://docs.temporal.io/ai-cookbook/human-in-the-loop-python)
  durable workflow 里的审批：signal 携带决定、`wait_condition` 等待、超时结束、等待不占计算。
- [OWASP — LLM06:2025 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
  过度代理的三类成因（功能、权限、自主性）与缓解：限制工具范围、最小权限、高风险动作人工批准、下游系统强制权限。
- [EU AI Act — Article 14: Human oversight](https://artificialintelligenceact.eu/article/14/)
  高风险 AI 的人类监督义务：理解能力与局限、防范 automation bias、解释输出、推翻/逆转、随时中断；特定场景要求两位有资质的人复核。
- [Microsoft — Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/uploads/prod/2019/01/Guidelines-for-Human-AI-Interaction-camera-ready.pdf)
  CHI 2019 的 18 条指南；第 12 课用到 G2（能力边界）、G11（解释原因）、G16（传达后果）、G17（全局控制）、G18（变更通知）。
- [Google PAIR — Feedback + Control](https://pair.withgoogle.com/chapter/feedback-controls/)
  反馈与控制的设计原则：平衡自动化与控制，敏感/高风险场景需要更高的用户控制。
- [Anthropic — Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
  第 12 课选读。Agent 可在 checkpoint 或遇到 blocker 时暂停等待人类反馈；用停止条件约束自主性。

### 检索与 RAG

- [Anthropic — Citations](https://platform.claude.com/docs/en/build-with-claude/citations)
  **第 6 课精读**。结构化引用：文档切句、`cited_text`、`document_index`、字符/页码/块索引；RAG 场景下每个 retrieved chunk 作为一个 document。
- [Anthropic — Introducing Citations](https://claude.com/resources/articles/introducing-citations-api)
  发布说明：内置引用比多数自定义实现最多高 15% recall accuracy；`cited_text` 不计输出 token。
- [Pinecone — Rerankers and Two-Stage Retrieval](https://www.pinecone.io/learn/series/rag/rerankers/)
  bi-encoder vs cross-encoder 的清晰解释；为什么 recall 可以靠“返回全部”刷高；两阶段系统的工程取舍。
- [Cohere — Rerank](https://docs.cohere.com/docs/rerank-overview)
  生产级 cross-encoder API：query + documents → relevance score；常召回 50–200，重排到 top 5–20。
- [Voyage — Reranker](https://docs.voyageai.com/docs/reranker)
  另一种 rerank API；与 Voyage embedding 的 query/document 类型配合使用。
- [Gao et al. — Enabling LLMs to Generate Text with Citations (ALCE, arXiv:2305.14627)](https://arxiv.org/abs/2305.14627)
  引用质量评测基准：fluency / correctness / citation quality；citation recall/precision 用 NLI 判断。
- [Barnett et al. — Seven Failure Points When Engineering a RAG System (arXiv:2401.05856)](https://arxiv.org/abs/2401.05856)
  RAG 的 7 类失败点：missing content、missed top ranked、not in context、not extracted、wrong format、incorrect specificity、incomplete。
- [Ragas — Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)
  claim 级有据性：被 retrieved context 支持的 claim / 总 claim。答案级评测的起点。
- [Anthropic — Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
  **第 5 课精读**。给 chunk 补 50–100 token 情境说明，再做 contextual embeddings + contextual BM25；报告 top-20 失败率 −35% / −49% / −67%（加 rerank）。含成本与 prompt caching 细节。
- [Pinecone — Chunking Strategies](https://www.pinecone.io/learn/chunking-strategies/)
  chunk 大小、重叠、fixed / recursive / structure-aware / semantic 切分的取舍。第 5 课 lab 的默认值来源。
- [Lewis et al. — Retrieval-Augmented Generation (arXiv:2005.11401)](https://arxiv.org/abs/2005.11401)
  RAG 原始论文：参数化记忆 + 非参数化记忆。读摘要与第 1–2 节即可。
- [Weaviate — Hybrid Search](https://docs.weaviate.io/weaviate/search/hybrid)
  BM25 + 向量融合的工程实现，`alpha` 权重与 ranked fusion。第 5 课 hybrid 检索的对照。
- [Ragas — Available Metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/)
  context recall / context precision / faithfulness 等 RAG 评测指标。第 3 周评测扩展的基础。
- [MTEB Leaderboard](https://huggingface.co/spaces/mteb/leaderboard)
  embedding 模型排行榜。只作起点，最终要在自己的语料上测。
- [Voyage — Embeddings](https://docs.voyageai.com/docs/embeddings)
  query/document 分开编码、维度与模型选择的工程细节。
- [Liu et al. — Lost in the Middle (arXiv:2307.03172)](https://arxiv.org/abs/2307.03172)
  长上下文中间信息利用率下降的实证。解释为什么“全塞进去”不是方案。

### 第一性原理与架构

- [Anthropic — Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
  最重要的起点。区分 workflow 与 agent，给出 5 种 workflow 模式、agent 的适用边界、ACI 工具设计原则。**第 1 周精读**。
- [Anthropic — Writing Effective Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
  工具设计的 5 条原则：少而好、命名空间、返回语义化上下文、token 效率、把 description 当 prompt。**第 2 周精读**。
- [Anthropic — How We Built Our Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system)
  **第 10 课精读**。orchestrator-worker 的真实工程复盘：90.2% 内部评测提升、约 15× chat token、token 解释 80% 方差、3–5 并行 subagents、失败模式与 LLM-judge 评测。
- [Cognition — Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents)
  **第 10 课精读（反方）**。共享完整上下文与 agent trace；动作携带隐含决策；Flappy Bird 例子；建议默认单线程线性 Agent，用压缩而非并行切片处理上下文溢出。
- [Why Do Multi-Agent LLM Systems Fail? (MAST, arXiv:2503.13657)](https://arxiv.org/abs/2503.13657)
  **第 10 课精读（选段）· 第 14 课（失败分类）**。14 种失败模式、3 类（system design / inter-agent misalignment / task verification）；1,600+ 轨迹、7 个框架、0.88 kappa；多数失败来自系统设计而非底层模型。
- [ReAct: Synergizing Reasoning and Acting in Language Models (arXiv:2210.03629)](https://arxiv.org/abs/2210.03629)
  ReAct 循环的原始论文。读第 1–2 节即可建立直觉。**第 3 课选读**。
- [Lilian Weng — LLM Powered Autonomous Agents](https://lilianweng.github.io/posts/2023-06-23-agent/)
  经典分类：Planning / Memory / Tool Use。用于建立全局地图，不是实现指南。
- [12-Factor Agents (HumanLayer)](https://github.com/humanlayer/12-factor-agents)
  生产 Agent 的 12 条工程原则：own your control flow、small focused agents、stateless reducer 等。第 3 周精读。

### 课程与系统学习

- [Hugging Face — AI Agents Course（中文版）](https://huggingface.co/learn/agents-course/zh-CN/unit0/introduction)
  免费、开源、有证书。Unit 1 的 Thought-Action-Observation 与 Unit 2 的框架对比很适合作为本课程的并行阅读。**贯穿 4 周，按单元选读**。
- [Anthropic Academy](https://www.anthropic.com/learn)
  官方免费课程：Claude API、MCP、Claude Code 等。用于补齐 API 与 MCP 的操作细节。
- [OpenAI — A Practical Guide to Building Agents (PDF)](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)
  面向工程团队的实施指南：单 Agent / 多 Agent、guardrails、HITL。第 2 周选读。
- [AI Engineer Conference](https://www.ai.engineer/)
  一线团队的生产实践演讲。第 3–4 周选看 2–3 个与评测、部署相关的 talk。

### 框架与协议

- [OpenAI Agents SDK (Python)](https://openai.github.io/openai-agents-python/)
  核心原语：Agent、Handoff、Guardrail、Session、Tracing。第 2 周对照学习。
- [OpenAI Agents SDK — Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
  **第 10 课精读**。handoff 作为 tool（`transfer_to_<agent>`）、控制转移、默认接管对话历史、`input_filter` / `HandoffInputData` 历史过滤、`Agent.as_tool()` 与 handoff 的分工。
- [OpenAI — Orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)
  第 10 课选读。manager（agents-as-tools）vs handoffs 的对比与选型；先从一个 Agent 开始，只在能力/策略隔离、prompt 清晰度或 trace 可读性有收益时拆 specialist。
- [LangChain — Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent)
  **第 10 课精读**。subagents / handoffs / skills / router / custom workflow 五种模式；调用次数与 token 成本的示例对比；按并发、上下文隔离、直接对话与状态连续性选型。
- [LangChain — Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
  第 10 课精读。subagent 默认无状态、独立上下文窗口；`create_agent` + `@tool` 包装；同步/异步、tool-per-agent / single dispatch、输入输出与 checkpointer 设计。
- [LangChain — Handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs)
  第 10 课精读。`Command(goto=..., update={...})` 与 `graph=Command.PARENT`；单 Agent + middleware 与多子图两种实现；消息过滤与 tool_call / ToolMessage 配对。
- [LangChain — Migrate from langgraph-supervisor](https://docs.langchain.com/oss/python/migrate/langgraph-supervisor)
  2026 现状：`langgraph-supervisor` 不再积极维护，官方建议改用 tool 包装的 subagent 或 handoff 写法；迁移映射与 interrupt/resume 传播规则。
- [LangGraph v1 文档](https://docs.langchain.com/oss/python/langgraph/overview)
  状态图、reducer、checkpoint、interrupt、durable execution。**第 2 周主框架**。
- [LangGraph — Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
  **第 8 课精读**。State schema、reducer、node、edge、conditional edge、recursion limit；`add_messages` 与 `InvalidUpdateError` 的官方说明。
- [LangGraph — Thinking in LangGraph](https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph)
  第 8 课选读。状态只存原始数据、节点保持单一职责、把错误处理放进流程；是设计图之前的检查清单。
- [LangGraph — Use the graph API](https://docs.langchain.com/oss/python/langgraph/use-graph-api)
  `Command`、`Send`、并行分支、`recursion_limit`、`RemainingSteps`、`RetryPolicy` 与错误处理。第 8–9 课查手册。
- [LangGraph — Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)
  **第 9 课精读**。super-step 与 checkpoint、pending writes、thread、get_state / get_state_history / update_state、durability 三模式（sync / async / exit）、序列化与保留策略。第 9 课 lab 的语义依据。
- [LangGraph — Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
  **第 9 课精读**。interrupt() 与 Command(resume=...) 的暂停/恢复协议、节点从头重跑、循环内多次 interrupt 的陷阱、并行 interrupt 按 id 配对、response_schema 校验。
- [LangGraph — Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
  checkpointer（thread 作用域）与 store（跨 thread）的分工；InMemorySaver / SqliteSaver / PostgresSaver；子图 checkpoint namespace。
- [LangGraph — Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)
  第 9 课选读。stream_mode 的 values / updates / messages / custom / debug / checkpoints；`version="v2"` 的统一 StreamPart；子图流式。
- [LangGraph — Time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
  第 9 课选读。get_state_history、update_state 分叉、replay 与 fork 的重执行边界。
- [langgraph-checkpoint-sqlite (PyPI)](https://pypi.org/project/langgraph-checkpoint-sqlite/)
  第 9 课 lab 的本地持久化依赖；SQLite 适合开发，生产用 PostgresSaver。
- [langgraph-checkpoint-postgres (PyPI)](https://pypi.org/project/langgraph-checkpoint-postgres/)
  第 9 课选修。`PostgresSaver` / `AsyncPostgresSaver`、`setup()` 建表、`delete_thread`；依赖 `psycopg` 与 `psycopg-pool`。
- [PostgresSaver API reference](https://reference.langchain.com/python/langgraph/checkpoints/#langgraph.checkpoint.postgres.PostgresSaver)
  构造参数（`Connection` / `ConnectionPool`）、`from_conn_string`、`setup`、`list`、`delete_thread` 的精确签名。
- [psycopg 3 — Connection pools](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)
  多线程服务的连接池配置；`autocommit=True`、`prepare_threshold=0`、`row_factory=dict_row` 的来源。
- [LangGraph — add_messages reference](https://reference.langchain.com/python/langgraph/graph/message/add_messages)
  `add_messages` 的精确语义：新 id 追加、同 id 更新、字典反序列化。第 8 课 lab 的 messages 通道依据。
- [PyPI — langgraph](https://pypi.org/project/langgraph/)
  版本、`requires_python` 与依赖。2026-10 核验：1.2.19 要求 Python >= 3.10；第 8 课 lab 因此需要 3.10+ 环境。
- [Model Context Protocol — 2026-07-28 规范：Key Changes](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
  **第 11 课精读（前半）**。无状态核心：移除 `initialize`/session、每次请求 `_meta`、`server/discover`、MRTR、`subscriptions/listen`、移除 SSE 重放、tasks 变扩展、Roots/Sampling/Logging 弃用。
- [MCP — Architecture](https://modelcontextprotocol.io/specification/2026-07-28/architecture)
  **第 11 课精读**。Host / client / server 的 1:1 关系、能力协商、server 不拥有整段对话的设计原则。
- [MCP — Server / Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
  **第 11 课精读（后半）**。`tools/list`、`tools/call`、`inputSchema`/`outputSchema`、`structuredContent`、annotations、协议错误 vs 工具执行错误、安全注意事项。
- [MCP — Server / Resources](https://modelcontextprotocol.io/specification/2026-07-28/server/resources)
  Application-driven 资源、URI 模板、`resources/read`、annotations、资源不存在错误码。
- [MCP — Server / Prompts](https://modelcontextprotocol.io/specification/2026-07-28/server/prompts)
  User-controlled 模板、`prompts/list` / `prompts/get`、参数与嵌入资源。
- [MCP — Security Best Practices](https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices)
  **第 11 课精读（安全）**。Confused deputy、token passthrough、SSRF、state handle hijacking、本地 server 妥协、scope 最小化。
- [MCP Python SDK v2 — What's new](https://py.sdk.modelcontextprotocol.io/whats-new/)
  `MCPServer`（原 FastMCP）、统一 `Client`、内存/stdio/HTTP 传输、`ToolError`、迁移要点。
- [MCP Python SDK — Tools](https://py.sdk.modelcontextprotocol.io/servers/tools/index.md)
  `@mcp.tool`、类型注解生成 schema、结构化返回、注解与校验。第 11 课 lab 的 API 依据。
- [MCP Python SDK — Client](https://py.sdk.modelcontextprotocol.io/client/)
  `Client` 生命周期、`list_tools` / `call_tool`、资源与 prompt、stdio 参数、错误语义。
- [MCP Python SDK — Testing](https://py.sdk.modelcontextprotocol.io/get-started/testing/index.md)
  `Client(mcp)` 内存测试、pytest + anyio 模式、`raise_exceptions=True`。
- [PyPI — mcp](https://pypi.org/project/mcp/)
  2026-11 核验：2.3.x，要求 Python ≥ 3.10；第 11 课 lab 的依赖。
- [Anthropic — Donating MCP to the Agentic AI Foundation](https://www.anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation)
  2025-12-09 捐赠给 Linux Foundation 下的 AAIF；AAIF 由 Anthropic、OpenAI、Block 共同发起，Google/Microsoft/AWS 等为成员。
- [MCP — Getting Started](https://modelcontextprotocol.io/docs/getting-started/intro)
  快速搭建 server/client 的官方入口。

### 评测方法与校准

- [Anthropic — Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
  **第 13–14 课精读**。20–50 个真实任务起步、任务无歧义且有参考解、code/model/human grader 取舍、
  capability vs regression、transcript vs outcome、pass^k、读 transcript、eval saturation、评测团队与维护。
- [Hamel Husain — Your AI Product Needs Evals](https://hamel.dev/blog/posts/evals/)
  **第 13 课精读（前半）**。三层评测（断言 / 人工+模型 / A/B）、从失败中建 case、
  单元断言与 trace 审查；核心主张：失败的 AI 产品通常缺一套可靠评测系统。
- [Hamel Husain — Creating a LLM-as-a-Judge that drives business results](https://hamel.dev/blog/posts/llm-judge/)
  **第 13 课精读（后半）**。critique shadowing、binary pass/fail + critique、30 个样本起步、
  每类失败模式约 100 个、10–20/40–45/40–45 划分、TPR/TNR 与 5% 失败率的 agreement 陷阱。
- [Zheng et al. — Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena (arXiv:2306.05685)](https://arxiv.org/abs/2306.05685)
  **第 13 课精读（偏差）**。position / verbosity / self-enhancement bias 与有限推理；
  强 judge 与人类偏好 agreement 可超过 80%，与人类-人类 agreement 相当。
- [Shankar et al. — Who Validates the Validators? (EvalGen, arXiv:2404.12272)](https://arxiv.org/abs/2404.12272)
  UIST 2024。criteria drift：标准与输出互相塑形；评测集与 rubric 必须迭代，不能一次性定死。
- [Yao et al. — τ-bench (arXiv:2406.12045)](https://arxiv.org/abs/2406.12045)
  **第 14 课精读（pass^k）**。用户模拟 + 数据库状态验收；pass^k = E_task[C(c,k)/C(n,k)]；GPT-4o retail pass^1 = 61.2%，pass^8 < 25%。
- [Kapoor et al. — AI Agents That Matter (arXiv:2407.01502)](https://arxiv.org/abs/2407.01502)
  成本受控的 Agent 评测、准确率-成本 Pareto、holdout 与 benchmark overfitting；第 13–14 课的成本与留出集依据。
- [LangSmith — Trajectory evals](https://docs.langchain.com/langsmith/trajectory-evals)
  **第 14 课（轨迹评分）**。strict / unordered / subset / superset 四种匹配模式；`agentevals` 的 trajectory match evaluator 与 LLM-as-judge trajectory evaluator；参考轨迹的适用边界。
- [OpenAI Agents SDK — Tracing](https://openai.github.io/openai-agents-python/tracing/)
  **第 14 课（trace schema）**。trace 是 end-to-end operation，span 是有起止时间的 operation；Runner、generation、function tool、handoff、guardrail 的默认 span。
- [OpenTelemetry — GenAI agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
  **第 14 课（trace 语义）**。`invoke_agent {agent.name}`、`execute_tool {tool.name}`、`gen_ai.operation.name`、`gen_ai.tool.call.id`、token 用量；状态为 Development，需 pin 版本。
- [OpenAI — Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
  **第 14 课（轨迹评分对照）**。给 Agent trace 的决策、工具调用、handoff 打结构化分数；从 trace grading 迁移到 dataset eval 的路径。
- [OpenAI — Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
  指标型 / 人工 / LLM-as-judge 三类 evaluator；Agent 专项类别：指令遵循、工具选择、参数精度、功能正确性、handoff。
- [OpenAI — Graders](https://developers.openai.com/api/docs/guides/graders)
  代码 grader（`string_check` / `text_similarity` / `python`）与模型 grader（`label_model` / `score_model`）；
  grader 校验与 reward hacking 防护。第 13 课“先代码后模型”的 API 对照。
- [LangSmith — How to define an LLM-as-a-judge evaluator](https://docs.langchain.com/langsmith/llm-as-judge)
  feedback schema 即 rubric；Boolean / Categorical / Continuous；人工修正自动成为 few-shot。

### 可观测性、成本与延迟

- [OpenTelemetry — GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)
  **第 15 课精读**。agent / inference / tool span 的命名与属性；`gen_ai.usage.input_tokens` / `output_tokens` / `cache_read` / `reasoning` 的“子集”语义；状态为 Development，需 pin 版本。
- [OpenTelemetry — GenAI token metrics](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-token-metrics.md)
  **第 15 课精读（账本）**。`gen_ai.client.inference.usage.*` 计数器与 operation 直方图；为什么不要在全部 span 上直接求和；metrics 不受 trace sampling 影响。
- [OpenTelemetry — GenAI agent spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
  **第 15 课（trace schema）**。`invoke_agent`、`execute_tool`、`create_agent`、`plan` 的 span 名、必需属性与 token 归属。
- [OpenAI Agents SDK — Tracing](https://openai.github.io/openai-agents-python/tracing/)
  **第 15 课（实现对照）**。trace / span、task/turn/agent/generation/function/handoff span、`custom_span()`、processors/exporters、`flush_traces()`。
- [OpenAI Agents SDK — Usage](https://openai.github.io/openai-agents-python/usage/)
  第 15 课（usage 对照）。`result.context_wrapper.usage` 的 requests / input / output / cached / reasoning token 与 `request_usage_entries`。
- [LangSmith — Cost tracking](https://docs.langchain.com/langsmith/cost-tracking)
  **第 15 课（成本模型）**。`usage_metadata` 的 token / cost 字段、价格表、cache / reasoning 分项、价格更新不回溯的 caveat。
- [Langfuse — Token & cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
  **第 15 课（缺失 usage）**。`usage_details` / `cost_details` 的非重叠桶；2026-11 停止自动 tokenization，缺失 usage 不估算。
- [OpenAI — Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
  第 15 课（cache 账本）。cached input 折扣、cache write 溢价、`prompt_tokens_details.cached_tokens` / `prompt_cache_key`。
- [Dean & Barroso — The Tail at Scale (CACM 2013)](https://research.google/pubs/the-tail-at-scale/)
  **第 15 课（尾延迟）**。fan-out 放大：1% 叶子慢 × 100 路 → 63.4% 请求慢；hedged requests 与 tail-tolerant 设计。
- [Google SRE Workbook — Implementing SLOs](https://sre.google/workbook/implementing-slos/)
  第 15 课（延迟 SLO）。延迟 SLI 写成“低于阈值的请求比例”，不要用平均；多阈值/多百分位观察长尾。
- [Where the Tokens Go: A Measured Anatomy of LLM Agent Trajectory Costs (REaLM 2026)](https://aclanthology.org/2026.realm-1.24.pdf)
  第 15 课（长上下文成本）。把轨迹成本拆成固定前缀、回显的助手历史与工具结果历史；同一段历史在后续步骤被反复计费。
- [Kapoor et al. — AI Agents That Matter (arXiv:2407.01502)](https://arxiv.org/abs/2407.01502)
  第 15 课（成本口径）。成本受控的 Agent 评测、准确率-成本 Pareto；cost per success 而不是单次调用成本。

### 安全与权限

- [OWASP — LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
  **第 16 课精读（前半）**。直接 vs 间接注入；为什么 OWASP 认为目前仍不清楚是否存在 fool-proof 的预防方法；输入/输出过滤、最小权限、人工审批、标记不可信内容等缓解。
- [OWASP — LLM06:2025 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
  **第 16 课精读（后半）**。过度代理的三个根因（excessive functionality / permissions / autonomy）；“authorization in downstream systems rather than relying on an LLM”的工程含义。
- [OWASP — Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
  第 16 课（agentic 风险框架）。ASI01 Goal Hijack、ASI02 Tool Misuse、ASI03 Identity & Privilege Abuse、ASI05 Unexpected Code Execution；least agency 与“把所有自然语言输入视为不可信”。
- [Simon Willison — The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
  **第 16 课（威胁模型）**。私有数据 + 不可信内容 + 对外通信；为什么 95% 拦截率在应用安全里是不及格；拆掉一条腿。
- [Meta — Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/)
  **第 16 课（设计启发式）**。单个 session 最多同时具备 A/B/C 中的两项；三项齐备时需要人类监督、可靠验证或干净 session。
- [Debenedetti et al. — CaMeL: Defeating Prompt Injections by Design (arXiv:2503.18813)](https://arxiv.org/abs/2503.18813)
  **第 16 课（能力 + 数据流）**。可信查询决定控制流，不可信数据被跟踪数据流，capability 策略在工具调用处执行；AgentDojo 77% vs 84%。
- [Beurer-Kellner et al. — Design Patterns for Securing LLM Agents against Prompt Injections (arXiv:2506.08837)](https://arxiv.org/abs/2506.08837)
  **第 16 课（架构模式）**。Action-Selector、Plan-Then-Execute、LLM Map-Reduce、Dual LLM、Code-Then-Execute、Context-Minimization；utility/security 取舍。
- [Nasr et al. — The Attacker Moves Second (arXiv:2510.09023)](https://arxiv.org/abs/2510.09023)
  **第 16 课（评测方法）**。12 种防御在自适应攻击下多数 ASR > 90%；静态、单轮测试不足以证明防御有效。
- [OpenAI Agents SDK — Guardrails](https://openai.github.io/openai-agents-python/guardrails/)
  **第 16 课（实现边界）**。input / output / tool guardrail 的覆盖范围；`run_in_parallel` 的副作用取舍；handoff/hosted tools 不在 tool guardrail 管线里。
- [Anthropic — Claude Code sandboxing](https://www.anthropic.com/research/claude-code-sandboxing)
  **第 16 课（沙箱）**。bubblewrap / seatbelt、文件系统与网络隔离、Unix socket 代理、权限提示减少 84%；“effective sandboxing requires both filesystem and network isolation”。
- [Anthropic — How we contain Claude across products](https://www.anthropic.com/research/how-we-contain-claude)
  第 16 课选读。gVisor / microVM / OS sandbox 的产品级 containment；隔离原语与自定义 glue code 的失败案例。
- [Simon Willison — New prompt injection papers: Agents Rule of Two and The Attacker Moves Second](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/)
  第 16 课（两份论文的对照摘要）；适合先读一遍再决定精读哪一篇。

### 服务化、流式与可靠性

- [FastAPI — Server-Sent Events](https://fastapi.tiangolo.com/tutorial/server-sent-events/)
  **第 17 课精读（前半）**。`EventSourceResponse` / `ServerSentEvent`（0.135+）；自动 keep-alive（15s ping）、`Cache-Control: no-cache`、`X-Accel-Buffering: no`；`Last-Event-ID` 恢复与 POST SSE。
- [AWS Builders' Library — Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
  **第 17 课精读（后半）**。timeout 取值与 p99.9；重试是“自私的”；capped exponential backoff + full jitter；retry token bucket；5 层 × 3 次 = 243 倍放大；只让一层重试。
- [FastAPI — Concurrency and async / await](https://fastapi.tiangolo.com/async/)
  第 17 课（事件循环）。`def` 路由走线程池、`async def` 直接 await；阻塞 SDK 不要放进 `async def`；CPU-bound 用 process pool。
- [FastAPI — Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)
  第 17 课（反例）。同进程、响应后执行的小任务；不是 job queue，不保证跨重启，重活要用外部队列。
- [WHATWG HTML — Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
  **第 17 课（SSE 规范）**。last event ID、重连、`Last-Event-ID`、UTF-8、事件流解析、204 停止重连。
- [MDN — Using server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
  第 17 课（SSE 入门）。单向连接、字段格式、`retry`、注释 keep-alive；HTTP/1.1 每浏览器每域约 6 条连接。
- [Python asyncio — Task cancellation](https://docs.python.org/3/library/asyncio-task.html)
  第 17 课（取消语义）。任务只在 `await` 点响应取消；清理阶段用 `shield`；`Task.cancelling()` 与超时。
- [Google SRE Book — Handling Overload](https://sre.google/sre-book/handling-overload/)
  第 17 课（自适应限流）。client-side adaptive throttling 公式 `max(0, (requests−K×accepts)/(requests+1))`；K 通常取 2；本地拒绝也计入 requests。
- [Stripe — Scaling your API with rate limiters](https://stripe.com/blog/rate-limiters)
  第 17 课（准入控制）。request rate limiter、concurrent requests limiter、fleet usage load shedder、worker utilization load shedder；限流器 fail open 与 kill switch。
- [RFC 6585 — 429 Too Many Requests](https://www.rfc-editor.org/rfc/rfc6585)
  第 17 课（HTTP 语义）。429 与 `Retry-After`；429 响应不能被缓存。
- [IETF — Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)
  **第 17 课（幂等）**。key 唯一性、payload fingerprint、进行中 409、同 key 不同 payload 422、有效期；注意仍是 Active Internet-Draft，不是 RFC。
- [Kubernetes — Pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-termination/)
  第 17 课（关停）。SIGTERM、`preStop`、`terminationGracePeriodSeconds`（默认 30s）、endpoint 摘除与 SIGKILL。
- [Uvicorn — Deployment](https://uvicorn.dev/deployment/)
  第 17 课（部署）。workers 使用 spawn 而非 pre-fork；`--reload` 与 `--workers` 互斥；代理头只信任 `--forwarded-allow-ips`。
- [Uvicorn — Settings](https://uvicorn.dev/settings/)
  第 17 课（开关）。`--timeout-graceful-shutdown`、`--limit-concurrency`（超额直接 503）、`--proxy-headers`。
- [OpenAI — Streaming API responses](https://developers.openai.com/api/docs/guides/streaming-responses)
  第 17 课选读。Chat Completions 的 `data:` 分块与 `[DONE]`；Responses 的语义化事件（`response.output_text.delta`、`response.completed`）；部分输出更难审核。

### 评测、可观测与安全

- [LangSmith — Evaluation](https://docs.langchain.com/langsmith/evaluation)
  数据集、evaluator、实验对比。第 3 周评测实验使用。
- [DeepEval — Getting Started](https://deepeval.com/docs/getting-started)
  开源评测框架，支持任务完成度、工具正确性、轨迹指标。第 3 周动手用。
- [OWASP — Top 10 for LLM Applications](https://genai.owasp.org/llm-top-10/)
  Prompt injection、敏感信息泄露、过度代理等风险清单。第 3 周安全课的依据。
- [Anthropic Claude Cookbooks](https://github.com/anthropics/claude-cookbooks)
  官方 notebook：工具调用、上下文工程、评测。遇到具体实现问题时查。

### 书

- [Chip Huyen — AI Engineering (O'Reilly, 2025)](https://www.oreilly.com/library/view/ai-engineering/9781098166298/)
  系统性强，适合补 RAG、评测、推理优化、生产化的底层原理。作为第 1、3 周的参考书，不必逐章读。

### 中文补充（非一手，仅对照术语）

- [JavaGuide — AI / Agent 章节](https://github.com/Snailclimb/JavaGuide)
  对 Java 背景读者友好，术语翻译可参考；技术判断仍以英文一手资料为准。

## Wisdom (Communities)

- [r/AI_Agents](https://www.reddit.com/r/AI_Agents/)
   practitioner 密度较高。适合问「这个架构在生产里踩过什么坑」，不适合问入门语法。
- [LangChain Forum](https://forum.langchain.com/)
  LangGraph / LangSmith 的官方社区，框架细节与报错求助响应快。
- [Hugging Face Discord](https://huggingface.co/join/discord)
  HF Agents Course 的课程答疑与 #agents-course 频道。
- [MCP GitHub Discussions](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions)
  协议设计、实现差异、SDK 问题的一手讨论。
- [Latent Space](https://www.latent.space/)
  高信号 podcast / newsletter，采访一线 Agent 团队。适合建立技术品味，而非查 API。
- [OpenAI Developers Discord](https://discord.gg/openai)
  Agents SDK、Evals、Realtime 的官方支持社区。

## Gaps

- 缺少**持续维护的中文 Agent 工程化课程**：HF 课程有中文翻译，但案例与部署环境偏英文生态。
- 缺少**面向 Java 后端转型者**的 Python 工程实践材料：本课程的 labs 会补这一块。
- 生产级 **Agent 评测** 的中文资料稀缺：建议直接读 LangSmith / DeepEval 文档与 2026 年论文。
- 本地线下社区未知：若你所在城市有 AI Engineer / LLM 应用 meetup，第 3 周后再补入。
