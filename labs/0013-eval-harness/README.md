# 第 13 课 Lab：给 RepoMedic 建一套可校准的评测集

这个 lab 不调用模型，也不依赖任何第三方包。它只做一件事：
把“我的 Agent 看起来不错”拆成 **数据集 + rubric + grader + judge 校准 + 报告**，
并让每一步都能被测试和复现。

核心主张：

> 一个没有置信区间、没有切片、没有校准过的 judge 的分数，不是证据，只是噪声。

## 文件

| 文件 | 作用 |
|---|---|
| `data/cases.jsonl` | 40 个 RepoMedic 事故案例：32 个可回答 + 8 个应拒答；8 类根因各 4 个 |
| `data/runs/baseline.jsonl` | 录制好的 baseline Agent 输出，20/40 通过 |
| `data/runs/candidate.jsonl` | 录制好的 candidate Agent 输出，32/40 通过，但仍有 7 个回归 |
| `data/judge_calibration.jsonl` | 30 个人工标注样本：24 pass + 6 fail，用来校准 judge |
| `data/pairwise.jsonl` | 12 组 A/B 对照，用来观察位置偏差 |
| `schema.py` | `Case` / `Answer` / `Citation` / `Action` 的数据契约 |
| `dataset.py` | 数据集加载与审计：标签平衡、泄漏、重复、证据引用 |
| `graders.py` | 确定性 grader：根因、证据、引用、禁用表述、动作安全、置信度 |
| `judge.py` | `RubricJudge`（离线、确定性）与 `LLMJudge`（OpenAI-compatible，可选） |
| `calibration.py` | 混淆矩阵、TPR/TNR、raw agreement、Cohen's kappa |
| `stats.py` | Wilson 置信区间、pass^k、pass@k |
| `harness.py` | 跑一条 run、按 capability/regression/label/difficulty 切片、对比两条 run |
| `bias.py` | 位置偏差与长度偏差的确定性模拟，以及 swap 协议 |
| `eval_cli.py` | 命令行：`audit / run / compare / calibrate / bias / demo` |
| `test_eval.py` | 60 个离线测试，不需要 API key、网络或 sleep |

## 环境

Python ≥ 3.9，仅标准库。本 lab 在 Python 3.12.16 上跑通 60 个测试；
源码另用 `ast.parse(..., feature_version=(3, 9))` 检查过 3.9 语法兼容性。

```bash
cd labs/0013-eval-harness
python3 --version
python3 -m unittest test_eval -v
```

应该看到：

```
Ran 60 tests in 0.07s

OK
```

## 快速开始

### 1. 审计数据集

```bash
python3 eval_cli.py audit
```

输出：

```
cases: 40
answerable: 32 · abstain: 8
split: capability 28 · regression 12
difficulty: easy 4 · hard 18 · medium 18
labels: cache_stampede 4 · config_drift 4 · connection_pool_exhaustion 4 · dependency_conflict 4 · disk_full 4 · gc_pause 4 · n_plus_one_query 4 · thread_deadlock 4

dataset audit: OK
```

审计会抓这些错误：重复 ID、重复 incident、required evidence 不存在、
abstain 案例却写了根因、incident 里直接泄漏根因标签。
它也会警告：标签不平衡、拒答案例太少、regression 切片太小。

### 2. 对比 baseline 与 candidate

```bash
python3 eval_cli.py compare
```

重点不是 `80% > 50%`，而是三行：

```
overall                            50.0%       80.0%    +30.0 pp
capability                         35.7%       78.6%    +42.9 pp
regression                         83.3%       83.3%     +0.0 pp
```

以及最后的：

```
fixed: E01, E02, E03, ...
regressed: E04, E09, E11, E17, E24, E28, E35
```

candidate 总体提升了 30 个百分点，但它**在 7 个 case 上比 baseline 更差**。
如果只看总分，你会把回归带上线。这就是为什么评测必须切片、必须看 diff。

### 3. 校准 judge

```bash
python3 eval_cli.py calibrate --judge rubric
```

输出：

```
calibration set: 30 examples · human fail: 6 · judge UNKNOWN: 0

judge
  TP 4 (fail caught) · TN 22 (pass kept) · FP 2 (false alarm) · FN 2 (failure missed)
  TPR (catch failures): 66.7%
  TNR (keep passes):    91.7%
  raw agreement:        86.7%
  precision:            66.7%
  Cohen's kappa:        0.583

always-pass baseline
  TP 0 (fail caught) · TN 24 (pass kept) · FP 0 (false alarm) · FN 6 (failure missed)
  TPR (catch failures): 0.0%
  TNR (keep passes):    100.0%
  raw agreement:        80.0%
  precision:            0.0%
  Cohen's kappa:        0.000
```

