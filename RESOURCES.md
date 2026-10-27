# AI Agent 开发 Resources

> 最后核验：2026-10-27。优先一手资料；中文材料仅作术语对照与补充。
> 使用方式：每节课会指定「本课精读」；其余条目按需查阅，不必通读。

## Knowledge

### 状态、记忆与上下文工程

- [Anthropic — Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  **第 5 课精读**。context rot、attention budget、compaction（保留决策/未解决 bug/实现细节，丢弃冗余工具输出）、structured note-taking、sub-agent。
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

### 检索与 RAG

- [Anthropic — Citations](https://platform.claude.com/docs/en/build-with-claude/citations)
  **第 4 课精读**。结构化引用：文档切句、`cited_text`、`document_index`、字符/页码/块索引；RAG 场景下每个 retrieved chunk 作为一个 document。
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
  **第 3 课精读**。给 chunk 补 50–100 token 情境说明，再做 contextual embeddings + contextual BM25；报告 top-20 失败率 −35% / −49% / −67%（加 rerank）。含成本与 prompt caching 细节。
- [Pinecone — Chunking Strategies](https://www.pinecone.io/learn/chunking-strategies/)
  chunk 大小、重叠、fixed / recursive / structure-aware / semantic 切分的取舍。第 3 课 lab 的默认值来源。
- [Lewis et al. — Retrieval-Augmented Generation (arXiv:2005.11401)](https://arxiv.org/abs/2005.11401)
  RAG 原始论文：参数化记忆 + 非参数化记忆。读摘要与第 1–2 节即可。
- [Weaviate — Hybrid Search](https://docs.weaviate.io/weaviate/search/hybrid)
  BM25 + 向量融合的工程实现，`alpha` 权重与 ranked fusion。第 3 课 hybrid 检索的对照。
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
  多 Agent 的真实工程复盘：orchestrator-worker、token 成本、评测与失败模式。第 2 周选读。
- [ReAct: Synergizing Reasoning and Acting in Language Models (arXiv:2210.03629)](https://arxiv.org/abs/2210.03629)
  ReAct 循环的原始论文。读第 1–2 节即可建立直觉。**第 1 课选读**。
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
- [LangGraph v1 文档](https://docs.langchain.com/oss/python/langgraph/overview)
  状态图、reducer、checkpoint、interrupt、durable execution。**第 2 周主框架**。
- [Model Context Protocol — 2026-07-28 规范](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
  最新 MCP：无状态核心、`server/discover`、extensions、OAuth 加固。第 2 周精读 changelog。
- [MCP — Getting Started](https://modelcontextprotocol.io/docs/getting-started/intro)
  快速搭建 server/client 的官方入口。

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
