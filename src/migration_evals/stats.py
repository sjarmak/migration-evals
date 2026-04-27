"""Confidence-interval helpers for the funnel report (hd9).

* :func:`wilson_interval` — closed-form Wilson score 95% CI for a
  binomial proportion, used to bound each per-tier pass rate without
  pulling in scipy.
* :func:`bootstrap_proportion_ci` — percentile bootstrap 95% CI for the
  mean of a 0/1 sequence, used for the cumulative pass-through rate.

Both are stdlib-only and deterministic given the seed. The Wilson form
matches the version cited in ``docs/PRD.md`` for "defensible, calibrated
success-rate numbers ... at 95% CI".
"""

from __future__ import annotations

import math
import random
from typing import Sequence

# 97.5 percentile of the standard normal distribution. Hard-coded so we
# don't drag in scipy for a single constant.
Z_95 = 1.959963984540054


def wilson_interval(
    k: int, n: int, *, z: float = Z_95
) -> tuple[float, float]:
    """Return the Wilson score interval ``(lo, hi)`` for ``k`` of ``n``.

    Returns ``(0.0, 0.0)`` when ``n <= 0`` so callers can render a
    placeholder rather than special-casing every empty tier. The output
    is clamped to ``[0, 1]`` because the Wilson formula occasionally
    drifts a hair past the unit interval at extreme proportions, and a
    rate CI of ``[-0.0001, 0.5]`` confuses readers more than it helps.
    """
    if n <= 0:
        return (0.0, 0.0)
    p_hat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    half = (
        z * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    ) / denom
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return (lo, hi)


def bootstrap_proportion_ci(
    successes: Sequence[bool],
    *,
    n_bootstrap: int = 10_000,
    seed: int = 42,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean of a 0/1 sequence.

    ``successes`` is the per-trial pass/fail indicator for the rate of
    interest (e.g. "did this trial make it past tier T"). The bootstrap
    resamples with replacement ``n_bootstrap`` times and returns the
    ``(alpha/2, 1-alpha/2)`` percentiles where ``alpha = 1 - confidence``.

    Returns ``(0.0, 0.0)`` for an empty sequence, mirroring
    :func:`wilson_interval` so call sites can treat the two
    interchangeably.
    """
    n = len(successes)
    if n == 0:
        return (0.0, 0.0)
    flags = [1 if bool(s) else 0 for s in successes]
    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_bootstrap):
        total = 0
        for _ in range(n):
            total += flags[rng.randrange(n)]
        boot_means.append(total / n)
    boot_means.sort()
    alpha = 1.0 - confidence
    lo = _percentile(boot_means, (alpha / 2.0) * 100.0)
    hi = _percentile(boot_means, (1.0 - alpha / 2.0) * 100.0)
    return (lo, hi)


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile on a pre-sorted, non-empty list."""
    if not sorted_vals:
        raise ValueError("_percentile: empty input")
    if pct <= 0.0:
        return sorted_vals[0]
    if pct >= 100.0:
        return sorted_vals[-1]
    n = len(sorted_vals)
    rank = (pct / 100.0) * (n - 1)
    lo_idx = int(math.floor(rank))
    hi_idx = int(math.ceil(rank))
    if lo_idx == hi_idx:
        return sorted_vals[lo_idx]
    frac = rank - lo_idx
    return sorted_vals[lo_idx] * (1 - frac) + sorted_vals[hi_idx] * frac


__all__ = ["Z_95", "bootstrap_proportion_ci", "wilson_interval"]