两个关键读数：

1. 一个**永远说 pass** 的 judge 也能拿到 80% 的 raw agreement，因为它只需要
   把 24 个 pass 全部猜对；它一个失败都没抓到。raw agreement 在类别不平衡时会骗人。
2. 这个 rubric judge 的 raw agreement 是 86.7%，看起来不错，但 TPR 只有 66.7%：
   6 个真实失败里漏了 2 个。它还会把 2 个“引用是改写但结论正确”的人工 pass 判成 fail。
   这就是为什么校准要报告 TPR/TNR，而不是只报告一致率。

### 4. 看位置偏差

```bash
python3 eval_cli.py bias
```

输出：

```
pairwise bias simulation: 12 pairs
position flips (AB != BA): 8/12 (66.7%)
human agreement — AB order: 100.0% · BA order: 33.3%
consensus: 100.0% (stable on 4/12 pairs)
A win rate when first: 100.0% · when second: 33.3%
longer answer wins: 33.3% of comparisons
```

同一个 judge，只把 A/B 顺序换一下，12 组里有 8 组改判。
`consensus 100%` 只覆盖了 4 组“顺序无关”的对照；剩下的 8 组根本没有共识。
这不是模型能力问题，是评测协议问题。生产里的缓解办法是：
交换位置、取平均/要求一致、用参考结论、校准 judge。

### 5. 一次跑完

```bash
python3 eval_cli.py demo
```

## 数据格式

一个 case：

```json
{
  "id": "E01",
  "title": "order-service 连接池耗尽",
  "service": "order-service",
  "incident": "order-service p99 从 180ms 升到 8.4s ...",
  "evidence": [
    {"id": "E1", "source": "metrics/hikaricp", "text": "hikaricp_connections_active 49/50 ..."},
    {"id": "E2", "source": "logs/app", "text": "HikariPool-1 - Connection is not available ..."}
  ],
  "expected": {
    "root_cause": "connection_pool_exhaustion",
    "required_evidence": ["E1", "E2"],
    "forbidden_claims": ["部署", "GC 停顿", "网络抖动"],
    "max_tier": "T1",
    "abstain": false
  },
  "reference_answer": "order-service：根因是连接池耗尽 ...",
  "split": "capability",
  "difficulty": "easy",
  "tags": ["db", "pool"]
}
```

一条 Agent 输出：

```json
{
  "case_id": "E01",
  "root_cause": "connection_pool_exhaustion",
  "confidence": 0.82,
  "evidence": [
    {"evidence_id": "E1", "quote": "hikaricp_connections_active 49/50"},
    {"evidence_id": "E2", "quote": "HikariPool-1 - Connection is not available"}
  ],
  "actions": [{"tool": "query_logs", "tier": "T0"}],
  "abstain": false,
  "answer": "order-service：根因是连接池耗尽。..."
}
```

`quote` 必须是证据原文的子串（忽略空白与大小写）。改写不是引用。
`actions` 会按 `TOOL_TIERS` 重新计算 canonical tier；未知工具默认 T4，
声称 T0 但工具实际是 T2 的“降级伪装”会被抓住。

## 换你自己的 run

把 Agent 输出按上面的格式写成 JSONL，每个 case 一行：

```bash
python3 eval_cli.py run --run /path/to/my-agent.jsonl
```

如果缺少某个 case，它会计为 schema error，而不是静默跳过。
如果输出缺字段、`confidence` 不是数字、`root_cause` 不在枚举里，也会计为 schema error。
这正是评测器该有的 fail-closed 行为。

## 实验

### 实验 1：故意改坏一个 grader

把 `graders.py` 的 `grade_evidence` 改成“只检查引用了哪些 ID，不检查 quote 是否在原文里”。
再跑：

```bash
python3 eval_cli.py run --run candidate
python3 eval_cli.py calibrate --judge rubric
```

看 TPR/TNR 怎么变。然后问自己：这个放宽，是让评测更接近人类判断，还是只是让分数更好看？

### 实验 2：加一个泄漏案例

复制 E01，改成一个新 ID，把 `incident` 改成“order-service 连接池耗尽，p99 升高”。
再跑 `audit`。它应该报 `incident contains the root-cause label`。
如果它没报，说明你的泄漏检查不够严；如果它报了，说明数据集审计在保护你。

### 实验 3：改变校准集的类别比例

把 `judge_calibration.jsonl` 里的 6 个 fail 删掉 3 个，再跑 `calibrate`。
观察 raw agreement 和 TPR 怎么变。一个 judge 的“准确率”会随着失败样本比例变化，
所以校准集要覆盖真实的失败分布，不能只放容易的 pass。

