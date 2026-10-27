# 第 6 课 Lab：重排 + 引用

第 5 课解决了“找得到”；这一课解决“找得准、说得清、查得动”。

Lab 复用第 5 课的语料与索引，做三件事：

1. **Rerank**：第一阶段召回 15 个候选，第二阶段重排到 top-5；比较 recall、MRR、上下文成本。
2. **引用协议**：把每个 chunk 变成 `[S1]` 这样的来源，要求模型只依据来源回答，并给出原文 quote。
3. **引用校验**：检查来源编号是否存在、quote 是否真在来源里、每个结论是否都有引用；再用答案事实与引用召回评估整条链路。

## 文件

| 文件 | 作用 |
|---|---|
| `common.py` | 复用第 5 课的语料、chunker、BM25、hybrid；必要时自动建索引 |
| `rerank.py` | 启发式二阶段重排：term coverage、heading、first-stage score、意图先验、MMR |
| `rerank_eval.py` | 用第 5 课的 20 个检索用例比较 first-5 vs rerank-5 vs ceiling |
| `citation.py` | 引用解析、ID/quote/coverage 校验、citation recall、拒答识别 |
| `generate.py` | 构建来源与 prompt；`fixtures` / `extractive` / `llm` 三种后端 |
| `eval.py` | 11 个答案级用例：检索 → 重排 → 生成 → 引用校验 → 指标 |
| `cases.json` | 9 个可回答 + 2 个应拒答的用例 |
| `fixtures.json` | 脚本化模型输出，其中 5 个故意写坏，用来验证 checker |
| `test_rerank_citations.py` | 28 个离线测试，不需要 API key |

## 快速开始（无需 API key）

```bash
cd labs/0006-rerank-citations

# 1) 测试
python3 -m unittest -v

# 2) 校验 11 个答案用例
python3 eval.py --validate

# 3) 重排：first-5 vs rerank-5 vs 候选集上限
python3 rerank_eval.py --retriever bm25

# 4) 引用校验：脚本化输出（含故意写坏的样本）
python3 eval.py --backend fixtures
```

第 3 步会看到类似：

```
retriever  mode          recall@k   full@k    mrr@k    prec  ctx_chars
none       none-5           0.625    0.550    0.552   0.250       1033
heuristic  heuristic-5      0.725    0.700    0.627   0.270       1174
ceiling    ceiling@15       0.875    0.850    0.576   0.156       2598
```

rerank 把 recall@5 从 0.625 提到 0.725，但**不可能超过候选集上限 0.875**。
如果第一阶段没召回，重排无法凭空造出证据。

第 4 步会看到类似：

```
answer accuracy      : 0.889  (9 answerable)
grounded rate        : 0.667
citation precision   : 0.889
citation recall      : 0.833
abstention accuracy  : 1.000  (2 unanswerable)
pass rate            : 0.545
```

失败是**故意设计**的：fixtures 里有 5 个写坏的输出，分别对应
“quote 不在来源里”“引用编号不存在”“有结论没引用”“少引一个来源”“引用存在但不支持结论”。
先看失败列表，再对照 `citation.py` 的检查逻辑。

## 接真实模型

`fixtures` 只验证 checker，不代表模型能力。要测真实生成：

```bash
cp .env.example .env
# 填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

python3 eval.py --backend llm
```

模型会被要求只输出：

```json
{"answer": "... [S1]", "citations": [{"source_id": "S1", "quote": "原文句子"}]}
```

几个常见失败：

- 模型输出 Markdown 代码块：解析器会尝试剥离。
- quote 是改写而不是原文：`quote_not_found`，这是**有意的严格**。
- 多来源问题只引一个来源：`citation_recall` 下降。
- 来源不足时仍然作答：`is_abstention` 为 false，`abstention_accuracy` 下降。

`--backend extractive` 是纯离线 smoke test：它从来源里抽一段文字并附引用，
不是 LLM，不要用它的答案质量评价模型。

## 指标

| 指标 | 回答的问题 |
|---|---|
| `answer accuracy` | 可回答用例里，期望事实是否都出现、禁止表述是否没出现 |
| `grounded rate` | 引用 ID 有效、quote 在来源里、每个结论都有引用（或被 quote 覆盖） |
| `citation precision` | 所有 citation 中，ID 有效且 quote 可验证的比例 |
| `citation recall` | 期望来源中，被有效引用的比例；多来源用例要求全部 |
| `abstention accuracy` | 不可回答用例中，是否正确说“证据不足” |
| `pass rate` | 答案事实 + 引用校验 + 来源召回全部通过的比例 |

## 实验

### 实验 1：重排到底有没有用

```bash
python3 rerank_eval.py --retriever bm25 --method none
python3 rerank_eval.py --retriever bm25 --method heuristic
python3 rerank_eval.py --retriever hybrid
```

比较 recall、MRR 与 `ctx_chars`。再看失败列表：哪些是第一阶段就没召回（重排救不了），
哪些是候选里有但排太低（重排能救）。

### 实验 2：候选集大小是重排的天花板

```bash
python3 rerank_eval.py --retriever bm25 --candidate-k 5
python3 rerank_eval.py --retriever bm25 --candidate-k 15
python3 rerank_eval.py --retriever bm25 --candidate-k 30
```

`ceiling@k` 会告诉你第一阶段最多能提供多少 recall。重排只能在这个上限内做分配。

### 实验 3：故意改坏一个引用

打开 `fixtures.json`，把 A01 的 quote 改成一句来源里不存在的话，再跑：

```bash
python3 eval.py --backend fixtures --case A01
```

观察 `quote_not_found`。然后把 `source_id` 改成 `S9`，观察 `invalid_ids`。
最后把答案第二句的 `[S1]` 删掉，观察 `uncited_claims`。

### 实验 4：加一个应拒答用例

在 `cases.json` 里加一条语料无法回答的问题，`answerable: false`；
在 `fixtures.json` 里给它一个正确的拒答。跑 `--validate` 和 `eval.py --backend fixtures`。
再把它改成“硬答一个不存在的结论”，看 `abstention_accuracy` 如何变化。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `Index not found` / 找不到第 5 课语料 | 第 5 课 lab 不在旁边 | 保持 `labs/0005-rag-retrieval/` 完整；`common.py` 会自动建索引 |
| `LLM_API_KEY is not set` | 用了 `--backend llm` 但没配 `.env` | 填 `.env`，或改用 `fixtures` |
| 所有 quote 都 `quote_not_found` | 模型在改写，不是引用原文 | 强化 prompt 的“quote 必须是原句”；或接受更宽松的 NLI 校验 |
| `citation recall` 很低 | 多来源问题只引了一个来源 | 检查 rerank 候选是否包含第二个来源；必要时加大 `--candidate-k` |
| `grounded rate` 高但 `answer accuracy` 低 | quote 存在，但不支持结论 | 这是 quote containment 的盲区，需要 NLI / LLM judge / 人工审计 |

## 下一课

第 7 课进入状态与记忆：当会话变长、任务跨轮次，如何用 checkpoint、compaction 与结构化笔记，
让 Agent 记住该记的、忘掉该忘的。
