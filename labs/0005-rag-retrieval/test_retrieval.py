"""第 5 课离线测试：tokenizer、切分、检索、融合与评测指标。无需 API key。

    python3 -m unittest -v

全部使用手写 chunk 或仓库内的小语料，目的是固定“可解释的基线行为”：
tokenizer 的切分规则、BM25 的排序、chunk 边界、RRF 的确定性，
以及 recall / MRR / precision 的定义。真实 embedding 路径只做 fail-fast 断言。
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
    """构造最小 Chunk；行号固定为 1，因为检索与评分只读取 chunk_id/doc_id/text。"""
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        heading=heading,
        text=text,
        start_line=1,
        end_line=1,
    )


class TokenizerTests(unittest.TestCase):
    """固定中英混合 tokenizer 的可观察行为，防止后续优化悄悄改变召回。"""

    def test_camel_case_is_split(self):
        # 整串保留用于精确匹配，同时拆出 camelCase 子词用于部分匹配。
        tokens = tokenize("OrderQueryService.findRecentOrders")
        for expected in ("orderqueryservice", "order", "query", "service",
                         "findrecentorders", "find", "recent", "orders"):
            self.assertIn(expected, tokens)

    def test_config_key_is_split(self):
        # kebab-case：保留完整配置键，同时拆成单词，兼顾精确与模糊查询。
        tokens = tokenize("maximum-pool-size: 50")
        for expected in ("maximum-pool-size", "maximum", "pool", "size", "50"):
            self.assertIn(expected, tokens)

    def test_cjk_bigrams(self):
        # 中文不依赖分词词典，用重叠 bigram 覆盖“连接池”这类短语。
        tokens = tokenize("数据库连接池")
        for expected in ("数据", "据库", "库连", "连接", "接池"):
            self.assertIn(expected, tokens)

    def test_punctuation_does_not_create_empty_tokens(self):
        # 标点与空白不应产生空 token，否则 BM25 的 df 会被污染。
        tokens = tokenize("!!! ，，， ---")
        self.assertEqual(tokens, [])


class BM25Tests(unittest.TestCase):
    """BM25 词面检索的基线：错误码、无重合与排序稳定性。"""

    def setUp(self):
        # a/b 是相邻错误码；c 与“连接池”查询共享词面，用于验证排序。
        self.chunks = [
            make_chunk("a", "docs/errors.md", "TS-999 表示下游超时。"),
            make_chunk("b", "docs/errors.md", "TS-998 表示连接被重置。"),
            make_chunk("c", "docs/unrelated.md", "连接池的最大连接数是 50。"),
        ]
        self.index = BM25Index(self.chunks)

    def test_exact_error_code_wins(self):
        # tokenizer 会保留完整错误码，因此 TS-999 应精确命中 a。
        top = self.index.search("TS-999 是什么错误？", k=3)
        self.assertEqual(top[0][0], "a")

    def test_exact_error_code_does_not_confuse_neighbor(self):
        # TS-998 与 TS-999 只差一位，不能因为共享前缀就召回错误邻居。
        top = self.index.search("TS-998 呢？", k=3)
        self.assertEqual(top[0][0], "b")

    def test_no_overlap_returns_empty(self):
        # 没有任何 token 重合时返回空列表，而不是一堆 0 分结果。
        self.assertEqual(self.index.search("xyzzy", k=3), [])

    def test_scores_are_descending(self):
        # 调用方依赖降序结果直接截断 top-k，排序契约不能变。
        top = self.index.search("连接池", k=3)
        scores = [score for _, score in top]
        self.assertEqual(scores, sorted(scores, reverse=True))


class ChunkingTests(unittest.TestCase):
    """结构切分与 naive 切分的边界：标题、前言、chunk id、长度与非法参数。"""

    def setUp(self):
        # 带前言和两个二级标题的 Markdown，覆盖“概述”chunk 的生成。
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
        # 每个 chunk 的 heading 带上文档标题，text 也保留标题前缀作为上下文。
        chunks = chunk_structure(self.doc)
        headings = [chunk.heading for chunk in chunks]
        self.assertIn("Runbook: DB > 概述", headings)
        self.assertIn("Runbook: DB > 症状", headings)
        self.assertIn("Runbook: DB > 处置", headings)
        for chunk in chunks:
            self.assertTrue(chunk.chunk_id.startswith("runbooks/db.md#"))
            self.assertIn("Runbook: DB", chunk.text)

    def test_structure_chunk_ids_are_unique(self):
        # 重复的二级标题 “A” 会生成相同 slug，chunk_id 仍必须唯一。
        doc = Document(
            doc_id="x.md",
            title="X",
            text="# X\n\n## A\n\na\n\n## A\n\nb\n",
        )
        chunks = chunk_structure(doc)
        ids = [chunk.chunk_id for chunk in chunks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_naive_chunker_progress_and_bounds(self):
        # 200 行文本按 100 字符切、20 字符重叠：必须前进，且每块不超长。
        text = "line\n" * 200
        doc = Document(doc_id="long.txt", title="long", text=text)
        chunks = chunk_naive(doc, size=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 100)
            self.assertTrue(chunk.text)

    def test_naive_chunker_rejects_bad_overlap(self):
        # overlap >= size 会让窗口无法前进，必须直接拒绝而不是死循环。
        with self.assertRaises(ValueError):
            chunk_naive(self.doc, size=100, overlap=100)

    def test_build_chunks_rejects_unknown_chunker(self):
        # 配置写错时应尽早失败，避免悄悄回退到默认切分策略。
        with self.assertRaises(ValueError):
            build_chunks([self.doc], chunker="magic")


class FusionTests(unittest.TestCase):
    """RRF 只使用排名，因此要固定共识优先、平局确定性与权重语义。"""

    def test_rrf_prefers_consensus(self):
        # a 在两路都靠前，c 只在第二路靠前；RRF 让共识项胜出。
        fused = rrf_fuse([["a", "b", "c"], ["c", "a", "b"]], k=60)
        self.assertEqual(fused[0][0], "a")

    def test_rrf_is_deterministic_on_ties(self):
        # 两路排名对称时分数相同，按 chunk_id 字典序打破平局。
        first = rrf_fuse([["b", "a"], ["a", "b"]], k=60)
        second = rrf_fuse([["b", "a"], ["a", "b"]], k=60)
        self.assertEqual(first, second)
        self.assertEqual([item[0] for item in first], ["a", "b"])

    def test_rrf_weights_can_disable_a_list(self):
        # 权重 0 等价于忽略这一路召回，结果完全由第一路决定。
        fused = rrf_fuse([["a", "b"], ["b", "a"]], k=60, weights=[1.0, 0.0])
        self.assertEqual([item[0] for item in fused], ["a", "b"])

    def test_rrf_rejects_bad_weights(self):
        # 权重数量必须与 ranking 数量一致，否则无法判断哪一路被加权。
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]], weights=[1.0, 2.0])

    def test_hybrid_returns_at_most_k(self):
        # 融合后仍要在最外层截断到 k，不能把候选集原样返回。
        chunks = [
            make_chunk("a", "d", "连接池耗尽 HikariPool"),
            make_chunk("b", "d", "缓存击穿 Redis"),
            make_chunk("c", "d", "JWT 时钟偏移"),
        ]
        hybrid = HybridIndex(BM25Index(chunks), LexicalVectorIndex(chunks))
        self.assertLessEqual(len(hybrid.search("连接池", k=2)), 2)


class DenseTests(unittest.TestCase):
    """DenseIndex 的检索契约与真实 embedding 的失败模式。"""

    def test_dense_index_uses_query_vector(self):
        # query 向量与 a 对齐；文本内容不参与打分，确保走的是向量而非词面。
        chunks = [
            make_chunk("a", "d", "连接池耗尽"),
            make_chunk("b", "d", "缓存击穿"),
        ]
        vectors = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
        index = DenseIndex(chunks, vectors, embed_query=lambda query: [1.0, 0.0])
        self.assertEqual(index.search("任意查询", k=2)[0][0], "a")

    def test_dense_index_requires_all_vectors(self):
        # 缺少任一 chunk 的向量就拒绝构建，避免运行时静默漏召回。
        chunks = [make_chunk("a", "d", "x"), make_chunk("b", "d", "y")]
        with self.assertRaises(ValueError):
            DenseIndex(chunks, {"a": [1.0, 0.0]}, embed_query=lambda query: [1.0, 0.0])

    def test_embedding_without_key_fails_fast(self):
        # 没有 API key 时立即报错，而不是返回空向量让检索“看起来正常”。
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                embed_texts(["hello"])


class ScoringTests(unittest.TestCase):
    """固定 recall / full / MRR / precision 的定义，以及证据的文档作用域。"""

    def setUp(self):
        # c1 命中证据；c2 是另一篇文档；c3 同文档但不含证据短语。
        self.chunks = {
            "c1": make_chunk("c1", "docs/a.md", "连接池 used 48/50"),
            "c2": make_chunk("c2", "docs/b.md", "缓存击穿"),
            "c3": make_chunk("c3", "docs/a.md", "别的内容"),
        }
        self.case = {
            "evidence": [{"doc": "docs/a.md", "contains": "48/50"}],
        }

    def test_perfect_retrieval(self):
        # 单条证据命中：所有指标为 1；context_chars 只统计召回 chunk 的文本。
        result = score_ranking(["c1"], self.chunks, self.case, k=5)
        self.assertEqual(result["recall"], 1.0)
        self.assertTrue(result["full"])
        self.assertEqual(result["mrr"], 1.0)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["context_chars"], len(self.chunks["c1"].text))

    def test_total_miss(self):
        # 召回结果与证据无关：recall、MRR、precision 都为 0。
        result = score_ranking(["c2"], self.chunks, self.case, k=5)
        self.assertEqual(result["recall"], 0.0)
        self.assertFalse(result["full"])
        self.assertEqual(result["mrr"], 0.0)
        self.assertEqual(result["precision"], 0.0)

    def test_partial_multi_evidence(self):
        # 两条证据只命中一条：recall=0.5、full=False；top-1 命中，MRR 仍为 1。
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
        # 同样的文本出现在别的文档里不算命中，避免跨文档“碰巧”得分。
        other = make_chunk("c4", "docs/other.md", "48/50")
        self.assertFalse(
            match_evidence(other, {"doc": "docs/a.md", "contains": "48/50"})
        )

    def test_summarize(self):
        # summarize 对逐 case 指标取算术平均；full 是命中 case 的比例。
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
    """索引持久化与真实语料的一致性检查。"""

    def test_index_roundtrip(self):
        # 保存再加载后，chunker 与 chunk_id 顺序都应保持不变。
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
        # 防止 eval_cases.json 指向已删除或改写的语料（stale evidence）。
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
        # 结构切分不能把证据短语切断，否则召回永远拿不到完整证据。
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
