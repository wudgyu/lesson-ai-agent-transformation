"""Offline tests for lesson 0006. No API key required.

    python3 -m unittest -v

中文说明：本文件把 lab 的核心行为固定成可执行规格，共 5 组测试：

- RerankTests: 重排的特征、基线、截断与错误处理；
- ParsingTests: 模型输出的 JSON 解析容错；
- CitationCheckTests: 引用 ID / quote / 覆盖率的校验规则；
- GenerateTests: 来源编号、prompt 契约与抽取式后端；
- DataTests: cases.json 的字段与语料一致性。

测试全部离线运行，不调用 LLM；`llm` 后端只在真实评测时手动使用。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from citation import (  # 引用协议：解析、校验、召回、拒答识别
    Citation,
    GroundedAnswer,
    Source,
    answer_contains_facts,
    check_citations,
    citation_recall,
    forbidden_hits,
    is_abstention,
    normalize_source_id,
    parse_grounded_answer,
)
from common import BM25Index, Chunk, DEFAULT_CORPUS  # 复用第 5 课的索引与语料
from generate import build_prompt, build_sources, extractive_answer, generate_answer
from rerank import _jaccard, rerank_candidates  # 被测的重排实现

# 测试数据文件所在目录；用 __file__ 定位，保证从任意 cwd 运行都正确。
ROOT = Path(__file__).resolve().parent


def make_chunk(chunk_id: str, doc_id: str, text: str, heading: str = "H") -> Chunk:
    """构造测试用 Chunk；行号固定为 1，测试不关心真实位置。"""
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        heading=heading,
        text=text,
        start_line=1,
        end_line=1,
    )


def make_source(source_id: str, doc_id: str, text: str) -> Source:
    """构造测试用 Source；heading 固定为 "H"，引用校验不依赖它。"""
    return Source(source_id=source_id, doc_id=doc_id, heading="H", text=text)


class RerankTests(unittest.TestCase):
    """用手工构造的小语料隔离验证重排逻辑。

    每个用例只改变一个信号（标识符、标题、基线顺序、top_n、非法 method），
    这样失败时能直接定位到对应特征。
    """

    def setUp(self):
        # 三条 chunk 分别代表通用文档、代码、runbook，覆盖 _intent_bonus 的三类先验。
        self.chunks = [
            make_chunk("c1", "docs/generic.md", "数据库连接池的一般介绍。"),
            make_chunk(
                "c2",
                "code/OrderQueryService.java",
                "findRecentOrders 使用循环查询，形成 N+1。",
                "OrderQueryService",
            ),
            make_chunk(
                "c3",
                "runbooks/deploy.md",
                "回滚需要 on-call 负责人审批。",
                "Runbook: 部署与回滚 > 注意",
            ),
        ]
        self.by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        # IDF 直接从这三条测试 chunk 构建，保证测试自包含、不依赖外部索引。
        self.idf = BM25Index(self.chunks).idf

    def test_exact_identifier_ranks_first(self):
        # c1 的第一阶段分远高于 c2（10 vs 1），但 query 含 camelCase 标识符，
        # 且 c2 正文精确包含它；重排应让 c2 反超，证明二阶段能纠正一阶段排序。
        candidates = [("c1", 10.0), ("c2", 1.0)]
        ranked = rerank_candidates(
            "findRecentOrders 有什么问题？",
            candidates,
            self.by_id,
            self.idf,
            top_n=2,
            method="heuristic",
        )
        self.assertEqual(ranked[0].chunk_id, "c2")

    def test_heading_match_boosts(self):
        # c1 第一阶段分更高，但 c3 的标题含“回滚”、正文含“审批”，
        # 覆盖率 + 标题命中 + runbook 意图先验叠加后应排到第一。
        candidates = [("c1", 5.0), ("c3", 1.0)]
        ranked = rerank_candidates(
            "回滚 审批",
            candidates,
            self.by_id,
            self.idf,
            top_n=2,
            method="heuristic",
        )
        self.assertEqual(ranked[0].chunk_id, "c3")

    def test_none_keeps_first_stage_order(self):
        # method="none" 是消融基线：即使 c2 在启发式下会赢，也必须保持原始顺序。
        candidates = [("c1", 10.0), ("c2", 1.0)]
        ranked = rerank_candidates(
            "findRecentOrders", candidates, self.by_id, self.idf, top_n=2, method="none"
        )
        self.assertEqual([item.chunk_id for item in ranked], ["c1", "c2"])

    def test_top_n_limit(self):
        # top_n 是最终输出上限；即使有 3 个候选，也只返回 1 个。
        candidates = [("c1", 3.0), ("c2", 2.0), ("c3", 1.0)]
        ranked = rerank_candidates(
            "连接池", candidates, self.by_id, self.idf, top_n=1, method="heuristic"
        )
        self.assertEqual(len(ranked), 1)

    def test_unknown_reranker_raises(self):
        # 拼错 method 应立即报错，避免评测静默跑错分支。
        with self.assertRaises(ValueError):
            rerank_candidates("x", [("c1", 1.0)], self.by_id, self.idf, method="magic")

    def test_jaccard(self):
        # 完全相同 -> 1.0；没有任何共同 token -> 0.0。
        self.assertEqual(_jaccard("连接池耗尽", "连接池耗尽"), 1.0)
        self.assertEqual(_jaccard("连接池", "缓存击穿"), 0.0)


class ParsingTests(unittest.TestCase):
    """验证 parse_grounded_answer 对真实模型常见输出格式的容错。"""

    def test_parse_plain_json(self):
        # 标准契约：纯 JSON，answer 中含 [S1]，citations 数组中有一条引用。
        raw = '{"answer": "下游超时 [S1]。", "citations": [{"source_id": "S1", "quote": "TS-999 表示下游超时。"}]}'
        grounded = parse_grounded_answer(raw)
        self.assertIn("[S1]", grounded.answer)
        self.assertEqual(grounded.citations[0].source_id, "S1")

    def test_parse_fenced_json(self):
        # 模型常把 JSON 包在 Markdown 代码块里，解析器应剥离围栏。
        raw = '```json\n{"answer": "a [S1]", "citations": []}\n```'
        grounded = parse_grounded_answer(raw)
        self.assertEqual(grounded.answer, "a [S1]")

    def test_parse_json_with_prose(self):
        # 模型可能在 JSON 前后加解释文字，解析器应提取第一个完整 JSON 对象。
        raw = '结果如下：\n{"answer": "a [S1]", "citations": []}\n以上。'
        grounded = parse_grounded_answer(raw)
        self.assertEqual(grounded.answer, "a [S1]")

    def test_parse_invalid_falls_back(self):
        # 完全无法解析时不能抛异常：退化为纯文本答案 + 空引用列表。
        grounded = parse_grounded_answer("just text [S1]")
        self.assertEqual(grounded.answer, "just text [S1]")
        self.assertEqual(grounded.citations, [])

    def test_normalize_source_id(self):
        # 宽松接受 "1" / "s2" 等写法，统一成 "S1" / "S2"。
        self.assertEqual(normalize_source_id("1"), "S1")
        self.assertEqual(normalize_source_id("s2"), "S2")


class CitationCheckTests(unittest.TestCase):
    """验证引用校验的四个维度：ID 有效、quote 存在、结论被引用、拒答合法。"""

    def setUp(self):
        # S1 是错误码文档，S2 是 runbook；后续用例都基于这两个来源。
        self.sources = [
            make_source("S1", "docs/error-codes.md", "TS-999 表示下游超时。"),
            make_source("S2", "runbooks/db-pool.md", "先扩容副本，再修慢查询。"),
        ]

    def test_valid_citation_passes(self):
        # 引用 ID 存在且 quote 是来源原句 -> 通过，precision/coverage 均为 1。
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。",
            citations=[Citation("S1", "TS-999 表示下游超时。")],
        )
        result = check_citations(answer, self.sources)
        self.assertTrue(result["valid"])
        self.assertEqual(result["citation_precision"], 1.0)
        self.assertEqual(result["citation_coverage"], 1.0)

    def test_invalid_source_id(self):
        # answer 与 citations 都出现 S9，但来源列表里没有 -> invalid_ids，整体不通过。
        answer = GroundedAnswer(
            answer="答案是 42 [S9]。",
            citations=[Citation("S9", "答案是 42")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(result["invalid_ids"], ["S9"])

    def test_quote_not_found(self):
        # ID 合法，但 quote 被改写成来源中不存在的内容 -> quote_not_found。
        answer = GroundedAnswer(
            answer="TS-999 表示数据库崩溃 [S1]。",
            citations=[Citation("S1", "TS-999 表示数据库崩溃。")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["quote_not_found"]), 1)

    def test_missing_quote(self):
        # 引用对象缺少 quote 字段 -> missing_quote（与“quote 不匹配”分开统计）。
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。",
            citations=[Citation("S1", "")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["missing_quote"]), 1)

    def test_uncited_claim_detected(self):
        # 第二句没有 [Sx]，其文本也不被任何 quote 覆盖 -> uncited_claims。
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。另外数据库也坏了。",
            citations=[Citation("S1", "TS-999 表示下游超时。")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["uncited_claims"]), 1)

    def test_citation_precision(self):
        # 两条引用只有一条有效，citation_precision = 1/2。
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1][S2]。",
            citations=[
                Citation("S1", "TS-999 表示下游超时。"),
                Citation("S2", "不存在的句子"),
            ],
        )
        result = check_citations(answer, self.sources)
        self.assertEqual(result["citation_precision"], 0.5)

    def test_citation_recall(self):
        # 期望两个文档都被引用，实际只有效引用了 error-codes，
        # 因此 recall = 1/2，并报告缺失的 runbook 文档。
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。",
            citations=[Citation("S1", "TS-999 表示下游超时。")],
        )
        recall, missing = citation_recall(
            ["docs/error-codes.md", "runbooks/db-pool.md"], answer, self.sources
        )
        self.assertEqual(recall, 0.5)
        self.assertEqual(missing, ["runbooks/db-pool.md"])

    def test_abstention(self):
        # 拒答话术应被识别出来；带引用的正常答案不应误判。
        self.assertTrue(is_abstention("证据不足：没有找到相关来源。"))
        self.assertFalse(is_abstention("TS-999 表示下游超时 [S1]。"))

    def test_abstention_answer_is_valid(self):
        # 拒答句以“证据不足”开头，被 _claims 当作元信息跳过；
        # 因此空 citations、没有事实结论的拒答也应通过校验。
        answer = GroundedAnswer(answer="证据不足：没有找到相关来源。", citations=[])
        result = check_citations(answer, self.sources)
        self.assertTrue(result["valid"])

    def test_facts_and_forbidden(self):
        # expected_facts 用大小写不敏感的子串包含判断；
        # forbidden_hits 返回命中的禁止表述。
        ok, missing = answer_contains_facts("答案包含 N+1。", ["N+1"])
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(forbidden_hits("一定是 N+1", ["一定是 N+1"]), ["一定是 N+1"])


class GenerateTests(unittest.TestCase):
    """验证“来源编号 -> prompt 契约 -> 抽取式回答”这条离线链路。"""

    def setUp(self):
        self.chunks = [
            make_chunk("c1", "docs/error-codes.md", "TS-999 表示下游超时。"),
            make_chunk("c2", "runbooks/db-pool.md", "先扩容副本，再修慢查询。"),
        ]

    def test_build_sources_ids(self):
        # 来源编号按输入顺序生成 S1..Sn，并保留原始 doc_id 供评测匹配。
        sources = build_sources(self.chunks)
        self.assertEqual([source.source_id for source in sources], ["S1", "S2"])
        self.assertEqual(sources[0].doc_id, "docs/error-codes.md")

    def test_prompt_contains_sources_and_json_contract(self):
        # prompt 必须列出所有来源编号、声明 JSON 契约，并给出“证据不足”的拒答出口。
        prompt = build_prompt("TS-999 是什么？", build_sources(self.chunks))
        self.assertIn("[S1]", prompt)
        self.assertIn("[S2]", prompt)
        self.assertIn('"citations"', prompt)
        self.assertIn("证据不足", prompt)

    def test_extractive_answer_cites_source(self):
        # 抽取式后端选出与 query 有重叠的 span，并在答案末尾附上 [S1]。
        grounded = extractive_answer(
            "TS-999 是什么错误？", build_sources(self.chunks), min_overlap=1
        )
        self.assertIn("[S1]", grounded.answer)
        self.assertEqual(grounded.citations[0].source_id, "S1")
        self.assertIn("下游超时", grounded.answer)

    def test_extractive_abstains_on_no_overlap(self):
        # query 与来源没有足够重叠（min_overlap=2）时，应明确拒答且不产生引用。
        grounded = extractive_answer(
            "Kubernetes 内核参数怎么改？", build_sources(self.chunks), min_overlap=2
        )
        self.assertTrue(is_abstention(grounded.answer))
        self.assertEqual(grounded.citations, [])

    def test_generate_unknown_backend(self):
        # 未知后端名应报错，避免悄悄回退到某个默认实现。
        with self.assertRaises(ValueError):
            generate_answer("x", build_sources(self.chunks), backend="magic")


class DataTests(unittest.TestCase):
    """校验 cases.json 的结构完整性，以及它与第 5 课语料的一致性。"""

    def test_cases_have_required_fields(self):
        # 每个用例都必须带评估所需字段，否则 eval.py 会在运行中途 KeyError。
        cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
        for case in cases:
            for key in (
                "id",
                "query",
                "answerable",
                "expected_facts",
                "expected_sources",
                "must_not_include",
            ):
                self.assertIn(key, case, case.get("id"))

    def test_expected_sources_exist_in_corpus(self):
        # expected_sources 必须指向真实存在的 doc_id，
        # 避免用例“考”一个语料里根本没有的文档。
        from common import load_documents

        docs = {doc.doc_id for doc in load_documents(DEFAULT_CORPUS)}
        cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
        for case in cases:
            for doc_id in case["expected_sources"]:
                self.assertIn(doc_id, docs, case["id"])


if __name__ == "__main__":
    unittest.main()
