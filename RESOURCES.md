# AI Agent 开发 Resources

> 最后核验：2026-10-23。优先一手资料；中文材料仅作术语对照与补充。
> 使用方式：每节课会指定「本课精读」；其余条目按需查阅，不必通读。

## Knowledge

### 第一性原理与架构

- [Anthropic — Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents)
  最重要的起点。区分 workflow 与 agent，给出 5 种 workflow 模式、agent 的适用边界、ACI 工具设计原则。**第 1 周精读**。
- [Anthropic — Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  把上下文当预算：context rot、system prompt 的「正确高度」、compaction、结构化笔记、sub-agent。**第 2 周精读**。
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
