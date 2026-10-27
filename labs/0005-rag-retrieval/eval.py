"""Retrieval evaluation for lesson 0005.

用法：
    python3 eval.py --validate
    python3 eval.py --index index/structure.json --retriever all
    python3 eval.py --index index/naive-400.json --retriever bm25
    python3 eval.py --index index/structure-embedded.json --retriever all

评测的是“检索层”，不是生成层：
- recall@k：该找到的证据，有多少进了 top-k；
- full@k：所有证据都进 top-k 的用例比例；
- mrr@k：第一条命中的倒数排名。

先跑 retrieval eval，再谈 prompt / 模型。检索没召回，生成只能猜。
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from rag import (
    BM25Index,
    DenseIndex,
    HybridIndex,
    LexicalVectorIndex,
    chunk_stats,
    chunks_from_index,
    load_documents,
    load_index,
    score_ranking,
    summarize,
)

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "eval_cases.json"
DEFAULT_INDEX = ROOT / "index" / "structure.json"
VALID_TYPES = ("keyword", "semantic", "mixed", "multi-hop")


def load_cases(path: Path = CASES_PATH) -> List[Dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("eval_cases.json must contain a list")
    return cases


def validate(cases: Sequence[Dict[str, Any]], corpus_dir: Path, index_path: Optional[Path]) -> int:
    errors: List[str] = []
    warnings: List[str] = []
    seen = set()

    docs = {doc.doc_id: doc for doc in load_documents(corpus_dir)}

    for case in cases:
        cid = case.get("id", "?")
        if cid in seen:
            errors.append("%s: duplicate id" % cid)
        seen.add(cid)
        for key in ("id", "type", "query", "evidence"):
            if key not in case:
                errors.append("%s: missing field '%s'" % (cid, key))
        if case.get("type") not in VALID_TYPES:
            errors.append("%s: unknown type '%s'" % (cid, case.get("type")))
        evidence = case.get("evidence") or []
        if not evidence:
            errors.append("%s: evidence is empty" % cid)
        for item in evidence:
            doc_id = item.get("doc")
            needle = item.get("contains")
            if not doc_id or not needle:
                errors.append("%s: evidence needs 'doc' and 'contains'" % cid)
                continue
            if doc_id not in docs:
                errors.append("%s: unknown doc '%s'" % (cid, doc_id))
                continue
            if needle.lower() not in docs[doc_id].text.lower():
                errors.append(
                    "%s: phrase '%s' not found in %s (stale label?)"
                    % (cid, needle, doc_id)
                )

    if index_path is not None and index_path.exists():
        index = load_index(index_path)
        chunks = chunks_from_index(index)
        if not chunks:
            errors.append("index has no chunks: %s" % index_path)
        ids = [chunk.chunk_id for chunk in chunks]
        if len(ids) != len(set(ids)):
            errors.append("index contains duplicate chunk ids")
        by_doc: Dict[str, List[Any]] = defaultdict(list)
        for chunk in chunks:
            by_doc[chunk.doc_id].append(chunk)
        for case in cases:
            cid = case.get("id", "?")
            for item in case.get("evidence") or []:
                doc_id = item.get("doc")
                needle = item.get("contains", "")
                if doc_id in by_doc and needle:
                    if not any(
                        needle.lower() in chunk.text.lower()
                        for chunk in by_doc[doc_id]
                    ):
                        warnings.append(
                            "%s: phrase '%s' is split or absent in chunker '%s'"
                            % (cid, needle, index.get("chunker", "?"))
                        )

    if errors:
        print("VALIDATION FAILED")
        for error in errors:
            print("  -", error)
        return 1

    print("OK: %d cases validated" % len(cases))
    print("  docs       : %d" % len(docs))
    print("  types      : %s" % ", ".join(
        "%s=%d" % (t, sum(1 for c in cases if c.get("type") == t)) for t in VALID_TYPES
    ))
    if index_path is not None and index_path.exists():
        index = load_index(index_path)
        stats = chunk_stats(chunks_from_index(index))
        print(
            "  index      : %s · %s · %d chunks"
            % (index_path, index.get("chunker", "?"), stats["chunks"])
        )
    if warnings:
        print("  warnings   : %d" % len(warnings))
        for warning in warnings[:10]:
            print("    -", warning)
        if len(warnings) > 10:
            print("    ... %d more" % (len(warnings) - 10))
    return 0


def build_retrievers(
    index: Dict[str, Any],
    chunks: Sequence[Any],
    args: argparse.Namespace,
) -> List[Tuple[str, Any]]:
    bm25 = BM25Index(chunks)
    vectors = index.get("vectors") or {}
    retriever = args.retriever

    if vectors:
        from embeddings import embedding_config, make_query_embedder

        config = embedding_config()
        model = index.get("embedding_model") or args.embed_model or config["model"]
        embedder = make_query_embedder(model=model)
        dense = DenseIndex(chunks, vectors, embedder)
        items = [
            ("vector(dense:%s)" % model, dense),
            (
                "hybrid(dense:%s)" % model,
                HybridIndex(bm25, dense, candidate_k=args.candidate_k, rrf_k=args.rrf_k),
            ),
        ]
    else:
        lexical = LexicalVectorIndex(chunks)
        items = [
            ("vector(lexical)", lexical),
            (
                "hybrid(lexical)",
                HybridIndex(bm25, lexical, candidate_k=args.candidate_k, rrf_k=args.rrf_k),
            ),
        ]

    available = [("bm25", bm25)] + items
    if retriever == "all":
        return available
    if retriever == "bm25":
        return [("bm25", bm25)]
    if retriever == "vector":
        return [items[0]]
    if retriever == "hybrid":
        return [items[1]]
    raise ValueError("unknown retriever: %s" % retriever)


def evaluate_retriever(
    name: str,
    retriever: Any,
    cases: Sequence[Dict[str, Any]],
    chunks_by_id: Dict[str, Any],
    k: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for case in cases:
        ranking = [chunk_id for chunk_id, _ in retriever.search(case["query"], k)]
        scored = score_ranking(ranking, chunks_by_id, case, k)
        rows.append(
            {
                "retriever": name,
                "id": case["id"],
                "type": case.get("type", "?"),
                "query": case["query"],
                "note": case.get("note", ""),
                "recall": scored["recall"],
                "full": scored["full"],
                "mrr": scored["mrr"],
                "first_rank": scored["first_rank"],
                "precision": scored["precision"],
                "context_chars": scored["context_chars"],
                "top": scored["top"],
                "missing": scored["missing"],
            }
        )
    return rows


def by_type(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["type"]].append(row)
    return {name: summarize(group) for name, group in sorted(groups.items())}


def print_summary_table(summaries: Dict[str, Dict[str, float]]) -> None:
    print(
        "%-30s %9s %8s %8s %7s %10s"
        % ("retriever", "recall@k", "full@k", "mrr@k", "prec", "ctx_chars")
    )
    print("-" * 78)
    for name, summary in summaries.items():
        print(
            "%-30s %9.3f %8.3f %8.3f %7.3f %10.0f"
            % (
                name,
                summary["recall"],
                summary["full"],
                summary["mrr"],
                summary.get("precision", 0.0),
                summary.get("avg_context_chars", 0.0),
            )
        )


def print_slice_table(name: str, slices: Dict[str, Dict[str, float]]) -> None:
    print()
    print("by type · %s" % name)
    print(
        "%-12s %5s %9s %8s %8s %7s %10s"
        % ("type", "n", "recall@k", "full@k", "mrr@k", "prec", "ctx_chars")
    )
    print("-" * 65)
    for slice_name, summary in slices.items():
        print(
            "%-12s %5d %9.3f %8.3f %8.3f %7.3f %10.0f"
            % (
                slice_name,
                summary["cases"],
                summary["recall"],
                summary["full"],
                summary["mrr"],
                summary.get("precision", 0.0),
                summary.get("avg_context_chars", 0.0),
            )
        )


def print_failures(
    name: str,
    rows: Sequence[Dict[str, Any]],
    chunks_by_id: Dict[str, Any],
    k: int,
) -> None:
    failures = [row for row in rows if not row["full"]]
    if not failures:
        print()
        print("failures · %s: none" % name)
        return
    print()
    print("failures · %s: %d/%d cases" % (name, len(failures), len(rows)))
    for row in failures:
        print(
            "  %s [%s] %s"
            % (row["id"], row["type"], row["query"])
        )
        for item in row["missing"]:
            print("    missing: %s :: %s" % (item.get("doc"), item.get("contains")))
        print("    top-%d:" % k)
        for rank, chunk_id in enumerate(row["top"], start=1):
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                print("      %d. %s (unknown chunk)" % (rank, chunk_id))
            else:
                print(
                    "      %d. %-46s [%s]"
                    % (rank, chunk.doc_id, chunk.heading)
                )
        if row.get("note"):
            print("    why this case: %s" % row["note"])


def default_out(args: argparse.Namespace) -> Path:
    stem = Path(args.index).stem
    return ROOT / "reports" / ("retrieval-%s-%s.json" % (stem, args.retriever))


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieval eval (lesson 0005)")
    parser.add_argument("--index", default=str(DEFAULT_INDEX), help="index JSON")
    parser.add_argument(
        "--retriever", choices=["bm25", "vector", "hybrid", "all"], default="all"
    )
    parser.add_argument("--k", type=int, default=5, help="评测截断位置")
    parser.add_argument(
        "--candidate-k", type=int, default=50, help="hybrid 每路先召回多少候选"
    )
    parser.add_argument("--rrf-k", type=int, default=60, help="RRF 的 k 常数")
    parser.add_argument("--case", action="append", help="只跑指定 id，可重复")
    parser.add_argument("--type", action="append", help="只跑指定 type，可重复")
    parser.add_argument("--validate", action="store_true", help="离线校验评测集")
    parser.add_argument(
        "--corpus", default=str(ROOT / "corpus"), help="语料目录（validate 用）"
    )
    parser.add_argument("--embed-model", default=None, help="覆盖查询 embedding 模型")
    parser.add_argument("--out", default=None, help="JSON 报告输出路径")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = load_cases()
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            parser.error("no cases matched --case")
    if args.type:
        wanted_types = set(args.type)
        cases = [case for case in cases if case.get("type") in wanted_types]
        if not cases:
            parser.error("no cases matched --type")

    index_path = Path(args.index)
    if args.validate:
        raise SystemExit(validate(cases, Path(args.corpus), index_path if index_path.exists() else None))

    if not index_path.exists():
        print("Index not found: %s" % index_path)
        print("Build it first:")
        print("  python3 index.py --chunker structure --out index/structure.json")
        print("  python3 index.py --chunker naive --size 400 --overlap 80 --out index/naive-400.json")
        raise SystemExit(2)

    index = load_index(index_path)
    chunks = chunks_from_index(index)
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    stats = chunk_stats(chunks)
    print("=== retrieval eval ===")
    print(
        "index       : %s · %s · %d chunks"
        % (index_path, index.get("chunker", "?"), stats["chunks"])
    )
    print("cases       : %d · k=%d" % (len(cases), args.k))
    print("vectors     : %s" % (
        "dense (%s)" % index.get("embedding_model")
        if index.get("vectors")
        else "none (offline lexical-vector fallback)"
    ))
    print()

    try:
        retrievers = build_retrievers(index, chunks, args)
    except Exception as exc:
        print("Cannot build retriever: %s" % exc)
        raise SystemExit(2)

    all_rows: List[Dict[str, Any]] = []
    summaries: Dict[str, Dict[str, float]] = {}
    slices: Dict[str, Dict[str, Dict[str, float]]] = {}

    for name, retriever in retrievers:
        try:
            rows = evaluate_retriever(name, retriever, cases, chunks_by_id, args.k)
        except RuntimeError as exc:
            print("SKIP %s: %s" % (name, exc))
            if len(retrievers) == 1:
                raise SystemExit(2)
            continue
        all_rows.extend(rows)
        summaries[name] = summarize(rows)
        slices[name] = by_type(rows)

    if not summaries:
        print("No retriever completed.")
        raise SystemExit(2)

    print_summary_table(summaries)
    best = max(summaries, key=lambda name: (summaries[name]["recall"], summaries[name]["mrr"]))
    print()
    print("best by recall then mrr: %s" % best)

    focus = best if args.retriever == "all" else next(iter(summaries))
    print_slice_table(focus, slices[focus])
    print_failures(focus, [row for row in all_rows if row["retriever"] == focus], chunks_by_id, args.k)

    if args.verbose and focus != best:
        print_slice_table(best, slices[best])

    out_path = Path(args.out) if args.out else default_out(args)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": {
            "path": str(index_path),
            "chunker": index.get("chunker"),
            "params": index.get("params", {}),
            "chunks": stats["chunks"],
            "embedding_model": index.get("embedding_model"),
        },
        "k": args.k,
        "candidate_k": args.candidate_k,
        "rrf_k": args.rrf_k,
        "summaries": summaries,
        "by_type": slices,
        "rows": all_rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print()
    print("report: %s" % out_path)


if __name__ == "__main__":
    main()
