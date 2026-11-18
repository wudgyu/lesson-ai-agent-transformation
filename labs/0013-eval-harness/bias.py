"""A small, deterministic simulation of pairwise-judge bias.

This is not a real model. It exists to make two failure modes visible:
  - position bias: the same answer wins more often when it appears first
  - verbosity bias: longer answers win more often than their quality deserves

The mitigation it demonstrates is the standard one: run both A/B and B/A,
then average or require agreement.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from common import load_jsonl, pct
from graders import grade_case
from schema import Answer, Case, SchemaError, parse_answer


BASE = Path(__file__).resolve().parent
DEFAULT_PAIRS = BASE / "data" / "pairwise.jsonl"


@dataclass
class Pair:
    id: str
    case_id: str
    a: Answer
    b: Answer
    human_winner: str  # "a" or "b"


@dataclass
class BiasReport:
    pairs: int
    flips: int
    stable_pairs: int
    ab_human_agreement: float
    ba_human_agreement: float
    consensus_agreement: float
    a_win_rate_first: float
    a_win_rate_second: float
    longer_winner_rate: float
    details: List[str] = field(default_factory=list)

    def format(self) -> str:
        return "\n".join([
            "pairwise bias simulation: %d pairs" % self.pairs,
            "position flips (AB != BA): %d/%d (%s)" % (
                self.flips, self.pairs, pct(self.flips / float(self.pairs) if self.pairs else 0.0)),
            "human agreement — AB order: %s · BA order: %s" % (
                pct(self.ab_human_agreement), pct(self.ba_human_agreement)),
            "consensus: %s (stable on %d/%d pairs)" % (
                pct(self.consensus_agreement), self.stable_pairs, self.pairs),
            "A win rate when first: %s · when second: %s" % (
                pct(self.a_win_rate_first), pct(self.a_win_rate_second)),
            "longer answer wins: %s of comparisons" % pct(self.longer_winner_rate),
            "",
            "read: a judge that changes its verdict when you swap positions is measuring order, not quality.",
            "the fix in production is not a better prompt alone; it is a protocol: randomise/swap, average, and calibrate.",
        ] + self.details)


def load_pairs(path: Optional[Path] = None, cases_by_id: Optional[Dict[str, Case]] = None) -> List[Pair]:
    path = Path(path) if path is not None else DEFAULT_PAIRS
    cases_by_id = cases_by_id or {}
    pairs: List[Pair] = []
    for line_no, raw in load_jsonl(path):
        try:
            pair_id = str(raw["id"])
            case_id = str(raw["case_id"])
            a = parse_answer(raw["a"])
            b = parse_answer(raw["b"])
            human = str(raw["human_winner"]).lower()
            if human not in ("a", "b"):
                raise SchemaError(["human_winner must be 'a' or 'b'"])
        except (KeyError, SchemaError) as exc:
            raise SchemaError(["%s:%d: %s" % (path, line_no, exc)])
        if cases_by_id and case_id not in cases_by_id:
            raise SchemaError(["%s:%d: unknown case_id %s" % (path, line_no, case_id)])
        pairs.append(Pair(pair_id, case_id, a, b, human))
    if not pairs:
        raise SchemaError(["%s: no pairs found" % path])
    return pairs


class SimulatedPairwiseJudge:
    """Quality + position bonus + length bonus. Deliberately biased."""

    def __init__(self, position_bonus: float = 0.35, verbosity_bonus: float = 0.00012):
        self.position_bonus = position_bonus
        self.verbosity_bonus = verbosity_bonus

    def quality(self, case: Case, answer: Answer) -> float:
        result = grade_case(case, answer)
        hard = [grade for grade in result.grades if grade.hard]
        if not hard:
            return 0.0
        return sum(1 for grade in hard if grade.passed) / float(len(hard))

    def compare(self, case: Case, first: Answer, second: Answer) -> str:
        score_first = self.quality(case, first) + self.position_bonus + self.verbosity_bonus * len(first.answer)
        score_second = self.quality(case, second) + self.verbosity_bonus * len(second.answer)
        return "first" if score_first >= score_second else "second"


def evaluate_pairs(pairs: List[Pair], cases_by_id: Dict[str, Case]) -> BiasReport:
    judge = SimulatedPairwiseJudge()
    flips = 0
    ab_correct = 0
    ba_correct = 0
    consensus_correct = 0
    consensus_total = 0
    a_first_wins = 0
    a_second_wins = 0
    longer_wins = 0
    longer_comparisons = 0

    for pair in pairs:
        case = cases_by_id[pair.case_id]
        ab = judge.compare(case, pair.a, pair.b)  # first = a
        ba = judge.compare(case, pair.b, pair.a)  # first = b
        ab_winner = "a" if ab == "first" else "b"
        ba_winner = "b" if ba == "first" else "a"
        if ab_winner != ba_winner:
            flips += 1
        if ab_winner == pair.human_winner:
            ab_correct += 1
        if ba_winner == pair.human_winner:
            ba_correct += 1
        if ab_winner == ba_winner:
            consensus_total += 1
            if ab_winner == pair.human_winner:
                consensus_correct += 1
        if ab_winner == "a":
            a_first_wins += 1
        if ba_winner == "a":
            a_second_wins += 1
        for winner_name in (ab_winner, ba_winner):
            winner = pair.a if winner_name == "a" else pair.b
            loser = pair.b if winner_name == "a" else pair.a
            longer_comparisons += 1
            if len(winner.answer) > len(loser.answer):
                longer_wins += 1

    n = len(pairs)
    return BiasReport(
        pairs=n,
        flips=flips,
        stable_pairs=consensus_total,
        ab_human_agreement=ab_correct / float(n) if n else 0.0,
        ba_human_agreement=ba_correct / float(n) if n else 0.0,
        consensus_agreement=(consensus_correct / float(consensus_total)) if consensus_total else 0.0,
        a_win_rate_first=a_first_wins / float(n) if n else 0.0,
        a_win_rate_second=a_second_wins / float(n) if n else 0.0,
        longer_winner_rate=longer_wins / float(longer_comparisons) if longer_comparisons else 0.0,
    )
