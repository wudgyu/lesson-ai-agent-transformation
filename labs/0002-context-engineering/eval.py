"""Baseline vs engineered prompt evaluation for lesson 0002.

Usage:
    python3 eval.py --validate
    python3 eval.py --backend fixtures
    python3 eval.py --backend llm
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from classifier import (
    build_classifier_messages,
    classify,
    expected_match,
)
from common import (
    CATEGORIES,
    LLMConfig,
    OpenAICompatibleClient,
    PRIORITIES,
    estimate_tokens,
    parse_json_object,
    validate_ticket,
)

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases.json"
FIXTURES = ROOT / "fixtures.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(cases: List[Dict[str, Any]], fixtures: Optional[Dict[str, Any]]) -> int:
    errors: List[str] = []
    seen = set()
    for case in cases:
        cid = case.get("id", "?")
        if cid in seen:
            errors.append("%s: duplicate id" % cid)
        seen.add(cid)
        for key in (
            "id",
            "ticket",
            "expected_category",
            "expected_priority",
            "expected_needs_human",
        ):
            if key not in case:
                errors.append("%s: missing field %s" % (cid, key))
        if case.get("expected_category") not in CATEGORIES:
            errors.append("%s: invalid expected_category" % cid)
        if case.get("expected_priority") not in PRIORITIES:
            errors.append("%s: invalid expected_priority" % cid)
        if not isinstance(case.get("expected_needs_human"), bool):
            errors.append("%s: expected_needs_human must be bool" % cid)
        if fixtures is not None:
            entry = fixtures.get(cid, {})
            for version in ("baseline", "engineered"):
                if version not in entry:
                    errors.append("%s: missing %s fixture" % (cid, version))

    if errors:
        print("VALIDATION FAILED")
        for error in errors:
            print("  -", error)
        return 1
    print("OK: %d cases validated" % len(cases))
    print("  fixtures: %s" % ("present" if fixtures else "not checked"))
    return 0


def run_case(
    case: Dict[str, Any],
    version: str,
    backend: str,
    fixtures: Dict[str, Any],
    client: Optional[OpenAICompatibleClient],
) -> Dict[str, Any]:
    if backend == "fixtures":
        raw = fixtures[case["id"]][version]
    else:
        if client is None:
            raise RuntimeError("LLM client is not configured")
        raw = classify(client, version, case["ticket"]).raw

    data = parse_json_object(raw)
    errors = validate_ticket(data)
    schema_valid = not errors
    exact = schema_valid and expected_match(data, case)
    category_ok = bool(data) and data.get("category") == case["expected_category"]
    priority_ok = bool(data) and data.get("priority") == case["expected_priority"]
    needs_human_ok = (
        bool(data)
        and bool(data.get("needs_human")) == bool(case["expected_needs_human"])
    )
    messages = build_classifier_messages(version, case["ticket"])
    prompt_tokens = sum(estimate_tokens(m["content"]) for m in messages)
    output_tokens = estimate_tokens(raw)
    return {
        "id": case["id"],
        "version": version,
        "raw": raw,
        "data": data,
        "schema_valid": schema_valid,
        "errors": errors,
        "exact": exact,
        "category_ok": category_ok,
        "priority_ok": priority_ok,
        "needs_human_ok": needs_human_ok,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
    }


def summarize(rows: List[Dict[str, Any]], version: str) -> Dict[str, Any]:
    total = len(rows)
    if total == 0:
        return {}
    return {
        "version": version,
        "cases": total,
        "exact_accuracy": sum(1 for row in rows if row["exact"]) / total,
        "category_accuracy": sum(1 for row in rows if row["category_ok"]) / total,
        "priority_accuracy": sum(1 for row in rows if row["priority_ok"]) / total,
        "needs_human_accuracy": sum(1 for row in rows if row["needs_human_ok"]) / total,
        "schema_validity": sum(1 for row in rows if row["schema_valid"]) / total,
        "parse_failures": sum(1 for row in rows if row["data"] is None),
        "avg_prompt_tokens": sum(row["prompt_tokens"] for row in rows) / total,
        "avg_output_tokens": sum(row["output_tokens"] for row in rows) / total,
    }


def print_table(summaries: List[Dict[str, Any]]) -> None:
    print(
        "%-12s %7s %8s %8s %8s %9s %7s %9s"
        % (
            "version",
            "exact",
            "category",
            "priority",
            "human",
            "schema",
            "parse✗",
            "prompt_tok",
        )
    )
    print("-" * 75)
    for summary in summaries:
        print(
            "%-12s %7.3f %8.3f %8.3f %8.3f %9.3f %7d %9.0f"
            % (
                summary["version"],
                summary["exact_accuracy"],
                summary["category_accuracy"],
                summary["priority_accuracy"],
                summary["needs_human_accuracy"],
                summary["schema_validity"],
                summary["parse_failures"],
                summary["avg_prompt_tokens"],
            )
        )


def print_failures(rows: List[Dict[str, Any]], version: str) -> None:
    version_rows = [row for row in rows if row["version"] == version]
    failures = [row for row in version_rows if not row["exact"]]
    print("\nfailures · %s: %d/%d" % (version, len(failures), len(version_rows)))
    for row in failures:
        case = next(c for c in CASES_DATA if c["id"] == row["id"])
        print(
            "  %s expected=%s/%s/%s got=%s schema=%s"
            % (
                row["id"],
                case["expected_category"],
                case["expected_priority"],
                case["expected_needs_human"],
                row["data"],
                "ok" if row["schema_valid"] else "invalid",
            )
        )
        if row["errors"]:
            print("    errors:", "; ".join(row["errors"]))
        print("    raw:", row["raw"].replace("\n", " ")[:180])


CASES_DATA: List[Dict[str, Any]] = []


def main() -> None:
    global CASES_DATA
    parser = argparse.ArgumentParser(description="Prompt/context eval (lesson 0002)")
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--fixtures", default=str(FIXTURES))
    parser.add_argument("--backend", choices=["fixtures", "llm"], default="fixtures")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--case", action="append")
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    CASES_DATA = load_json(Path(args.cases))
    fixtures = load_json(Path(args.fixtures))
    if args.case:
        wanted = set(args.case)
        CASES_DATA = [case for case in CASES_DATA if case["id"] in wanted]
        if not CASES_DATA:
            parser.error("no cases matched --case")

    if args.validate:
        raise SystemExit(validate(CASES_DATA, fixtures))

    client = None
    if args.backend == "llm":
        config = LLMConfig.from_env()
        if args.model:
            config.model = args.model
        client = OpenAICompatibleClient(config)

    print("=== context engineering eval ===")
    print("backend     : %s" % args.backend)
    if args.backend == "fixtures":
        print("note        : fixtures are scripted; not a model evaluation.")
    print("cases       : %d" % len(CASES_DATA))
    print()

    all_rows: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    for version in ("baseline", "engineered"):
        rows = [
            run_case(case, version, args.backend, fixtures, client)
            for case in CASES_DATA
        ]
        all_rows.extend(rows)
        summaries.append(summarize(rows, version))

    print_table(summaries)
    print_failures(all_rows, "baseline")
    print_failures(all_rows, "engineered")

    out_path = Path(args.out) if args.out else (
        ROOT / "reports" / ("prompt-%s.json" % args.backend)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backend": args.backend,
                "summaries": summaries,
                "rows": all_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("\nreport: %s" % out_path)


if __name__ == "__main__":
    main()
