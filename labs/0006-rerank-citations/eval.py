"""Answer-level evaluation with citation checks for lesson 0006.

Pipeline per case:
    retrieve candidates -> rerank -> build source tags -> generate
    -> verify citation IDs / quotes / coverage -> check expected facts

Usage:
    python3 eval.py --validate
    python3 eval.py --backend extractive
    python3 eval.py --backend llm --model gpt-4o-mini
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from citation import (
    answer_contains_facts,
    check_citations,
    citation_recall,
    forbidden_hits,
    is_abstention,
)
from common import (
    DEFAULT_CORPUS,
    DEFAULT_INDEX,
    BM25Index,
    build_retriever,
    load_chunks,
    load_documents,
)
from generate import build_sources, fixture_answer, generate_answer
from rerank import rerank_candidates

ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = ROOT / "cases.json"


def load_cases(path: Path = DEFAULT_CASES) -> List[Dict[str, Any]]:
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("cases.json must contain a list")
    return cases


def validate(cases: Sequence[Dict[str, Any]], corpus_dir: Path) -> int:
    errors: List[str] = []
    warnings: List[str] = []
    seen = set()
    docs = {doc.doc_id for doc in load_documents(corpus_dir)}

    for case in cases:
        cid = case.get("id", "?")
        if cid in seen:
            errors.append("%s: duplicate id" % cid)
        seen.add(cid)
        for key in (
            "id",
            "query",
            "answerable",
            "expected_facts",
            "expected_sources",
            "must_not_include",
        ):
            if key not in case:
                errors.append("%s: missing field '%s'" % (cid, key))
        if not isinstance(case.get("answerable"), bool):
            errors.append("%s: answerable must be true/false" % cid)
        if case.get("answerable") and not case.get("expected_facts"):
            errors.append("%s: answerable case needs expected_facts" % cid)
        if not case.get("answerable") and case.get("expected_facts"):
            warnings.append("%s: unanswerable case has expected_facts" % cid)
        for doc_id in case.get("expected_sources") or []:
            if doc_id not in docs:
                errors.append("%s: unknown expected source '%s'" % (cid, doc_id))

    if errors:
        print("VALIDATION FAILED")
        for error in errors:
            print("  -", error)
        return 1
    print("OK: %d cases validated" % len(cases))
    print("  docs       : %d" % len(docs))
    print(
        "  answerable : %d · unanswerable: %d"
        % (
            sum(1 for case in cases if case.get("answerable")),
            sum(1 for case in cases if not case.get("answerable")),
        )
    )
    if warnings:
        print("  warnings   : %d" % len(warnings))
        for warning in warnings:
            print("    -", warning)
    return 0


def run_case(
    case: Dict[str, Any],
    retriever: Any,
    chunks_by_id: Dict[str, Any],
    idf: Dict[str, float],
    args: argparse.Namespace,
    fixtures: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    candidates = retriever.search(case["query"], args.candidate_k)
    reranked = rerank_candidates(
        case["query"],
        candidates,
        chunks_by_id,
        idf,
        top_n=args.top_k,
        method=args.reranker,
    )
    selected_chunks = [
        chunks_by_id[item.chunk_id]
        for item in reranked
        if item.chunk_id in chunks_by_id
    ]
    sources = build_sources(selected_chunks)
    if args.backend == "fixtures":
        grounded = fixture_answer(case["id"], sources, fixtures or {})
        usage = {}
    else:
        grounded, usage = generate_answer(
            case["query"],
            sources,
            backend=args.backend,
            model=args.model,
            min_overlap=args.min_overlap,
        )
    check = check_citations(grounded, sources)
    facts_ok, missing_facts = answer_contains_facts(
        grounded.answer, case.get("expected_facts") or []
    )
    forbidden = forbidden_hits(
        grounded.answer, case.get("must_not_include") or []
    )
    source_recall, missing_sources = citation_recall(
        case.get("expected_sources") or [], grounded, sources
    )
    abstained = is_abstention(grounded.answer)
    if case.get("answerable"):
        passed = bool(facts_ok and not forbidden and check["valid"] and source_recall == 1.0)
    else:
        passed = bool(abstained and check["valid"])
    return {
        "id": case["id"],
        "query": case["query"],
        "answerable": case["answerable"],
        "note": case.get("note", ""),
        "answer": grounded.answer,
        "citations": [citation.__dict__ for citation in grounded.citations],
        "check": check,
        "facts_ok": facts_ok,
        "missing_facts": missing_facts,
        "forbidden_hits": forbidden,
        "source_recall": source_recall,
        "missing_sources": missing_sources,
        "abstained": abstained,
        "pass": passed,
        "top": [item.chunk_id for item in reranked],
        "first_stage_ranks": {
            item.chunk_id: item.first_stage_rank for item in reranked
        },
        "context_chars": sum(len(chunk.text) for chunk in selected_chunks),
        "usage": usage,
    }


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def ratio(subset: Sequence[Dict[str, Any]], predicate) -> float:
        if not subset:
            return 0.0
        return sum(1.0 for row in subset if predicate(row)) / len(subset)

    return {
        "cases": len(rows),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "answer_accuracy": ratio(
            answerable, lambda row: row["facts_ok"] and not row["forbidden_hits"]
        ),
        "grounded_rate": ratio(answerable, lambda row: row["check"]["valid"]),
        "citation_precision": mean(
            [row["check"]["citation_precision"] for row in answerable]
        ),
        "citation_recall": mean([row["source_recall"] for row in answerable]),
        "abstention_accuracy": ratio(unanswerable, lambda row: row["abstained"]),
        "pass_rate": ratio(rows, lambda row: row["pass"]),
        "avg_context_chars": mean([row["context_chars"] for row in rows]),
        "total_tokens": sum(
            row["usage"].get("total_tokens", 0) for row in rows
        ),
    }


def print_summary(summary: Dict[str, Any]) -> None:
    print("answer accuracy      : %.3f  (%d answerable)" % (
        summary["answer_accuracy"], summary["answerable"]
    ))
    print("grounded rate        : %.3f" % summary["grounded_rate"])
    print("citation precision   : %.3f" % summary["citation_precision"])
    print("citation recall      : %.3f" % summary["citation_recall"])
    print(
        "abstention accuracy  : %.3f  (%d unanswerable)"
        % (summary["abstention_accuracy"], summary["unanswerable"])
    )
    print("pass rate            : %.3f" % summary["pass_rate"])
    print("avg context chars    : %.0f" % summary["avg_context_chars"])
    if summary["total_tokens"]:
        print("total tokens         : %d" % summary["total_tokens"])


def print_failures(rows: Sequence[Dict[str, Any]]) -> None:
    failures = [row for row in rows if not row["pass"]]
    if not failures:
        print("\nfailures: none")
        return
    print("\nfailures: %d/%d" % (len(failures), len(rows)))
    for row in failures:
        print("  %s [%s] %s" % (
            row["id"],
            "answerable" if row["answerable"] else "unanswerable",
            row["query"],
        ))
        print("    answer: %s" % row["answer"].replace("\n", " ")[:220])
        if row["missing_facts"]:
            print("    missing facts: %s" % ", ".join(row["missing_facts"]))
        if row["forbidden_hits"]:
            print("    forbidden hits: %s" % ", ".join(row["forbidden_hits"]))
        check = row["check"]
        if check["invalid_ids"]:
            print("    invalid citation ids: %s" % ", ".join(check["invalid_ids"]))
        if check["quote_not_found"]:
            print(
                "    quotes not found: %s"
                % ", ".join(item["source_id"] for item in check["quote_not_found"])
            )
        if check["missing_quote"]:
            print(
                "    missing quotes: %s"
                % ", ".join(item["source_id"] for item in check["missing_quote"])
            )
        if check["uncited_claims"]:
            print("    uncited claims: %d" % len(check["uncited_claims"]))
        if row["missing_sources"]:
            print("    missing sources: %s" % ", ".join(row["missing_sources"]))
        print("    top: %s" % ", ".join(row["top"]))
        if row["note"]:
            print("    why this case: %s" % row["note"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Answer eval (lesson 0006)")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    parser.add_argument("--retriever", choices=["bm25", "hybrid"], default="bm25")
    parser.add_argument(
        "--reranker", choices=["none", "heuristic"], default="heuristic"
    )
    parser.add_argument("--candidate-k", type=int, default=15)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--backend", choices=["fixtures", "extractive", "llm"], default="fixtures"
    )
    parser.add_argument(
        "--fixtures", default=str(ROOT / "fixtures.json"), help="fixtures backend 的脚本输出"
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--min-overlap", type=int, default=2)
    parser.add_argument("--case", action="append", help="只跑指定 id，可重复")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["id"] in wanted]
        if not cases:
            parser.error("no cases matched --case")

    if args.validate:
        raise SystemExit(validate(cases, DEFAULT_CORPUS))

    fixtures: Dict[str, Any] = {}
    if args.backend == "fixtures":
        fixtures = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
        missing = [case["id"] for case in cases if case["id"] not in fixtures]
        if missing:
            print("Missing fixtures for: %s" % ", ".join(missing))
            raise SystemExit(2)

    chunks = load_chunks(Path(args.index))
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    bm25 = BM25Index(chunks)
    retriever = build_retriever(args.retriever, chunks, candidate_k=args.candidate_k)

    print("=== answer + citation eval ===")
    print("index       : %s (%d chunks)" % (args.index, len(chunks)))
    print(
        "pipeline    : %s → rerank=%s → top-%d → %s"
        % (args.retriever, args.reranker, args.top_k, args.backend)
    )
    print("cases       : %d" % len(cases))
    if args.backend == "fixtures":
        print("note        : fixtures are scripted; several are intentionally flawed.")
    elif args.backend == "extractive":
        print("note        : extractive is a pipeline smoke test, not an LLM.")
    print()

    rows: List[Dict[str, Any]] = []
    for case in cases:
        try:
            row = run_case(
                case, retriever, chunks_by_id, bm25.idf, args, fixtures=fixtures
            )
        except RuntimeError as exc:
            print("Cannot run LLM backend: %s" % exc)
            print("Hint: fill .env, or run with --backend extractive.")
            raise SystemExit(2)
        rows.append(row)
        mark = "✓" if row["pass"] else "✗"
        print(
            "%-4s %s  facts=%-5s grounded=%-5s recall=%.2f  %s"
            % (
                row["id"],
                mark,
                "ok" if row["facts_ok"] else "miss",
                "ok" if row["check"]["valid"] else "fail",
                row["source_recall"],
                "" if row["pass"] else "see failures",
            )
        )

    summary = summarize(rows)
    print()
    print_summary(summary)
    print_failures(rows)

    out_path = Path(args.out) if args.out else (
        ROOT / "reports" / ("answer-%s-%s.json" % (args.backend, args.reranker))
    )
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": str(args.index),
        "retriever": args.retriever,
        "reranker": args.reranker,
        "candidate_k": args.candidate_k,
        "top_k": args.top_k,
        "backend": args.backend,
        "summary": summary,
        "rows": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("\nreport: %s" % out_path)


if __name__ == "__main__":
    main()
