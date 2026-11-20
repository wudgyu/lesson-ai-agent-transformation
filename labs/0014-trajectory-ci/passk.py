"""pass^k、pass@k、flaky case 与配对显著性。

pass^k 不是把总体通过率做 k 次方。那个公式假设每个 case 的成功概率都一样，
而且每次 trial 独立；真实 Agent 两条都不满足。正确做法是按 case 统计 trials，
再用 τ-bench 的无偏估计：E_task[C(c,k) / C(n,k)]。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple


@dataclass
class PassKReport:
    k: int
    cases: int
    trials_total: int
    pass_hat_k: float
    pass_at_k: float
    naive_power: float
    all_trials_pass_fraction: float
    all_trials_pass_ci: Tuple[float, float]
    pass_hat_ci: Tuple[float, float]
    flake_cases: int
    flake_rate: float
    counts: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    matrix: Dict[str, str] = field(default_factory=dict)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = successes / float(n)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _bootstrap_ci(values: List[float], iters: int = 2000, seed: int = 13) -> Tuple[float, float]:
    """对 case 做 bootstrap；比把 pass^k 当二项比例更诚实。"""
    if not values:
        return (0.0, 0.0)
    if all(v == values[0] for v in values):
        return (values[0], values[0])
    rng = random.Random(seed)
    n = len(values)
    samples: List[float] = []
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    lo = samples[int(0.025 * iters)]
    hi = samples[max(0, int(0.975 * iters) - 1)]
    return (lo, hi)


def _group(grades: Iterable, predicate: Callable) -> Dict[str, List[bool]]:
    grouped: Dict[str, List[bool]] = {}
    for grade in grades:
        grouped.setdefault(grade.case_id, []).append(bool(predicate(grade)))
    return grouped


def compute_passk(
    grades: Iterable,
    k: int,
    predicate: Optional[Callable] = None,
) -> PassKReport:
    """按 case 计算 pass^k 与 pass@k。

    predicate 默认用 contract pass（g.passed）；传 `lambda g: g.outcome_passed`
    可以对比“只看最终答案”的 pass^k。
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if predicate is None:
        predicate = lambda g: g.passed

    grouped = _group(grades, predicate)
    if not grouped:
        raise ValueError("no grades to evaluate")

    values: List[float] = []
    at_least_once: List[float] = []
    all_pass_flags: List[int] = []
    counts: Dict[str, Tuple[int, int]] = {}
    matrix: Dict[str, str] = {}
    total_c = 0
    total_n = 0
    flake_cases = 0

    for case_id in sorted(grouped):
        flags = grouped[case_id]
        n = len(flags)
        if n < k:
            raise ValueError("case %s has %d trials, need at least %d" % (case_id, n, k))
        c = sum(1 for flag in flags if flag)
        counts[case_id] = (c, n)
        total_c += c
        total_n += n

        denom = math.comb(n, k)
        values.append(math.comb(c, k) / float(denom))
        at_least_once.append(1.0 - math.comb(n - c, k) / float(denom))
        all_pass_flags.append(1 if c == n else 0)
        if 0 < c < n:
            flake_cases += 1
        matrix[case_id] = "".join("✓" if flag else "✗" for flag in flags)

    case_count = len(grouped)
    pass_hat = sum(values) / case_count
    pass_at = sum(at_least_once) / case_count
    all_pass = sum(all_pass_flags) / float(case_count)
    ci_low, ci_high = wilson_interval(sum(all_pass_flags), case_count)
    boot_low, boot_high = _bootstrap_ci(values)
    naive = (total_c / float(total_n)) ** k

    return PassKReport(
        k=k,
        cases=case_count,
        trials_total=total_n,
        pass_hat_k=pass_hat,
        pass_at_k=pass_at,
        naive_power=naive,
        all_trials_pass_fraction=all_pass,
        all_trials_pass_ci=(ci_low, ci_high),
        pass_hat_ci=(boot_low, boot_high),
        flake_cases=flake_cases,
        flake_rate=flake_cases / float(case_count),
        counts=counts,
        matrix=matrix,
    )


def majority_pass(grades: Iterable, predicate: Optional[Callable] = None) -> bool:
    """一个 case 的多数票结果；平票算失败（保守）。"""
    if predicate is None:
        predicate = lambda g: g.passed
    flags = [bool(predicate(g)) for g in grades]
    if not flags:
        return False
    return sum(1 for flag in flags if flag) * 2 > len(flags)


def case_majority(grades: Iterable, predicate: Optional[Callable] = None) -> Dict[str, bool]:
    """每个 case 的多数票结果；平票算失败（保守）。"""
    if predicate is None:
        predicate = lambda g: g.passed
    grouped = _group(grades, predicate)
    return {
        case_id: sum(1 for flag in flags if flag) * 2 > len(flags)
        for case_id, flags in grouped.items()
    }


def mcnemar_exact(
    baseline: Dict[str, bool], candidate: Dict[str, bool]
) -> Dict[str, float]:
    """配对二分比较的精确 McNemar 检验（双侧）。

    只看发生变化的 case：baseline-only 与 candidate-only。
    适合“同一批 case，两个版本”的场景；不要拿两个独立百分比做比较。
    """
    shared = sorted(set(baseline) & set(candidate))
    baseline_only = sum(1 for cid in shared if baseline[cid] and not candidate[cid])
    candidate_only = sum(1 for cid in shared if candidate[cid] and not baseline[cid])
    discordant = baseline_only + candidate_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = min(baseline_only, candidate_only)
        p_value = 2.0 * sum(
            math.comb(discordant, i) for i in range(tail + 1)
        ) / (2.0 ** discordant)
        p_value = min(1.0, p_value)
    return {
        "baseline_only": float(baseline_only),
        "candidate_only": float(candidate_only),
        "discordant": float(discordant),
        "p_value": p_value,
    }