### 实验 4：接真实 LLM judge

```bash
cp .env.example .env
# 填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

python3 eval_cli.py calibrate --judge llm
```

`LLMJudge` 对每个维度单独调用一次，要求先写 critique 再给 verdict，
并在证据不足时返回 UNKNOWN。它使用标准库 `urllib`，不需要 `openai` 包。
将 `LLMJudge` 的判决与人工标签比较，看看它的 TPR/TNR 是否比 `RubricJudge` 更好。
如果没有 API key，跳过这一步；lab 的其余部分全部离线可跑。

### 实验 5：把 pass^3 算进报告

`harness.py` 已经算了 `pass^3 = pass_rate ** 3`。拿 candidate 的 80% 算：
`0.8^3 = 51.2%`。如果这个 Agent 面向用户，51.2% 的“三次全对”意味着什么？
再拿 75% 算：`0.75^3 = 42.2%`。这就是为什么单次 75% 不能直接叫“可靠”。

### 实验 6：给 judge 加一个维度

在 `judge.py` 的 `RUBRIC` 里加一个 `actionability` 维度：
“下一步必须能在 5 分钟内执行，且是只读的。”
在 `RubricJudge.evaluate` 里实现它，并在 `DIMENSIONS` 注册。
然后重新校准。注意：每加一个维度，整体 PASS 的门槛就更高；
你要确认这个维度真的与人类判断一致，而不是制造更多 false alarm。

## 设计说明

- **为什么用录制 run 而不是现场调模型？** 评测器本身要先被测试。fixtures 让 grader、
  校验、报告和校准可以在没有 API key、没有网络、没有随机性的情况下被验证。
  这些输出是**录制/合成**的，只能证明评测器工作，不能证明模型能力。
  真实结论必须用 `LLMJudge` 或你自己的 Agent run。
- **为什么一开始用确定性 grader？** 它们便宜、可复现、可调试。先让 schema、引用、
  动作安全这些硬约束由代码判定，再把“清晰度、证据充分性”这类开放判断交给 judge。
- **为什么先写 critique 再给 verdict？** 这迫使 judge 把理由说出来，便于人类发现
  它是在看证据，还是在看长度、位置或语气。`Unknown` 是为了防止证据不足时编一个 verdict。
- **为什么每个维度单独调用？** 一个 judge 同时评“根因、证据、安全、清晰度”，
  很容易让一个高分维度稀释一个硬失败。安全失败必须是一票否决，不是平均分。
- **为什么报告里保留 soft flag？** 置信度、长度、过度自信不是硬失败，但它们是
  失败分析的第一现场。把它们单独列出来，避免它们悄悄混进总分。
- **`forbidden_claims` 是粗粒度的词面检查。** 它不懂否定（“这不是代码 bug”也会命中“代码 bug”），
  也不懂同义改写。这正是为什么还需要 judge 与人工抽样；代码 grader 负责快速抓住明显问题，
  不负责语义判断。

## 和 Java 后端的对照

| 评测概念 | Java 后端对应 |
|---|---|
| `cases.jsonl` | 回归测试用例 / golden master |
| `graders.py` | Assertion / JSON Schema validator / 契约测试 |
| `reference_answer` | 验收标准 / oracle |
| `RubricJudge` | 人工验收规则的自动化代理 |
| `calibration.py` | 分类器评估：混淆矩阵、TPR/TNR、kappa |
| `pass/fail` | 测试通过/失败 |
| `capability / regression` | 新功能测试 / 回归测试 |
| `Wilson CI` | A/B 实验的置信区间 |
| `pass^k` | 连续 k 次运行全部通过的可靠性 |
| `--fail-under` | CI 质量门禁 |
| `data/runs/*.jsonl` | 测试报告 / 构建产物 |

## 一手资料

- Anthropic — Demystifying evals for AI agents:
  <https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents>
- Hamel Husain — Your AI Product Needs Evals:
  <https://hamel.dev/blog/posts/evals/>
- Hamel Husain — Creating a LLM-as-a-Judge that drives business results:
  <https://hamel.dev/blog/posts/llm-judge/>
- Zheng et al. — Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena:
  <https://arxiv.org/abs/2306.05685>
- Shankar et al. — Who Validates the Validators? (EvalGen):
  <https://arxiv.org/abs/2404.12272>
- Yao et al. — τ-bench (pass^k):
  <https://arxiv.org/abs/2406.12045>
- Kapoor et al. — AI Agents That Matter:
  <https://arxiv.org/abs/2407.01502>
