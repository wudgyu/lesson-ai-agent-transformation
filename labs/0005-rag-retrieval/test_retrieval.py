"""Offline tests for lesson 0005. No API key required.

    python3 -m unittest -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from embeddings import embed_texts
from rag import (
    BM25Index,
    Chunk,
    DenseIndex,
    Document,
    HybridIndex,
    LexicalVectorIndex,
    build_chunks,
    chunk_naive,
    chunk_structure,
    chunks_from_index,
    load_documents,
    load_index,
    make_index,
    match_evidence,
    rrf_fuse,
    save_index,
    score_ranking,
    summarize,
    tokenize,
)

ROOT = Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
CASES = ROOT / "eval_cases.json"


def make_chunk(chunk_id: str, doc_id: str, text: str, heading: str = "H") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        heading=heading,
        text=text,
        start_line=1,
        end_line=1,
    )


class TokenizerTests(unittest.TestCase):
    def test_camel_case_is_split(self):
        tokens = tokenize("OrderQueryService.findRecentOrders")
        for expected in ("orderqueryservice", "order", "query", "service",
                         "findrecentorders", "find", "recent", "orders"):
            self.assertIn(expected, tokens)

    def test_config_key_is_split(self):
        tokens = tokenize("maximum-pool-size: 50")
        for expected in ("maximum-pool-size", "maximum", "pool", "size", "50"):
            self.assertIn(expected, tokens)

    def test_cjk_bigrams(self):
        tokens = tokenize("数据库连接池")
        for expected in ("数据", "据库", "库连", "连接", "接池"):
            self.assertIn(expected, tokens)

    def test_punctuation_does_not_create_empty_tokens(self):
        tokens = tokenize("!!! ，，， ---")
        self.assertEqual(tokens, [])


class BM25Tests(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            make_chunk("a", "docs/errors.md", "TS-999 表示下游超时。"),
            make_chunk("b", "docs/errors.md", "TS-998 表示连接被重置。"),
            make_chunk("c", "docs/unrelated.md", "连接池的最大连接数是 50。"),
        ]
        self.index = BM25Index(self.chunks)

    def test_exact_error_code_wins(self):
        top = self.index.search("TS-999 是什么错误？", k=3)
        self.assertEqual(top[0][0], "a")

    def test_exact_error_code_does_not_confuse_neighbor(self):
        top = self.index.search("TS-998 呢？", k=3)
        self.assertEqual(top[0][0], "b")

    def test_no_overlap_returns_empty(self):
        self.assertEqual(self.index.search("xyzzy", k=3), [])

    def test_scores_are_descending(self):
        top = self.index.search("连接池", k=3)
        scores = [score for _, score in top]
        self.assertEqual(scores, sorted(scores, reverse=True))


class ChunkingTests(unittest.TestCase):
    def setUp(self):
        self.doc = Document(
            doc_id="runbooks/db.md",
            title="Runbook: DB",
            text=(
                "# Runbook: DB\n\n"
                "这是一个前言。\n\n"
                "## 症状\n\n"
                "5xx 上升，连接池耗尽。\n\n"
                "## 处置\n\n"
                "先扩容，再修慢查询。\n"
            ),
        )

    def test_structure_chunks_follow_headings(self):
        chunks = chunk_structure(self.doc)
        headings = [chunk.heading for chunk in chunks]
        self.assertIn("Runbook: DB > 概述", headings)
        self.assertIn("Runbook: DB > 症状", headings)
        self.assertIn("Runbook: DB > 处置", headings)
        for chunk in chunks:
            self.assertTrue(chunk.chunk_id.startswith("runbooks/db.md#"))
            self.assertIn("Runbook: DB", chunk.text)

    def test_structure_chunk_ids_are_unique(self):
        doc = Document(
            doc_id="x.md",
            title="X",
            text="# X\n\n## A\n\na\n\n## A\n\nb\n",
        )
        chunks = chunk_structure(doc)
        ids = [chunk.chunk_id for chunk in chunks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_naive_chunker_progress_and_bounds(self):
        text = "line\n" * 200
        doc = Document(doc_id="long.txt", title="long", text=text)
        chunks = chunk_naive(doc, size=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 100)
            self.assertTrue(chunk.text)

    def test_naive_chunker_rejects_bad_overlap(self):
        with self.assertRaises(ValueError):
            chunk_naive(self.doc, size=100, overlap=100)

    def test_build_chunks_rejects_unknown_chunker(self):
        with self.assertRaises(ValueError):
            build_chunks([self.doc], chunker="magic")


class FusionTests(unittest.TestCase):
    def test_rrf_prefers_consensus(self):
        fused = rrf_fuse([["a", "b", "c"], ["c", "a", "b"]], k=60)
        self.assertEqual(fused[0][0], "a")

    def test_rrf_is_deterministic_on_ties(self):
        first = rrf_fuse([["b", "a"], ["a", "b"]], k=60)
        second = rrf_fuse([["b", "a"], ["a", "b"]], k=60)
        self.assertEqual(first, second)
        self.assertEqual([item[0] for item in first], ["a", "b"])

    def test_rrf_weights_can_disable_a_list(self):
        fused = rrf_fuse([["a", "b"], ["b", "a"]], k=60, weights=[1.0, 0.0])
        self.assertEqual([item[0] for item in fused], ["a", "b"])

    def test_rrf_rejects_bad_weights(self):
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]], weights=[1.0, 2.0])

    def test_hybrid_returns_at_most_k(self):
        chunks = [
            make_chunk("a", "d", "连接池耗尽 HikariPool"),
            make_chunk("b", "d", "缓存击穿 Redis"),
            make_chunk("c", "d", "JWT 时钟偏移"),
        ]
        hybrid = HybridIndex(BM25Index(chunks), LexicalVectorIndex(chunks))
        self.assertLessEqual(len(hybrid.search("连接池", k=2)), 2)


class DenseTests(unittest.TestCase):
    def test_dense_index_uses_query_vector(self):
        chunks = [
            make_chunk("a", "d", "连接池耗尽"),
            make_chunk("b", "d", "缓存击穿"),
        ]
        vectors = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
        index = DenseIndex(chunks, vectors, embed_query=lambda query: [1.0, 0.0])
        self.assertEqual(index.search("任意查询", k=2)[0][0], "a")

    def test_dense_index_requires_all_vectors(self):
        chunks = [make_chunk("a", "d", "x"), make_chunk("b", "d", "y")]
        with self.assertRaises(ValueError):
            DenseIndex(chunks, {"a": [1.0, 0.0]}, embed_query=lambda query: [1.0, 0.0])

    def test_embedding_without_key_fails_fast(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                embed_texts(["hello"])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.chunks = {
            "c1": make_chunk("c1", "docs/a.md", "连接池 used 48/50"),
            "c2": make_chunk("c2", "docs/b.md", "缓存击穿"),
            "c3": make_chunk("c3", "docs/a.md", "别的内容"),
        }
        self.case = {
            "evidence": [{"doc": "docs/a.md", "contains": "48/50"}],
        }

    def test_perfect_retrieval(self):
        result = score_ranking(["c1"], self.chunks, self.case, k=5)
        self.assertEqual(result["recall"], 1.0)
        self.assertTrue(result["full"])
        self.assertEqual(result["mrr"], 1.0)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["context_chars"], len(self.chunks["c1"].text))

    def test_total_miss(self):
        result = score_ranking(["c2"], self.chunks, self.case, k=5)
        self.assertEqual(result["recall"], 0.0)
        self.assertFalse(result["full"])
        self.assertEqual(result["mrr"], 0.0)
        self.assertEqual(result["precision"], 0.0)

    def test_partial_multi_evidence(self):
        case = {
            "evidence": [
                {"doc": "docs/a.md", "contains": "48/50"},
                {"doc": "docs/b.md", "contains": "缓存击穿"},
            ]
        }
        result = score_ranking(["c1"], self.chunks, case, k=5)
        self.assertEqual(result["recall"], 0.5)
        self.assertFalse(result["full"])
        self.assertEqual(result["mrr"], 1.0)

    def test_evidence_is_doc_scoped(self):
        other = make_chunk("c4", "docs/other.md", "48/50")
        self.assertFalse(
            match_evidence(other, {"doc": "docs/a.md", "contains": "48/50"})
        )

    def test_summarize(self):
        rows = [
            {"recall": 1.0, "full": True, "mrr": 1.0, "precision": 1.0, "context_chars": 100},
            {"recall": 0.0, "full": False, "mrr": 0.0, "precision": 0.0, "context_chars": 300},
        ]
        summary = summarize(rows)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["recall"], 0.5)
        self.assertEqual(summary["full"], 0.5)
        self.assertEqual(summary["mrr"], 0.5)
        self.assertEqual(summary["precision"], 0.5)
        self.assertEqual(summary["avg_context_chars"], 200.0)


class IndexTests(unittest.TestCase):
    def test_index_roundtrip(self):
        docs = [Document(doc_id="a.md", title="A", text="# A\n\n## B\n\nhello")]
        chunks = build_chunks(docs, chunker="structure")
        index = make_index(docs, chunks, chunker="structure")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.json"
            save_index(index, path)
            loaded = load_index(path)
            self.assertEqual(loaded["chunker"], "structure")
            self.assertEqual(
                [chunk.chunk_id for chunk in chunks_from_index(loaded)],
                [chunk.chunk_id for chunk in chunks],
            )

    def test_real_corpus_evidence_labels_are_present(self):
        docs = {doc.doc_id: doc for doc in load_documents(CORPUS)}
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        for case in cases:
            for item in case["evidence"]:
                self.assertIn(item["doc"], docs, case["id"])
                self.assertIn(
                    item["contains"].lower(),
                    docs[item["doc"]].text.lower(),
                    "%s: stale evidence '%s'" % (case["id"], item["contains"]),
                )

    def test_structure_chunks_cover_evidence_phrases(self):
        docs = load_documents(CORPUS)
        chunks = build_chunks(docs, chunker="structure")
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        for case in cases:
            for item in case["evidence"]:
                matches = [
                    chunk
                    for chunk in chunks
                    if chunk.doc_id == item["doc"]
                    and item["contains"].lower() in chunk.text.lower()
                ]
                self.assertTrue(
                    matches,
                    "%s: '%s' split or absent in structure chunks"
                    % (case["id"], item["contains"]),
                )


if __name__ == "__main__":
    unittest.main()
