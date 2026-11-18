"""Small statistical helpers used by the eval report.

The point is not advanced statistics; it is to stop reading a 2-point
difference on 40 cases as if it were a fact.
"""
import math


def wilson_interval(successes: int, n: int, z: float = 1.96):
    """95% Wilson score interval for a binomial proportion.

    Unlike the normal approximation, Wilson behaves sensibly at 0/40 and 40/40.
    """
    if n <= 0:
        return (0.0, 1.0)
    if successes < 0 or successes > n:
        raise ValueError("successes must be between 0 and n")
    p = successes / float(n)
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    margin = (z * math.sqrt(p * (1.0 - p) / n + (z * z) / (4.0 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def pass_hat_k(p: float, k: int) -> float:
    """Probability that all k independent trials succeed."""
    if k <= 0:
        raise ValueError("k must be >= 1")
    return float(p) ** k


def pass_at_k(p: float, k: int) -> float:
    """Probability that at least one of k independent trials succeeds."""
    if k <= 0:
        raise ValueError("k must be >= 1")
    return 1.0 - (1.0 - float(p)) ** k
