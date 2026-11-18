"""Offline tests for the Lesson 0013 eval harness.

Run:
    python3 -m unittest test_eval -v

No API key, network, or third-party package is required.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

import bias
import calibration
import dataset
import eval_cli
import graders
import harness
import judge
import stats
from schema import Answer, Action, Citation, Case


BASE = Path(__file__).resolve().parent


def perfect_answer(case: Case, confidence: float = 0.82) -> Answer:
    if case.expected.abstain:
        return Answer(
            case_id=case.id,
            root_cause=None,
            confidence=0.3,
            evidence=[],
            actions=[Action("query_logs", "T0")],
            abstain=True,
            answer=case.reference_answer,
        )
    evidence_map = case.evidence_map()
    citations = [
        Citation(eid, evidence_map[eid].text[:90])
        for eid in case.expected.required_evidence
    ]
    return Answer(
        case_id=case.id,
        root_cause=case.expected.root_cause,
        confidence=confidence,
        evidence=citations,
        actions=[Action("query_logs", "T0")],
        abstain=False,
        answer=case.reference_answer,
    )


class TestDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dataset.load_cases()
        cls.by_id = {case.id: case for case in cls.cases}

    def test_loads_40_cases(self):
        self.assertEqual(len(self.cases), 40)

    def test_audit_passes(self):
        audit = dataset.audit_cases(self.cases)
        self.assertTrue(audit.passed, audit.errors)

    def test_ids_are_unique(self):
        ids = [case.id for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_abstention_count_is_8(self):
        self.assertEqual(sum(1 for case in self.cases if case.expected.abstain), 8)

    def test_regression_count_is_12(self):
        self.assertEqual(sum(1 for case in self.cases if case.split == "regression"), 12)

    def test_labels_are_balanced(self):
        counts = {}
        for case in self.cases:
            if not case.expected.abstain:
                counts[case.expected.root_cause] = counts.get(case.expected.root_cause, 0) + 1
        self.assertEqual(len(counts), 8)
        self.assertEqual(min(counts.values()), 4)
        self.assertEqual(max(counts.values()), 4)

    def test_required_evidence_exists(self):
        for case in self.cases:
            evidence_ids = {item.id for item in case.evidence}
            self.assertTrue(set(case.expected.required_evidence) <= evidence_ids, case.id)

    def test_incident_does_not_leak_label(self):
        for case in self.cases:
            if case.expected.root_cause:
                self.assertNotIn(case.expected.root_cause, case.incident, case.id)

    def test_duplicate_incident_is_an_error(self):
        extra = replace(self.cases[0], id="X01")
        audit = dataset.audit_cases(self.cases + [extra])
        self.assertFalse(audit.passed)
        self.assertTrue(any("duplicate incident" in error for error in audit.errors))

    def test_missing_required_evidence_is_an_error(self):
        case = self.cases[0]
        broken = replace(case, expected=replace(case.expected, required_evidence=["E99"]))
        audit = dataset.audit_cases([broken])
        self.assertFalse(audit.passed)
        self.assertTrue(any("does not exist" in error for error in audit.errors))

    def test_abstain_with_root_cause_is_an_error(self):
        case = next(c for c in self.cases if c.expected.abstain)
        broken = replace(case, expected=replace(case.expected, root_cause="gc_pause"))
        audit = dataset.audit_cases([broken])
        self.assertFalse(audit.passed)
        self.assertTrue(any("root_cause = null" in error for error in audit.errors))


class TestGraders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dataset.load_cases()
        cls.by_id = {case.id: case for case in cls.cases}
        cls.case = cls.by_id["E01"]
        cls.abstain_case = cls.by_id["E33"]

    def test_perfect_answer_passes_every_hard_grade(self):
        result = graders.grade_case(self.case, perfect_answer(self.case))
        self.assertTrue(result.overall, [(g.name, g.detail) for g in result.failed])

    def test_wrong_root_cause_fails(self):
        answer = replace(perfect_answer(self.case), root_cause="gc_pause")
        result = graders.grade_case(self.case, answer)
        self.assertFalse(result.overall)
        self.assertIn("root_cause", [g.name for g in result.failed])

    def test_missing_required_evidence_fails(self):
        answer = perfect_answer(self.case)
        answer = replace(answer, evidence=answer.evidence[:1])
        result = graders.grade_case(self.case, answer)
        self.assertFalse(result.overall)
        self.assertTrue(any("missing required evidence" in g.detail for g in result.failed))

    def test_bad_quote_fails_evidence_and_citation_validity(self):
        answer = perfect_answer(self.case)
        answer.evidence[0] = Citation("E1", "一句不在来源里的话")
        result = graders.grade_case(self.case, answer)
        names = {g.name for g in result.failed}
        self.assertIn("evidence", names)
        self.assertIn("citation_validity", names)

    def test_invalid_evidence_id_fails(self):
        answer = perfect_answer(self.case)
        answer.evidence.append(Citation("E99", "anything"))
        result = graders.grade_case(self.case, answer)
        self.assertIn("citation_validity", [g.name for g in result.failed])

    def test_forbidden_claim_fails(self):
        answer = perfect_answer(self.case)
        answer = replace(answer, answer=answer.answer + " 根因是上次部署。")
        result = graders.grade_case(self.case, answer)
        self.assertIn("forbidden_claims", [g.name for g in result.failed])

    def test_unsafe_action_fails(self):
        answer = perfect_answer(self.case)
        answer.actions.append(Action("restart_service", "T2"))
        result = graders.grade_case(self.case, answer)
        self.assertIn("action_safety", [g.name for g in result.failed])

    def test_unknown_tool_fails_closed(self):
        answer = perfect_answer(self.case)
        answer.actions.append(Action("kubectl_delete", None))
        result = graders.grade_case(self.case, answer)
        self.assertIn("action_safety", [g.name for g in result.failed])

    def test_tier_spoofing_fails(self):
        answer = perfect_answer(self.case)
        answer.actions.append(Action("restart_service", "T0"))
        result = graders.grade_case(self.case, answer)
        self.assertIn("action_safety", [g.name for g in result.failed])

    def test_correct_abstention_passes(self):
        result = graders.grade_case(self.abstain_case, perfect_answer(self.abstain_case))
        self.assertTrue(result.overall, [(g.name, g.detail) for g in result.failed])

    def test_guessing_on_abstention_case_fails(self):
        answer = replace(
            perfect_answer(self.abstain_case),
            abstain=False,
            root_cause="config_drift",
            answer="根因是配置漂移。建议观察。",
        )
        result = graders.grade_case(self.abstain_case, answer)
        self.assertFalse(result.overall)

    def test_abstaining_on_answerable_case_fails(self):
        answer = replace(perfect_answer(self.case), abstain=True, root_cause=None, evidence=[])
        result = graders.grade_case(self.case, answer)
        self.assertFalse(result.overall)

    def test_confidence_is_a_soft_grade(self):
        answer = replace(perfect_answer(self.case), confidence=0.99, evidence=perfect_answer(self.case).evidence[:1])
        result = graders.grade_case(self.case, answer)
        soft = [g for g in result.grades if g.name == "confidence"]
        self.assertEqual(len(soft), 1)
        self.assertFalse(soft[0].hard)
        self.assertFalse(soft[0].passed)

    def test_soft_grade_does_not_change_overall(self):
        answer = replace(perfect_answer(self.case), confidence=0.5)
        result = graders.grade_case(self.case, answer)
        # confidence is low but no hard criterion fails
        self.assertTrue(result.overall)


class TestStats(unittest.TestCase):
    def test_wilson_30_of_40(self):
        low, high = stats.wilson_interval(30, 40)
        self.assertAlmostEqual(low, 0.598, places=3)
        self.assertAlmostEqual(high, 0.858, places=3)

    def test_wilson_zero_has_zero_lower_bound(self):
        low, high = stats.wilson_interval(0, 10)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.278, places=3)

    def test_pass_hat_k(self):
        self.assertAlmostEqual(stats.pass_hat_k(0.75, 3), 0.421875)

    def test_pass_at_k(self):
        self.assertAlmostEqual(stats.pass_at_k(0.75, 3), 0.984375)


class TestCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dataset.load_cases()
        cls.by_id = {case.id: case for case in cls.cases}
        cls.examples = calibration.load_calibration(cases_by_id=cls.by_id)
        cls.report = calibration.evaluate_judge(judge.RubricJudge(), cls.examples, cls.by_id)

    def test_loads_30_examples(self):
        self.assertEqual(len(self.examples), 30)

    def test_human_fail_count(self):
        self.assertEqual(sum(1 for item in self.examples if not item.human_pass), 6)

    def test_rubric_judge_confusion_matrix(self):
        matrix = self.report.matrix
        self.assertEqual((matrix.tp, matrix.tn, matrix.fp, matrix.fn), (4, 22, 2, 2))

    def test_rubric_judge_tpr_tnr(self):
        self.assertAlmostEqual(self.report.matrix.tpr, 4 / 6.0)
        self.assertAlmostEqual(self.report.matrix.tnr, 22 / 24.0)

    def test_raw_agreement_is_not_enough(self):
        self.assertGreater(self.report.matrix.raw_agreement, 0.85)
        self.assertLess(self.report.matrix.tpr, 0.7)

    def test_always_pass_baseline_catches_nothing(self):
        matrix = self.report.always_pass
        self.assertEqual(matrix.tp, 0)
        self.assertEqual(matrix.fn, 6)
        self.assertAlmostEqual(matrix.raw_agreement, 0.8)

    def test_kappa_between_zero_and_one(self):
        self.assertGreater(self.report.matrix.kappa, 0)
        self.assertLess(self.report.matrix.kappa, 1)


class TestJudge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dataset.load_cases()
        cls.by_id = {case.id: case for case in cls.cases}
        cls.case = cls.by_id["E01"]

    def test_rubric_judge_passes_perfect_answer(self):
        verdict = judge.RubricJudge().overall(self.case, perfect_answer(self.case))
        self.assertEqual(verdict.verdict, "PASS", verdict.critique)

    def test_rubric_judge_fails_wrong_answer(self):
        answer = replace(perfect_answer(self.case), root_cause="gc_pause")
        verdict = judge.RubricJudge().overall(self.case, answer)
        self.assertEqual(verdict.verdict, "FAIL")

    def test_prompt_has_unknown_escape_hatch(self):
        prompt = judge.build_judge_prompt(self.case, perfect_answer(self.case), "root_cause")
        self.assertIn("UNKNOWN", prompt)
        self.assertIn("先写 critique", prompt)

    def test_prompt_marks_data_untrusted(self):
        prompt = judge.build_judge_prompt(self.case, perfect_answer(self.case), "evidence")
        self.assertIn("不可信数据", prompt)
        self.assertIn("<answer>", prompt)

    def test_parse_fenced_json(self):
        parsed = judge.parse_judge_response('```json\n{"critique": "ok", "verdict": "PASS"}\n```')
        self.assertEqual(parsed["verdict"], "PASS")

    def test_parse_rejects_bad_verdict(self):
        with self.assertRaises(ValueError):
            judge.parse_judge_response('{"critique": "ok", "verdict": "MAYBE"}')

    def test_llm_judge_requires_env(self):
        with self.assertRaises(ValueError):
            judge.LLMJudge(env={})

    def test_llm_judge_parses_response_without_network(self):
        class Stub(judge.LLMJudge):
            def __init__(self):
                self.name = "llm"

            def _chat(self, prompt):
                return '{"critique": "looks right", "verdict": "PASS"}'

        verdict = Stub().evaluate(self.case, perfect_answer(self.case), "root_cause")
        self.assertEqual(verdict.verdict, "PASS")
        self.assertEqual(verdict.critique, "looks right")

    def test_llm_judge_fails_closed_on_error(self):
        class Broken(judge.LLMJudge):
            def __init__(self):
                self.name = "llm"

            def _chat(self, prompt):
                raise RuntimeError("network down")

        verdict = Broken().evaluate(self.case, perfect_answer(self.case), "root_cause")
        self.assertEqual(verdict.verdict, "UNKNOWN")
        self.assertIn("network down", verdict.critique)


class TestHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dataset.load_cases()
        cls.by_id = {case.id: case for case in cls.cases}
        cls.baseline = harness.evaluate_run(
            cls.cases, harness.load_run(BASE / "data" / "runs" / "baseline.jsonl", cls.by_id))
        cls.candidate = harness.evaluate_run(
            cls.cases, harness.load_run(BASE / "data" / "runs" / "candidate.jsonl", cls.by_id))

    def test_candidate_passes_32(self):
        self.assertEqual(self.candidate.passed, 32)

    def test_baseline_passes_20(self):
        self.assertEqual(self.baseline.passed, 20)

    def test_candidate_improves_capability(self):
        self.assertGreater(
            self.candidate.slices["capability"].rate,
            self.baseline.slices["capability"].rate,
        )

    def test_candidate_regression_slice_is_not_perfect(self):
        self.assertLess(self.candidate.slices["regression"].rate, 1.0)

    def test_report_includes_ci_and_pass_hat_3(self):
        text = self.candidate.format()
        self.assertIn("95% CI", text)
        self.assertIn("pass^3", text)

    def test_compare_mentions_fixed_and_regressed(self):
        text = harness.compare_reports(self.baseline, self.candidate)
        self.assertIn("fixed:", text)
        self.assertIn("regressed:", text)
        self.assertIn("E04", text)

    def test_missing_answer_is_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.jsonl"
            answer = perfect_answer(self.cases[0])
            raw = {
                "case_id": answer.case_id,
                "root_cause": answer.root_cause,
                "confidence": answer.confidence,
                "evidence": [{"evidence_id": c.evidence_id, "quote": c.quote} for c in answer.evidence],
                "actions": [{"tool": a.tool, "tier": a.tier} for a in answer.actions],
                "abstain": answer.abstain,
                "answer": answer.answer,
            }
            path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
            run = harness.load_run(path, self.by_id)
            report = harness.evaluate_run(self.cases, run)
            self.assertEqual(report.schema_errors, 39)

    def test_invalid_answer_is_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text('{"case_id": "E01", "confidence": "high"}\n', encoding="utf-8")
            run = harness.load_run(path, self.by_id)
            report = harness.evaluate_run(self.cases, run)
            self.assertEqual(report.schema_errors, 40)


class TestBias(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dataset.load_cases()
        cls.by_id = {case.id: case for case in cls.cases}
        cls.pairs = bias.load_pairs(cases_by_id=cls.by_id)
        cls.report = bias.evaluate_pairs(cls.pairs, cls.by_id)

    def test_loads_12_pairs(self):
        self.assertEqual(len(self.pairs), 12)

    def test_position_flips(self):
        self.assertEqual(self.report.flips, 8)

    def test_a_wins_more_when_first(self):
        self.assertGreater(self.report.a_win_rate_first, self.report.a_win_rate_second)

    def test_consensus_is_conditional(self):
        self.assertEqual(self.report.stable_pairs, 4)
        self.assertAlmostEqual(self.report.consensus_agreement, 1.0)


class TestCli(unittest.TestCase):
    def test_audit_returns_zero(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(eval_cli.main(["audit"]), 0)

    def test_run_gate_fails_below_threshold(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(eval_cli.main(["run", "--run", "candidate", "--fail-under", "0.9"]), 1)

    def test_run_gate_passes_above_threshold(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(eval_cli.main(["run", "--run", "candidate", "--fail-under", "0.5"]), 0)


if __name__ == "__main__":
    unittest.main()
