# 第 5 课 Lab：RAG 检索层

RepoMedic 的知识库里不只有 runbook，还有事故复盘、Java 代码、配置和错误码。
这一课不接生成模型，只做 RAG 的前半段：**把对的材料找出来，并且证明你找得到。**

Lab 用 14 篇文档、43 个 structure chunk、20 个带证据标签的查询，比较：

- `bm25`：关键词检索，精确错误码/类名/配置键的主场；
- `vector(lexical)`：离线占位的词面向量，用来验证索引/融合/评测管线；
- `vector(dense:...)`：真实 embedding（需要 OpenAI-compatible `/v1/embeddings`）；
- `hybrid`：BM25 + 向量，各自召回后用 RRF 融合。

## 文件

| 文件 | 作用 |
|---|---|
| `rag.py` | chunker、tokenizer、BM25、向量索引、RRF、指标函数 |
| `index.py` | 构建索引：`structure` 或 `naive`；可选写入 embedding |
| `eval.py` | 20 例检索评测；`--validate` 检查标签是否过期 |
| `eval_cases.json` | 查询、类型切片、期望证据（doc + contains） |
| `test_retrieval.py` | 29 个离线测试，不需要 API key |
| `embeddings.py` | 可选的 OpenAI-compatible embedding 适配层 |
| `corpus/` | RepoMedic 知识库：runbook、事故复盘、代码、配置、错误码 |
| `index/` | 构建产物（已加入 .gitignore） |
| `reports/` | 评测报告（已加入 .gitignore） |

## 快速开始（离线，无需 API key）

```bash
cd labs/0005-rag-retrieval

# 1) 先跑测试
python3 -m unittest -v

# 2) 校验 20 个用例：证据文档存在、证据短语没有被改坏
python3 eval.py --validate

# 3) 构建 structure-aware 索引
python3 index.py --chunker structure --out index/structure.json

# 4) 一次跑三种检索器
python3 eval.py --index index/structure.json --retriever all
```

你会看到类似：

```
retriever                       recall@k   full@k    mrr@k    prec  ctx_chars
bm25                               0.625    0.550    0.552   0.250       1033
vector(lexical)                    0.650    0.600    0.499   0.250       1100
hybrid(lexical)                    0.650    0.600    0.506   0.260       1115
```

**不要只看 recall。** `ctx_chars` 是每次查询平均塞进上下文的字符数；
`prec` 是 top-k 里真正命中证据的 chunk 比例。检索质量是召回、精度、上下文成本的三角。

## 指标怎么读

| 指标 | 回答的问题 |
|---|---|
| `recall@k` | 该找到的证据，有多少进了 top-k？多证据问题会按比例计分 |
| `full@k` | 所有证据都进 top-k 的用例比例（最严格） |
| `mrr@k` | 第一条命中证据排在第几？第 1 名 = 1.0，第 2 名 = 0.5 |
| `prec` | top-k 里命中证据的 chunk 占比；低 prec 意味着上下文里噪音多 |
| `ctx_chars` | 平均每次查询返回多少字符；用 token 估算时可直接除以 3–4 |

## 实验 1：structure vs naive，别只看平均分

```bash
python3 index.py --chunker naive --size 600 --overlap 80 --out index/naive-600.json
python3 eval.py --index index/naive-600.json --retriever bm25
```

在 k=5 时，naive-600 的 recall 更高（约 0.80 vs 0.625），但 `ctx_chars` 也接近翻倍（约 2200 vs 1030）。
它靠“每块塞更多字”提高命中率，不是靠更聪明的检索。

再把 structure 的 k 从 5 调到 10：

```bash
python3 eval.py --index index/structure.json --retriever bm25 --k 10
```

观察 recall 与 `ctx_chars` 如何一起上升。在这里，structure k=10 的 recall 会反超 naive-600，
而 `ctx_chars` 仍更低。**在相近上下文预算下比较**，才是公平的比较。

## 实验 2：失败分析，而不是调参

`eval.py` 会打印失败用例的 top-k 和缺失证据。挑 2 个回答：

1. 失败是因为 **chunk 边界**（证据被拆开）、**tokenizer**（中文/代码没切对）、**检索器**（BM25 不懂同义词），还是 **标签**（证据写错了）？
2. 如果只把 k 调大，能救回它吗？代价是什么？

## 实验 3：真实 embedding

`vector(lexical)` 只捕捉词面重合，不是语义检索。要验证语义召回，需要一个支持 `/v1/embeddings` 的端点：

```bash
cp .env.example .env
# 填写 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL

python3 index.py --chunker structure --embed-api --out index/structure-embedded.json
python3 eval.py --index index/structure-embedded.json --retriever all
```

重点看 `semantic` 切片是否上升，以及 `hybrid(dense:...)` 是否比单路更稳。
如果供应商只提供 chat、不提供 embedding，离线 BM25 仍然能完成本 lab 的核心训练；
不要把 `vector(lexical)` 的结果当成“向量检索没用”的证据。

## 实验 4：加一个你自己的查询

在 `eval_cases.json` 里加一条，例如：

```json
{
  "id": "Q21",
  "type": "semantic",
  "query": "连接池满了但数据库 CPU 不高，应该怀疑什么？",
  "evidence": [{"doc": "runbooks/db-pool.md", "contains": "连接泄漏"}]
}
```

先跑 `python3 eval.py --validate`，再跑评测。如果标签在文档里不存在，validate 会直接报错——
这就是“评测集也是代码，会腐烂”的具体表现。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `Index not found` | 还没构建索引 | 先跑 `index.py --chunker structure ...` |
| `No embedding API key` | dense 路径缺少 key | 填 `.env`，或改用离线 `bm25` / `lexical` |
| `phrase ... not found` | 评测标签过期 | 检查文档是否改名/改词，更新 `eval_cases.json` |
| recall 很低但 top-k 都是对的文档 | chunk 粒度太细，证据被拆开 | 调大 k，或改用 parent-child / 更大 chunk |
| 中文查询几乎召不回 | 语料里中文 token 没匹配上 | 检查 tokenizer 的 CJK bigram；不要假设空格分词 |

## 下一步

第 6 课会在这条管线后面加：rerank、引用、以及“检索到了但模型还是答错”的失败模式。
这一课先把 retrieval eval 跑通——后面所有优化都要用它证明自己没有退步。
