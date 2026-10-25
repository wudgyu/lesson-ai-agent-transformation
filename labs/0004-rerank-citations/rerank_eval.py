"""Retrieval-level rerank evaluation for lesson 0004.

Reuses lesson 0003's 20 labelled queries and index. It answers one question:

    Does a second-stage reranker improve the top-5 enough to justify its cost,
    given the first-stage candidate ceiling?

Usage:
    python3 rerank_eval.py --retriever bm25
    python3 rerank_eval.py --retriever hybrid --candidate-k 15 --top-k 5
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common import (
    DEFAULT_INDEX,
    DEFAULT_RETRIEVAL_CASES,
    BM25Index,
    build_retriever,
    load_chunks,
)
from rag import score_ranking, summarize
from rerank import rerank_candidates

ROOT = Path(__file__).resolve().parent


def load_cases(path: Path = DEFAULT_RETRIEVAL_CASES) -> List[Dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evaluate(
    cases: Sequence[Dict[str, Any]],
    chunks_by_id: Dict[str, Any],
    candidates_by_case: Dict[str, List[Tuple[str, float]]],
    method: str,
    top_k: int,
    idf: Dict[str, float],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for case in cases:
        candidates = candidates_by_case[case["id"]]
        if method == "none":
            ranked = candidates[:top_k]
            ranking = [chunk_id for chunk_id, _ in ranked]
            ranks = {chunk_id: rank for rank, (chunk_id, _) in enumerate(ranked, start=1)}
        else:
            reranked = rerank_candidates(
                case["query"],
                candidates,
                chunks_by_id,
                idf,
                top_n=top_k,
                method=method,
            )
            ranking = [item.chunk_id for item in reranked]
            ranks = {item.chunk_id: item.first_stage_rank for item in reranked}
        scored = score_ranking(ranking, chunks_by_id, case, top_k)
        rows.append(
            {
                "id": case["id"],
                "type": case["type"],
                "query": case["query"],
                "method": method,
                "recall": scored["recall"],
                "full": scored["full"],
                "mrr": scored["mrr"],
                "precision": scored["precision"],
                "context_chars": scored["context_chars"],
                "first_stage_ranks": ranks,
                "top": scored["top"],
                "missing": scored["missing"],
            }
        )
    return rows


def ceiling(
    cases: Sequence[Dict[str, Any]],
    chunks_by_id: Dict[str, Any],
    candidates_by_case: Dict[str, List[Tuple[str, float]]],
    candidate_k: int,
) -> Dict[str, Any]:
    rows = []
    for case in cases:
        ranking = [chunk_id for chunk_id, _ in candidates_by_case[case["id"]]]
        scored = score_ranking(ranking, chunks_by_id, case, candidate_k)
        rows.append(scored)
    return summarize(rows)


def print_table(rows_by_name: Dict[str, Dict[str, Any]]) -> None:
    print(
        "%-10s %-12s %9s %8s %8s %7s %10s"
        % ("retriever", "mode", "recall@k", "full@k", "mrr@k", "prec", "ctx_chars")
    )
    print("-" * 72)
    for name, summary in rows_by_name.items():
        print(
            "%-10s %-12s %9.3f %8.3f %8.3f %7.3f %10.0f"
            % (
                name,
                summary["mode"],
                summary["recall"],
                summary["full"],
                summary["mrr"],
                summary.get("precision", 0.0),
                summary.get("avg_context_chars", 0.0),
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Rerank eval (lesson 0004)")
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument(
        "--retriever", choices=["bm25", "hybrid"], default="bm25"
    )
    parser.add_argument("--candidate-k", type=int, default=15)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--method", choices=["all", "none", "heuristic"], default="all"
    )
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    chunks = load_chunks(Path(args.index))
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    bm25 = BM25Index(chunks)
    retriever = build_retriever(args.retriever, chunks, candidate_k=args.candidate_k)
    cases = load_cases()

    candidates_by_case: Dict[str, List[Tuple[str, float]]] = {}
    for case in cases:
        candidates_by_case[case["id"]] = retriever.search(
            case["query"], args.candidate_k
        )

    methods = ["none", "heuristic"] if args.method == "all" else [args.method]
    all_rows: List[Dict[str, Any]] = []
    summaries: Dict[str, Dict[str, Any]] = {}
    for method in methods:
        rows = evaluate(
            cases,
            chunks_by_id,
            candidates_by_case,
            method,
            args.top_k,
            bm25.idf,
        )
        all_rows.extend(rows)
        summary = summarize(rows)
        summary["mode"] = "%s-%d" % (method, args.top_k)
        summaries[method] = summary

    ceiling_summary = ceiling(
        cases, chunks_by_id, candidates_by_case, args.candidate_k
    )
    ceiling_summary["mode"] = "ceiling@%d" % args.candidate_k
    summaries["ceiling"] = ceiling_summary

    print("=== rerank eval ===")
    print("index       : %s (%d chunks)" % (args.index, len(chunks)))
    print(
        "retriever   : %s · candidates=%d · top-k=%d"
        % (args.retriever, args.candidate_k, args.top_k)
    )
    print("cases       : %d" % len(cases))
    print()
    print_table(summaries)

    if "none" in summaries and "heuristic" in summaries:
        delta = summaries["heuristic"]["recall"] - summaries["none"]["recall"]
        print()
        print(
            "rerank delta: recall %+0.3f · full %+0.3f · mrr %+0.3f"
            % (
                delta,
                summaries["heuristic"]["full"] - summaries["none"]["full"],
                summaries["heuristic"]["mrr"] - summaries["none"]["mrr"],
            )
        )
        print(
            "ceiling     : recall %.3f at candidate_k=%d; rerank cannot exceed it."
            % (ceiling_summary["recall"], args.candidate_k)
        )

    if "heuristic" in summaries:
        failures = [row for row in all_rows if row["method"] == "heuristic" and not row["full"]]
        print()
        print("failures · heuristic: %d/%d" % (len(failures), len(cases)))
        for row in failures:
            print("  %s [%s] %s" % (row["id"], row["type"], row["query"]))
            for item in row["missing"]:
                print("    missing: %s :: %s" % (item.get("doc"), item.get("contains")))
            print("    top: %s" % ", ".join(row["top"]))

    out_path = Path(args.out) if args.out else (
        ROOT / "reports" / ("rerank-%s-%dk.json" % (args.retriever, args.candidate_k))
    )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": str(args.index),
        "retriever": args.retriever,
        "candidate_k": args.candidate_k,
        "top_k": args.top_k,
        "summaries": summaries,
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
