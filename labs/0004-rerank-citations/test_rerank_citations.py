"""Offline tests for lesson 0004. No API key required.

    python3 -m unittest -v
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from citation import (
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
from common import BM25Index, Chunk, DEFAULT_CORPUS
from generate import build_prompt, build_sources, extractive_answer, generate_answer
from rerank import _jaccard, rerank_candidates

ROOT = Path(__file__).resolve().parent


def make_chunk(chunk_id: str, doc_id: str, text: str, heading: str = "H") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        heading=heading,
        text=text,
        start_line=1,
        end_line=1,
    )


def make_source(source_id: str, doc_id: str, text: str) -> Source:
    return Source(source_id=source_id, doc_id=doc_id, heading="H", text=text)


class RerankTests(unittest.TestCase):
    def setUp(self):
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
        self.idf = BM25Index(self.chunks).idf

    def test_exact_identifier_ranks_first(self):
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
        candidates = [("c1", 10.0), ("c2", 1.0)]
        ranked = rerank_candidates(
            "findRecentOrders", candidates, self.by_id, self.idf, top_n=2, method="none"
        )
        self.assertEqual([item.chunk_id for item in ranked], ["c1", "c2"])

    def test_top_n_limit(self):
        candidates = [("c1", 3.0), ("c2", 2.0), ("c3", 1.0)]
        ranked = rerank_candidates(
            "连接池", candidates, self.by_id, self.idf, top_n=1, method="heuristic"
        )
        self.assertEqual(len(ranked), 1)

    def test_unknown_reranker_raises(self):
        with self.assertRaises(ValueError):
            rerank_candidates("x", [("c1", 1.0)], self.by_id, self.idf, method="magic")

    def test_jaccard(self):
        self.assertEqual(_jaccard("连接池耗尽", "连接池耗尽"), 1.0)
        self.assertEqual(_jaccard("连接池", "缓存击穿"), 0.0)


class ParsingTests(unittest.TestCase):
    def test_parse_plain_json(self):
        raw = '{"answer": "下游超时 [S1]。", "citations": [{"source_id": "S1", "quote": "TS-999 表示下游超时。"}]}'
        grounded = parse_grounded_answer(raw)
        self.assertIn("[S1]", grounded.answer)
        self.assertEqual(grounded.citations[0].source_id, "S1")

    def test_parse_fenced_json(self):
        raw = '```json\n{"answer": "a [S1]", "citations": []}\n```'
        grounded = parse_grounded_answer(raw)
        self.assertEqual(grounded.answer, "a [S1]")

    def test_parse_json_with_prose(self):
        raw = '结果如下：\n{"answer": "a [S1]", "citations": []}\n以上。'
        grounded = parse_grounded_answer(raw)
        self.assertEqual(grounded.answer, "a [S1]")

    def test_parse_invalid_falls_back(self):
        grounded = parse_grounded_answer("just text [S1]")
        self.assertEqual(grounded.answer, "just text [S1]")
        self.assertEqual(grounded.citations, [])

    def test_normalize_source_id(self):
        self.assertEqual(normalize_source_id("1"), "S1")
        self.assertEqual(normalize_source_id("s2"), "S2")


class CitationCheckTests(unittest.TestCase):
    def setUp(self):
        self.sources = [
            make_source("S1", "docs/error-codes.md", "TS-999 表示下游超时。"),
            make_source("S2", "runbooks/db-pool.md", "先扩容副本，再修慢查询。"),
        ]

    def test_valid_citation_passes(self):
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。",
            citations=[Citation("S1", "TS-999 表示下游超时。")],
        )
        result = check_citations(answer, self.sources)
        self.assertTrue(result["valid"])
        self.assertEqual(result["citation_precision"], 1.0)
        self.assertEqual(result["citation_coverage"], 1.0)

    def test_invalid_source_id(self):
        answer = GroundedAnswer(
            answer="答案是 42 [S9]。",
            citations=[Citation("S9", "答案是 42")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(result["invalid_ids"], ["S9"])

    def test_quote_not_found(self):
        answer = GroundedAnswer(
            answer="TS-999 表示数据库崩溃 [S1]。",
            citations=[Citation("S1", "TS-999 表示数据库崩溃。")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["quote_not_found"]), 1)

    def test_missing_quote(self):
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。",
            citations=[Citation("S1", "")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["missing_quote"]), 1)

    def test_uncited_claim_detected(self):
        answer = GroundedAnswer(
            answer="TS-999 表示下游超时 [S1]。另外数据库也坏了。",
            citations=[Citation("S1", "TS-999 表示下游超时。")],
        )
        result = check_citations(answer, self.sources)
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["uncited_claims"]), 1)

    def test_citation_precision(self):
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
        self.assertTrue(is_abstention("证据不足：没有找到相关来源。"))
        self.assertFalse(is_abstention("TS-999 表示下游超时 [S1]。"))

    def test_abstention_answer_is_valid(self):
        answer = GroundedAnswer(answer="证据不足：没有找到相关来源。", citations=[])
        result = check_citations(answer, self.sources)
        self.assertTrue(result["valid"])

    def test_facts_and_forbidden(self):
        ok, missing = answer_contains_facts("答案包含 N+1。", ["N+1"])
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(forbidden_hits("一定是 N+1", ["一定是 N+1"]), ["一定是 N+1"])


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            make_chunk("c1", "docs/error-codes.md", "TS-999 表示下游超时。"),
            make_chunk("c2", "runbooks/db-pool.md", "先扩容副本，再修慢查询。"),
        ]

    def test_build_sources_ids(self):
        sources = build_sources(self.chunks)
        self.assertEqual([source.source_id for source in sources], ["S1", "S2"])
        self.assertEqual(sources[0].doc_id, "docs/error-codes.md")

    def test_prompt_contains_sources_and_json_contract(self):
        prompt = build_prompt("TS-999 是什么？", build_sources(self.chunks))
        self.assertIn("[S1]", prompt)
        self.assertIn("[S2]", prompt)
        self.assertIn('"citations"', prompt)
        self.assertIn("证据不足", prompt)

    def test_extractive_answer_cites_source(self):
        grounded = extractive_answer(
            "TS-999 是什么错误？", build_sources(self.chunks), min_overlap=1
        )
        self.assertIn("[S1]", grounded.answer)
        self.assertEqual(grounded.citations[0].source_id, "S1")
        self.assertIn("下游超时", grounded.answer)

    def test_extractive_abstains_on_no_overlap(self):
        grounded = extractive_answer(
            "Kubernetes 内核参数怎么改？", build_sources(self.chunks), min_overlap=2
        )
        self.assertTrue(is_abstention(grounded.answer))
        self.assertEqual(grounded.citations, [])

    def test_generate_unknown_backend(self):
        with self.assertRaises(ValueError):
            generate_answer("x", build_sources(self.chunks), backend="magic")


class DataTests(unittest.TestCase):
    def test_cases_have_required_fields(self):
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
        from common import load_documents

        docs = {doc.doc_id for doc in load_documents(DEFAULT_CORPUS)}
        cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
        for case in cases:
            for doc_id in case["expected_sources"]:
                self.assertIn(doc_id, docs, case["id"])


if __name__ == "__main__":
    unittest.main()
