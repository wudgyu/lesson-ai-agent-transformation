"""60+ 个离线测试：schema、metrics、graders、pass^k、McNemar、gate。

全部不需要 API key、网络或 sleep。先让评测器自己被测试，再相信它给出的分数。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from gate import evaluate_gate, format_gate
from graders import (
    Check,
    RunGrade,
    check_efficiency,
    check_evidence,
    check_outcome,
    check_process,
    check_safety,
    grade_run,
    grade_runs,
)
from make_fixtures import TraceBuilder, good_answer
from metrics import compute_metrics, failure_category
from passk import (
    case_majority,
    compute_passk,
    majority_pass,
    mcnemar_exact,
    wilson_interval,
)
from trace_schema import (
    Case,
    Run,
    action_digest,
    load_cases,
    load_runs,
    tier_of,
)
from metrics import TraceMetrics

HERE = Path(__file__).resolve().parent
CASES = load_cases(HERE / "cases.jsonl")


def build_run(case: Case, name: str = "test", trial: int = 1) -> TraceBuilder:
    return TraceBuilder("%s-%s-t%d" % (name, case.id, trial), case.id, name, trial)


def good_trace(case: Case, include: int = 2) -> TraceBuilder:
    b = build_run(case)
    b.llm("plan")
    for idx, tool in enumerate(case.expected.required_tools):
        b.tool(tool, {}, case.tool_outputs[tool])
        if idx < include - 1:
            b.llm("observe")
    b.answer(good_answer(case))
    return b


def run_of(b: TraceBuilder) -> Run:
    return Run.from_dict(b.build())


def stub_grade(
    case_id: str,
    trial: int,
    passed: bool,
    arm: str = "test",
    safety_ok: bool = True,
    tokens: int = 1000,
) -> RunGrade:
    checks = [
        Check("safety", "ok", safety_ok, "safety ok"),
        Check("outcome", "ok", passed, "outcome"),
        Check("efficiency", "ok", passed, "efficiency"),
    ]
    return RunGrade(
        run_id="%s-%s-t%d" % (arm, case_id, trial),
        case_id=case_id,
        arm=arm,
        trial=trial,
        checks=checks,
        outcome_passed=passed,
        metrics=TraceMetrics(tokens_in=tokens, tokens_out=0),
    )


def full_grades(pass_map, arm: str, safety_case=None, tokens: int = 1000):
    grades = []
    for case_id in sorted(CASES):
        flags = pass_map.get(case_id, [True, True, True])
        for trial, flag in enumerate(flags, 1):
            safety_ok = not (case_id == safety_case and trial == 1)
            grades.append(stub_grade(case_id, trial, flag, arm=arm, safety_ok=safety_ok, tokens=tokens))
    return grades


def all_pass_map():
    return {case_id: [True, True, True] for case_id in CASES}


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class TestSchema(unittest.TestCase):
    def test_case_count_and_splits(self):
        self.assertEqual(len(CASES), 16)
        self.assertEqual(sum(1 for c in CASES.values() if c.split == "capability"), 12)
        self.assertEqual(sum(1 for c in CASES.values() if c.split == "regression"), 4)
        self.assertEqual(sum(1 for c in CASES.values() if c.expected.abstain), 2)

    def test_run_files_have_three_trials(self):
        cases = load_cases(HERE / "cases.jsonl")
        for name in ("v03-baseline.jsonl", "v04-candidate.jsonl", "v05-hotfix.jsonl"):
            runs = load_runs(HERE / "runs" / name, cases)
            self.assertEqual(len(runs), 48)
            counts = {}
            for run in runs:
                counts[run.case_id] = counts.get(run.case_id, 0) + 1
            self.assertTrue(all(count == 3 for count in counts.values()))

    def test_missing_answer_rejected(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        with self.assertRaises(ValueError):
            Run.from_dict(b.build())

    def test_unknown_step_type_rejected(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.steps.append({"i": 2, "type": "telepathy", "name": "x"})
        b.answer(good_answer(case))
        with self.assertRaises(ValueError):
            Run.from_dict(b.build())

    def test_negative_tokens_rejected(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan", tokens_in=-1)
        b.answer(good_answer(case))
        with self.assertRaises(ValueError):
            Run.from_dict(b.build())

    def test_answer_must_be_last(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.answer(good_answer(case))
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        with self.assertRaises(ValueError):
            Run.from_dict(b.build())

    def test_duplicate_run_id_rejected(self):
        case = CASES["C05"]
        b = good_trace(case)
        raw = b.build()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.jsonl"
            path.write_text(json.dumps(raw) + "\n" + json.dumps(raw) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_runs(path, CASES)

    def test_unknown_case_rejected(self):
        case = CASES["C05"]
        raw = good_trace(case).build()
        raw["case_id"] = "C99"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.jsonl"
            path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_runs(path, CASES)

    def test_duplicate_case_rejected(self):
        raw = {
            "id": "C05",
            "title": "x",
            "service": "x",
            "split": "capability",
            "incident": "x",
            "expected": {
                "root_cause": "gc_pause",
                "abstain": False,
                "required_evidence": [],
                "required_tools": [],
                "forbidden_tools": [],
                "allowed_tools": [],
                "max_tier": "T1",
            },
            "budgets": {},
            "tool_outputs": {},
            "reference_answer": "x",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps(raw) + "\n" + json.dumps(raw) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_action_digest_is_stable_and_arg_sensitive(self):
        a = action_digest("restart_service", {"service": "x", "strategy": "rolling"})
        b = action_digest("restart_service", {"strategy": "rolling", "service": "x"})
        c = action_digest("restart_service", {"service": "x", "strategy": "blue-green"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 12)

    def test_tier_of_unknown_is_t4(self):
        self.assertEqual(tier_of("get_metrics"), "T0")
        self.assertEqual(tier_of("restart_service"), "T3")
        self.assertEqual(tier_of("rm_rf"), "T4")


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


class TestMetrics(unittest.TestCase):
    def test_counts(self):
        case = CASES["C05"]
        run = run_of(good_trace(case))
        m = compute_metrics(run)
        self.assertEqual(m.tool_calls, 2)
        self.assertEqual(m.llm_turns, 2)
        self.assertEqual(m.steps, 5)
        self.assertEqual(m.tokens_total, sum(s.tokens_in + s.tokens_out for s in run.steps))

    def test_max_repeat(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        for _ in range(4):
            b.tool("query_logs", {"q": "x"}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        m = compute_metrics(run_of(b))
        self.assertEqual(m.max_repeat, 4)
        self.assertEqual(m.repeated_calls, 3)

    def test_blind_retry(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {"q": "x"}, status="error", error="timeout")
        b.tool("query_logs", {"q": "x"}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        m = compute_metrics(run_of(b))
        self.assertEqual(m.blind_retries, 1)

    def test_retry_with_changed_args_is_not_blind(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {"q": "x"}, status="error", error="timeout")
        b.tool("query_logs", {"q": "x", "since": "1h"}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        m = compute_metrics(run_of(b))
        self.assertEqual(m.blind_retries, 0)
        self.assertEqual(m.recovered_errors, 1)

    def test_write_actions(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("restart_service", {}, case.tool_outputs["restart_service"])
        b.answer(good_answer(case))
        m = compute_metrics(run_of(b))
        self.assertEqual(m.write_actions, 1)

    def test_failure_category_mapping(self):
        self.assertEqual(failure_category("unapproved_action"), "unsafe_action")
        self.assertEqual(failure_category("missing_evidence"), "verification_gap")
        self.assertEqual(failure_category("tool_loop"), "step_repetition")
        self.assertEqual(failure_category("mystery"), "other")


# ---------------------------------------------------------------------------
# outcome
# ---------------------------------------------------------------------------


class TestOutcome(unittest.TestCase):
    def test_correct_root_cause(self):
        check = check_outcome(CASES["C05"], run_of(good_trace(CASES["C05"])))
        self.assertTrue(check.passed)

    def test_wrong_root_cause(self):
        case = CASES["C05"]
        b = good_trace(case)
        b.steps[-1]["data"]["root_cause"] = "gc_pause"
        check = check_outcome(case, run_of(b))
        self.assertFalse(check.passed)
        self.assertEqual(check.code, "wrong_root_cause")

    def test_false_abstain(self):
        case = CASES["C05"]
        b = good_trace(case)
        b.steps[-1]["data"] = {"abstain": True, "root_cause": None, "confidence": 0.3, "evidence": []}
        check = check_outcome(case, run_of(b))
        self.assertEqual(check.code, "false_abstain")

    def test_correct_abstain(self):
        case = CASES["C13"]
        b = build_run(case)
        b.llm("plan")
        b.tool("get_metrics", {}, case.tool_outputs["get_metrics"])
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.answer({"abstain": True, "root_cause": None, "confidence": 0.3, "evidence": []})
        self.assertTrue(check_outcome(case, run_of(b)).passed)

    def test_should_abstain(self):
        case = CASES["C13"]
        b = build_run(case)
        b.llm("plan")
        b.tool("get_metrics", {}, case.tool_outputs["get_metrics"])
        b.answer({"abstain": False, "root_cause": "gc_pause", "confidence": 0.8, "evidence": []})
        check = check_outcome(case, run_of(b))
        self.assertEqual(check.code, "should_abstain")


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


class TestEvidence(unittest.TestCase):
    def test_required_evidence_verified(self):
        case = CASES["C05"]
        check = check_evidence(case, run_of(good_trace(case)))
        self.assertTrue(check.passed)

    def test_missing_required_evidence(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        answer = good_answer(case)
        answer["evidence"] = answer["evidence"][:1]
        b.answer(answer)
        check = check_evidence(case, run_of(b))
        self.assertEqual(check.code, "missing_evidence")

    def test_fabricated_quote(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        answer = good_answer(case)
        answer["evidence"][1]["quote"] = "这是一段模型编造的引用"
        b.answer(answer)
        check = check_evidence(case, run_of(b))
        self.assertEqual(check.code, "fabricated_evidence")

    def test_quote_must_come_from_observed_output(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {"q": "empty"}, "no marker here")
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        answer = good_answer(case)
        answer["evidence"] = [
            {"evidence_id": "E1", "quote": "Thread.State: BLOCKED"},
            {"evidence_id": "E2", "quote": "CheckoutLock.order -> inventory"},
        ]
        b.answer(answer)
        check = check_evidence(case, run_of(b))
        self.assertEqual(check.code, "evidence_source_mismatch")

    def test_unknown_evidence(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        answer = good_answer(case)
        answer["evidence"].append({"evidence_id": "E9", "quote": "something"})
        b.answer(answer)
        check = check_evidence(case, run_of(b))
        self.assertEqual(check.code, "unknown_evidence")

    def test_whitespace_and_case_normalized(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        answer = good_answer(case)
        answer["evidence"][0]["quote"] = "  " + answer["evidence"][0]["quote"].lower() + "  "
        b.answer(answer)
        self.assertTrue(check_evidence(case, run_of(b)).passed)

    def test_abstain_needs_no_evidence(self):
        case = CASES["C13"]
        b = build_run(case)
        b.llm("plan")
        b.tool("get_metrics", {}, case.tool_outputs["get_metrics"])
        b.answer({"abstain": True, "root_cause": None, "confidence": 0.3, "evidence": []})
        self.assertTrue(check_evidence(case, run_of(b)).passed)


# ---------------------------------------------------------------------------
# process
# ---------------------------------------------------------------------------


class TestProcess(unittest.TestCase):
    def test_required_tools_present(self):
        case = CASES["C05"]
        self.assertTrue(check_process(case, run_of(good_trace(case))).passed)

    def test_missing_required_tool(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        check = check_process(case, run_of(b))
        self.assertEqual(check.code, "missing_required_tool")

    def test_forbidden_tool(self):
        case = CASES["C01"]
        b = build_run(case)
        b.llm("plan")
        b.tool("get_metrics", {}, case.tool_outputs["get_metrics"])
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("restart_service", {}, "restarted")
        b.answer(good_answer(case))
        check = check_process(case, run_of(b))
        self.assertEqual(check.code, "forbidden_tool")

    def test_write_before_runbook(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("restart_service", {}, case.tool_outputs["restart_service"])
        b.answer(good_answer(case))
        check = check_process(case, run_of(b))
        self.assertEqual(check.code, "missing_runbook")

    def test_write_before_approval(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        b.tool("restart_service", {}, case.tool_outputs["restart_service"])
        b.answer(good_answer(case))
        check = check_process(case, run_of(b))
        self.assertEqual(check.code, "missing_approval_step")

    def test_write_after_runbook_and_approval(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        args = {"service": "checkout-service", "strategy": "rolling"}
        b.approval("restart_service", action_digest("restart_service", args))
        b.tool("restart_service", args, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        self.assertTrue(check_process(case, run_of(b)).passed)


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------


class TestSafety(unittest.TestCase):
    def test_allowed_tools_pass(self):
        case = CASES["C05"]
        self.assertTrue(check_safety(case, run_of(good_trace(case))).passed)

    def test_unknown_tool_fails_closed(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("delete_pod", {}, "deleted")
        b.answer(good_answer(case))
        check = check_safety(case, run_of(b))
        self.assertEqual(check.code, "unknown_tool")

    def test_tier_exceeded(self):
        case = replace(CASES["C15"], expected=replace(CASES["C15"].expected, max_tier="T2"))
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        args = {"service": "checkout-service", "strategy": "rolling"}
        b.approval("restart_service", action_digest("restart_service", args))
        b.tool("restart_service", args, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        check = check_safety(case, run_of(b))
        self.assertEqual(check.code, "tier_exceeded")

    def test_unapproved_write(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("restart_service", {}, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        check = check_safety(case, run_of(b))
        self.assertEqual(check.code, "unapproved_action")

    def test_approved_write(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        args = {"service": "checkout-service", "strategy": "rolling"}
        b.approval("restart_service", action_digest("restart_service", args))
        b.tool("restart_service", args, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        self.assertTrue(check_safety(case, run_of(b)).passed)

    def test_digest_mismatch(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        b.approval("restart_service", action_digest("restart_service", {"service": "other"}))
        b.tool("restart_service", {}, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        check = check_safety(case, run_of(b))
        self.assertEqual(check.code, "unapproved_action")

    def test_rejected_approval_executed(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        args = {"service": "checkout-service", "strategy": "rolling"}
        b.approval("restart_service", action_digest("restart_service", args), status="rejected")
        b.tool("restart_service", args, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        check = check_safety(case, run_of(b))
        self.assertEqual(check.code, "rejected_action_executed")

    def test_world_state_mismatch(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("consult_runbook", {}, case.tool_outputs["consult_runbook"])
        args = {"service": "checkout-service", "strategy": "rolling"}
        b.approval("restart_service", action_digest("restart_service", args))
        b.tool("restart_service", args, case.tool_outputs["restart_service"])
        # 故意不更新 world：轨迹说重启了，环境说没有。
        b.answer(good_answer(case))
        check = check_safety(case, run_of(b))
        self.assertEqual(check.code, "undeclared_side_effect")


# ---------------------------------------------------------------------------
# efficiency
# ---------------------------------------------------------------------------


class TestEfficiency(unittest.TestCase):
    def test_clean_run(self):
        case = CASES["C05"]
        run = run_of(good_trace(case))
        self.assertTrue(check_efficiency(case, run, compute_metrics(run)).passed)

    def test_over_step_budget(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        for _ in range(12):
            b.llm("think")
        b.answer(good_answer(case))
        run = run_of(b)
        check = check_efficiency(case, run, compute_metrics(run))
        self.assertEqual(check.code, "over_step_budget")

    def test_over_tool_budget(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        for i in range(9):
            b.tool("query_logs", {"q": str(i)}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        run = run_of(b)
        check = check_efficiency(case, run, compute_metrics(run))
        self.assertEqual(check.code, "over_tool_budget")

    def test_over_token_budget(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan", tokens_in=9000, tokens_out=1000)
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        run = run_of(b)
        check = check_efficiency(case, run, compute_metrics(run))
        self.assertEqual(check.code, "over_token_budget")

    def test_tool_loop(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        for _ in range(3):
            b.tool("query_logs", {"q": "same"}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        run = run_of(b)
        check = check_efficiency(case, run, compute_metrics(run))
        self.assertEqual(check.code, "tool_loop")

    def test_blind_retry(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {"q": "x"}, status="error", error="timeout")
        b.tool("query_logs", {"q": "x"}, case.tool_outputs["query_logs"])
        b.answer(good_answer(case))
        run = run_of(b)
        check = check_efficiency(case, run, compute_metrics(run))
        self.assertEqual(check.code, "blind_retry")


# ---------------------------------------------------------------------------
# composite
# ---------------------------------------------------------------------------


class TestComposite(unittest.TestCase):
    def test_clean_trace_passes(self):
        case = CASES["C05"]
        self.assertTrue(grade_run(case, run_of(good_trace(case))).passed)

    def test_correct_but_unsafe_fails(self):
        case = CASES["C15"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.tool("restart_service", {}, case.tool_outputs["restart_service"])
        b.world["restarts"] += 1
        b.answer(good_answer(case))
        grade = grade_run(case, run_of(b))
        self.assertTrue(grade.outcome_passed)
        self.assertFalse(grade.passed)
        self.assertEqual(grade.primary_failure, "unapproved_action")

    def test_correct_but_looping_fails(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        for _ in range(4):
            b.tool("query_logs", {"q": "same"}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        b.answer(good_answer(case))
        grade = grade_run(case, run_of(b))
        self.assertTrue(grade.outcome_passed)
        self.assertEqual(grade.primary_failure, "tool_loop")

    def test_missing_evidence_fails_even_with_correct_answer(self):
        case = CASES["C05"]
        b = build_run(case)
        b.llm("plan")
        b.tool("query_logs", {}, case.tool_outputs["query_logs"])
        b.tool("search_code", {}, case.tool_outputs["search_code"])
        answer = good_answer(case)
        answer["evidence"] = answer["evidence"][:1]
        b.answer(answer)
        grade = grade_run(case, run_of(b))
        self.assertTrue(grade.outcome_passed)
        self.assertEqual(grade.primary_failure, "missing_evidence")


# ---------------------------------------------------------------------------
# pass^k / McNemar / Wilson
# ---------------------------------------------------------------------------


class TestPassK(unittest.TestCase):
    def setUp(self):
        self.grades = []
        for trial, flag in enumerate([True, True, True], 1):
            self.grades.append(stub_grade("A", trial, flag))
        for trial, flag in enumerate([True, True, False], 1):
            self.grades.append(stub_grade("B", trial, flag))
        for trial, flag in enumerate([False, False, False], 1):
            self.grades.append(stub_grade("C", trial, flag))

    def test_pass_1_is_mean_success_rate(self):
        report = compute_passk(self.grades, 1)
        self.assertAlmostEqual(report.pass_hat_k, 5.0 / 9.0, places=6)

    def test_pass_k_unbiased_estimator(self):
        report = compute_passk(self.grades, 2)
        expected = (1.0 + 1.0 / 3.0 + 0.0) / 3.0
        self.assertAlmostEqual(report.pass_hat_k, expected, places=6)

    def test_pass_n_is_all_trials_fraction(self):
        report = compute_passk(self.grades, 3)
        self.assertAlmostEqual(report.pass_hat_k, 1.0 / 3.0, places=6)
        self.assertAlmostEqual(report.all_trials_pass_fraction, 1.0 / 3.0, places=6)

    def test_pass_at_k_differs_from_pass_k(self):
        report = compute_passk(self.grades, 3)
        self.assertAlmostEqual(report.pass_at_k, 2.0 / 3.0, places=6)
        self.assertGreater(report.pass_at_k, report.pass_hat_k)

    def test_naive_power_is_not_pass_k(self):
        report = compute_passk(self.grades, 3)
        self.assertAlmostEqual(report.naive_power, (5.0 / 9.0) ** 3, places=6)
        self.assertNotAlmostEqual(report.naive_power, report.pass_hat_k, places=3)

    def test_flake_rate(self):
        report = compute_passk(self.grades, 3)
        self.assertEqual(report.flake_cases, 1)
        self.assertAlmostEqual(report.flake_rate, 1.0 / 3.0, places=6)

    def test_insufficient_trials_raises(self):
        with self.assertRaises(ValueError):
            compute_passk(self.grades, 4)

    def test_wilson_interval_known_value(self):
        lo, hi = wilson_interval(30, 40)
        self.assertAlmostEqual(lo, 0.598, places=2)
        self.assertAlmostEqual(hi, 0.858, places=2)

    def test_majority_tie_is_conservative(self):
        two = [stub_grade("A", 1, True), stub_grade("A", 2, False)]
        self.assertFalse(majority_pass(two))
        three = [stub_grade("A", i, flag) for i, flag in enumerate([True, True, False], 1)]
        self.assertTrue(majority_pass(three))

    def test_case_majority_is_bool_map(self):
        result = case_majority(self.grades)
        self.assertEqual(result, {"A": True, "B": True, "C": False})


class TestMcNemar(unittest.TestCase):
    def test_no_discordant_pairs(self):
        result = mcnemar_exact({"A": True}, {"A": True})
        self.assertEqual(result["p_value"], 1.0)

    def test_five_baseline_only(self):
        baseline = {"C%d" % i: True for i in range(5)}
        candidate = {"C%d" % i: False for i in range(5)}
        result = mcnemar_exact(baseline, candidate)
        self.assertEqual(result["baseline_only"], 5)
        self.assertAlmostEqual(result["p_value"], 0.0625, places=6)

    def test_one_vs_nine(self):
        baseline = {"C%d" % i: (i != 0) for i in range(10)}
        candidate = {"C%d" % i: (i == 0) for i in range(10)}
        result = mcnemar_exact(baseline, candidate)
        self.assertEqual(result["baseline_only"], 9)
        self.assertEqual(result["candidate_only"], 1)
        self.assertAlmostEqual(result["p_value"], 0.0215, places=4)

    def test_direction(self):
        baseline = {"A": True, "B": True}
        candidate = {"A": False, "B": True}
        result = mcnemar_exact(baseline, candidate)
        self.assertEqual(result["baseline_only"], 1)
        self.assertEqual(result["candidate_only"], 0)


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


class TestGate(unittest.TestCase):
    def test_real_candidate_is_blocked(self):
        # 用真实 fixture：stub 只用于隔离单条规则。
        real_cases = load_cases(HERE / "cases.jsonl")
        baseline = grade_runs(
            real_cases, load_runs(HERE / "runs" / "v03-baseline.jsonl", real_cases)
        )
        candidate = grade_runs(
            real_cases, load_runs(HERE / "runs" / "v04-candidate.jsonl", real_cases)
        )
        report = evaluate_gate(baseline, candidate, real_cases, candidate_arm="v04", baseline_arm="v03")
        self.assertEqual(report.verdict, "BLOCK")
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["safety"], "BLOCK")
        self.assertEqual(levels["regression"], "BLOCK")
        self.assertEqual(levels["reliability"], "BLOCK")
        self.assertEqual(levels["cost"], "WARN")
        self.assertEqual(levels["flakiness"], "WARN")

    def test_real_hotfix_passes_with_warning(self):
        real_cases = load_cases(HERE / "cases.jsonl")
        baseline = grade_runs(
            real_cases, load_runs(HERE / "runs" / "v03-baseline.jsonl", real_cases)
        )
        candidate = grade_runs(
            real_cases, load_runs(HERE / "runs" / "v05-hotfix.jsonl", real_cases)
        )
        report = evaluate_gate(baseline, candidate, real_cases)
        self.assertEqual(report.verdict, "WARN")
        self.assertEqual(len(report.blocks), 0)
        self.assertEqual({rule.id for rule in report.warnings}, {"flakiness"})

    def test_integrity_blocks_missing_trial(self):
        baseline = full_grades(all_pass_map(), "baseline")
        candidate = full_grades(all_pass_map(), "candidate")
        candidate = [g for g in candidate if g.run_id != "candidate-C05-t3"]
        report = evaluate_gate(baseline, candidate, CASES)
        self.assertEqual(report.rules[0].id, "integrity")
        self.assertEqual(report.rules[0].level, "BLOCK")

    def test_safety_blocks(self):
        baseline = full_grades(all_pass_map(), "baseline")
        candidate = full_grades(all_pass_map(), "candidate", safety_case="C15")
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["safety"], "BLOCK")

    def test_regression_blocks(self):
        baseline = full_grades(all_pass_map(), "baseline")
        candidate = full_grades(all_pass_map(), "candidate")
        candidate = [
            stub_grade(g.case_id, g.trial, False if g.case_id == "C14" else g.passed, arm="candidate")
            for g in candidate
        ]
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["regression"], "BLOCK")

    def test_reliability_blocks(self):
        baseline = full_grades(all_pass_map(), "baseline")
        pass_map = all_pass_map()
        for case_id in sorted(CASES)[10:]:
            pass_map[case_id] = [True, True, False]
        candidate = full_grades(pass_map, "candidate")
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["reliability"], "BLOCK")

    def test_cost_warns(self):
        baseline = full_grades(all_pass_map(), "baseline", tokens=1000)
        candidate = full_grades(all_pass_map(), "candidate", tokens=1300)
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["cost"], "WARN")

    def test_cost_blocks(self):
        baseline = full_grades(all_pass_map(), "baseline", tokens=1000)
        candidate = full_grades(all_pass_map(), "candidate", tokens=2000)
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["cost"], "BLOCK")

    def test_capability_drop_blocks(self):
        baseline = full_grades(all_pass_map(), "baseline")
        pass_map = all_pass_map()
        for case_id in ["C01", "C02", "C03", "C04", "C05", "C06"]:
            pass_map[case_id] = [False, False, False]
        candidate = full_grades(pass_map, "candidate")
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["capability"], "BLOCK")

    def test_flakiness_warns(self):
        baseline = full_grades(all_pass_map(), "baseline")
        pass_map = all_pass_map()
        for case_id in ["C01", "C02", "C03"]:
            pass_map[case_id] = [True, True, False]
        candidate = full_grades(pass_map, "candidate")
        report = evaluate_gate(baseline, candidate, CASES)
        levels = {rule.id: rule.level for rule in report.rules}
        self.assertEqual(levels["flakiness"], "WARN")

    def test_format_contains_verdict(self):
        baseline = full_grades(all_pass_map(), "baseline")
        candidate = full_grades(all_pass_map(), "candidate", safety_case="C15")
        text = format_gate(evaluate_gate(baseline, candidate, CASES))
        self.assertIn("verdict: BLOCK", text)
        self.assertIn("[BLOCK]", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
