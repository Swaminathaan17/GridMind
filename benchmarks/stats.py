"""Robust, dependency-free statistics for Stage 5E reliability evidence.

The functions in this module describe observed samples; they never discard
outliers or turn a performance threshold into a correctness condition. The
primary location estimate is the median. IQR and MAD describe robust spread,
while the sample standard deviation and coefficient of variation are reported
only as conventional context.

The median interval is distribution-free and based on order statistics of a
binomial sign test. It is available only when the observed samples support the
requested coverage, and the achieved coverage is always recorded. This avoids
implying a normal-theory interval for small or skewed performance samples.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any, Callable, Iterable, Mapping, Sequence

DEFAULT_CONFIDENCE = 0.95
DEFAULT_BOOTSTRAP_RESAMPLES = 10000
DEFAULT_BOOTSTRAP_SEED = 20261101


def clean_samples(values: Iterable[float | None]) -> list[float]:
    """Return sorted finite floats, excluding only missing/non-finite values."""
    return sorted(float(value) for value in values
                  if value is not None and math.isfinite(float(value)))


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile (Hyndman-Fan type 7)."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _binomial_cdf(n: int, k: int) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    numerator = sum(math.comb(n, index) for index in range(k + 1))
    return numerator / (2 ** n)


def median_interval(values: Sequence[float],
                    confidence: float = DEFAULT_CONFIDENCE) -> dict[str, Any] | None:
    """Distribution-free order-statistic interval for the population median."""
    ordered = clean_samples(values)
    n = len(ordered)
    if n < 2:
        return None
    selected = None
    for order in range(1, n // 2 + 1):
        achieved = 1.0 - 2.0 * _binomial_cdf(n, order - 1)
        if achieved + 1e-12 < confidence:
            break
        selected = {
            "lower": ordered[order - 1],
            "upper": ordered[n - order],
            "nominal_confidence": confidence,
            "achieved_confidence": round(achieved, 6),
            "order_statistic": order,
            "method": "distribution-free binomial order-statistic interval",
        }
    return selected


def minimum_median_interval_samples(
        confidence: float = DEFAULT_CONFIDENCE) -> int | None:
    """Smallest sample size with a defined order-statistic median interval.

    A distribution-free median interval based on a binomial sign test needs
    enough observations that the selected order statistic is not the sample
    minimum or maximum at the requested nominal coverage. For 0.95 this is six
    observations; smaller samples report no interval rather than a fabricated
    one.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    for size in range(1, 1001):
        if median_interval(list(range(size)), confidence) is not None:
            return size
    return None


def _quartiles(ordered: Sequence[float]) -> tuple[float, float]:
    if len(ordered) == 1:
        return ordered[0], ordered[0]
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
    return float(quartiles[0]), float(quartiles[2])


def outlier_summary(ordered: Sequence[float]) -> dict[str, Any]:
    """Tukey fences; flagged values are reported and never removed."""
    if not ordered:
        return {"rule": "Tukey 1.5*IQR", "removed": False, "count": 0,
                "values": [], "lower_fence": None, "upper_fence": None}
    q1, q3 = _quartiles(ordered)
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    values = [value for value in ordered if value < lower or value > upper]
    return {
        "rule": "Tukey 1.5*IQR",
        "removed": False,
        "count": len(values),
        "values": values,
        "lower_fence": round(lower, 6),
        "upper_fence": round(upper, 6),
    }


def summarize(values: Iterable[float | None],
              confidence: float = DEFAULT_CONFIDENCE) -> dict[str, Any] | None:
    """Return robust location, dispersion, interval and outlier information."""
    ordered = clean_samples(values)
    if not ordered:
        return None
    median = statistics.median(ordered)
    q1, q3 = _quartiles(ordered)
    mad = statistics.median(abs(value - median) for value in ordered)
    mean = statistics.mean(ordered)
    stdev = statistics.stdev(ordered) if len(ordered) > 1 else None
    return {
        "samples": ordered,
        "count": len(ordered),
        "median": round(median, 6),
        "min": round(ordered[0], 6),
        "max": round(ordered[-1], 6),
        "q1": round(q1, 6),
        "q3": round(q3, 6),
        "iqr": round(q3 - q1, 6),
        "mad": round(mad, 6),
        "mean": round(mean, 6),
        "stdev": None if stdev is None else round(stdev, 6),
        "cv": None if stdev is None or mean == 0 else round(stdev / mean, 6),
        "median_interval": median_interval(ordered, confidence),
        "outliers": outlier_summary(ordered),
    }


def bootstrap_interval(values: Sequence[float], statistic: Callable[[Sequence[float]], float],
                       *, resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
                       confidence: float = DEFAULT_CONFIDENCE,
                       seed: int = DEFAULT_BOOTSTRAP_SEED) -> dict[str, Any] | None:
    """Percentile bootstrap interval for a scalar statistic."""
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(clean) < 2 or resamples < 1:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        sample = [clean[rng.randrange(len(clean))] for _ in clean]
        estimate = float(statistic(sample))
        if math.isfinite(estimate):
            estimates.append(estimate)
    if not estimates:
        return None
    alpha = (1.0 - confidence) / 2.0
    return {
        "lower": round(percentile(estimates, alpha), 6),
        "upper": round(percentile(estimates, 1.0 - alpha), 6),
        "nominal_confidence": confidence,
        "resamples": resamples,
        "seed": seed,
        "method": "percentile bootstrap",
    }


def median_span(values: Sequence[float]) -> dict[str, Any] | None:
    """Spread of independent batch medians, for reproducibility reporting."""
    clean = clean_samples(values)
    if not clean:
        return None
    median = statistics.median(clean)
    span = clean[-1] - clean[0]
    return {
        "medians": clean,
        "min": clean[0],
        "max": clean[-1],
        "span": round(span, 6),
        "relative_span": None if median == 0 else round(span / median, 6),
    }


def bootstrap_groups_interval(groups: Mapping[Any, Sequence[float]],
                              statistic: Callable[[Mapping[Any, float]], float],
                              *, resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
                              confidence: float = DEFAULT_CONFIDENCE,
                              seed: int = DEFAULT_BOOTSTRAP_SEED) -> dict[str, Any] | None:
    """Percentile bootstrap for a statistic of per-group medians."""
    clean = {key: [float(value) for value in values
                   if value is not None and math.isfinite(float(value))]
             for key, values in groups.items()}
    if any(not values for values in clean.values()) or len(clean) < 2 or resamples < 1:
        return None
    rng = random.Random(seed)
    estimates = []
    for _ in range(resamples):
        medians = {}
        for key, values in clean.items():
            sample = [values[rng.randrange(len(values))] for _ in values]
            medians[key] = statistics.median(sample)
        estimate = float(statistic(medians))
        if math.isfinite(estimate):
            estimates.append(estimate)
    if not estimates:
        return None
    alpha = (1.0 - confidence) / 2.0
    return {
        "lower": round(percentile(estimates, alpha), 6),
        "upper": round(percentile(estimates, 1.0 - alpha), 6),
        "nominal_confidence": confidence,
        "resamples": resamples,
        "seed": seed,
        "method": "percentile bootstrap over per-group medians",
    }
