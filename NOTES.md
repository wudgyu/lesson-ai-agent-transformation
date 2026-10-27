# NOTES

## 学习者画像

- 资深 Java 后端工程师；工程能力强，熟悉状态机、幂等、可观测性、事务边界等概念。
- 做过 RAG 或简单 Agent Demo，但自评「不成体系」。
- 4 周全职投入（30+ h/周），技术栈以 Python 为主。
- 目标：系统理解 + 真实项目；不是刷面试题。

## 教学偏好

- **中文授课**，关键术语保留英文（agent loop、trajectory、guardrail…），避免翻译造成的歧义。
- **先理论后实践**：每个概念先建立心智模型，再落到可运行的 lab；不做纯 Prompt 技巧堆砌。
- 喜欢**看得见的交付物**：每周一个可运行、可评测的产物，而不是只读文章。
- 用 Java 工程经验做锚点：状态机 ↔ StateGraph，事务/幂等 ↔ tool idempotency，日志/链路追踪 ↔ tracing，接口契约 ↔ tool schema。
- 不要过度讲解已会内容；每节课用 1–2 个问题快速校验基础，再进入深水区。

## 环境

- Linux / WSL2 (Debian)，Python 3.9.2，`openai` 1.109.1 已安装。
- 模型通过任意 OpenAI-compatible API 接入（`.env` 配置 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`）。
- 浏览器打开方式优先 `sensible-browser`；WSL 下如失败，用 Windows 侧打开路径。

## 课程决策

- 4 周、约 122 小时；理论 : 实践 ≈ 4 : 6。
- 第 1 周先修「从零写循环」，第 2 周再上 LangGraph / OpenAI Agents SDK；避免框架先行导致只会调 API。
- 评测（evaluation）不是最后一周的附加项，而是从第 1 周就埋入：每个 lab 都有小评测集。
- 默认项目：**RepoMedic**（Java 代码库排障 Agent）。第 3 周开始前确认是否沿用、换领域或接入你公司的真实代码库。
- 项目必须包含：RAG、工具、记忆、HITL、评测、tracing、部署。缺一项就不算“生产级”。

## 进度

- 2026-10-27：Lesson 0001《LLM 工程师心智模型》交付（含 `labs/0001-llm-client`：流式 client、schema 校验、修复循环，20 个离线测试）。
- 2026-10-23：Lesson 0003《Agent 循环》交付（含 `labs/0003-mini-agent`）。
- 2026-10-24：Lesson 0004《工具设计（ACI）》交付（含 `labs/0004-tool-design` 的 baseline/refactored 对照评测）。
- 2026-10-24：Lesson 0005《RAG I：检索，才是上限》交付（含 `labs/0005-rag-retrieval`：14 篇语料、20 例评测、BM25 / lexical-vector / hybrid，29 个离线测试）。
- 2026-10-25：Lesson 0006《RAG II：让答案可被核查》交付（含 `labs/0006-rerank-citations`：15→5 重排、引用协议、11 例答案级评测、28 个离线测试）。
- 2026-10-27：Lesson 0007《状态与记忆：上下文不是记忆》交付（含 `labs/0007-state-memory`：state / compaction / long-term memory / checkpoint-resume，4 个场景、23 个离线测试）。
- 尚未获得 lab 1–7 的完成证据；每节课开头都有 90 秒回忆，若答不上来先补前一课。
- 第 1 课的核心观察：模型只输出 tool_calls，不执行；temperature=0 不保证绝对确定；结构化输出必须 schema + 校验 + 修复循环。
- 第 5 课的核心观察：离线 `vector(lexical)` 只是词面向量，不能证明语义检索的收益；真实结论要等用户配置 embedding 后跑 dense/hybrid。
- 第 6 课的核心观察：`fixtures` 只验证 checker，不代表模型能力；真实答案级结论必须跑 `--backend llm`。A09 是 quote containment 的盲区，需要 NLI / LLM judge / 人工审计。
- 第 7 课的核心观察：`none`（硬截断）能答对最终问题但丢掉最初约束；compaction 能保住约束，但默认 0.70/keep4 会压缩 8 次（thrash），调到 0.90/keep2 后只压 2 次、峰值上升但最终 tokens 更低。长期记忆改变第一个工具（health → runbook）；旧记忆 40 被新证据 50 supersede；resume 后副作用不重复。
- 已修复 `reference/course-roadmap.html` 中第 3、4 课（D5/D6）的缺失链接，并补上第 6 课入口。

## 待确认

- 模型供应商与可用模型（DeepSeek / Qwen / GLM / OpenAI / 本地 Ollama）。
- **Chat 模型**：第 6 课 `--backend llm` 需要 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`；没有时只能跑 fixtures / extractive。
- **Embedding 供应商**：是否有支持 `/v1/embeddings` 的 OpenAI-compatible 端点？若没有，第 5 课 lab 的 dense 部分只能跳过。
- RepoMedic 是否作为最终项目；若换，优先选你熟悉且能拿到真实数据的领域。
- 是否希望每周固定一次「口头答辩 / 讲解」来检验 storage strength。
