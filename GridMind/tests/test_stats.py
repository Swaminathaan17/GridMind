"""Stage 5E E9 robust statistics and uncertainty reporting."""
from __future__ import annotations

import pytest

from benchmarks import stats


def test_summary_reports_robust_dispersion_and_keeps_outliers():
    summary = stats.summarize([1.0, 2.0, 3.0, 4.0, 100.0])
    assert summary["count"] == 5
    assert summary["median"] == 3.0
    assert summary["q1"] == 2.0
    assert summary["q3"] == 4.0
    assert summary["iqr"] == 2.0
    assert summary["mad"] == 1.0
    assert summary["outliers"]["count"] == 1
    assert summary["outliers"]["values"] == [100.0]
    assert summary["outliers"]["removed"] is False
    assert len(summary["samples"]) == 5
    assert summary["median_interval"] is None


def test_median_interval_uses_order_statistics_and_reports_coverage():
    interval = stats.median_interval([1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert interval["lower"] == 2.0
    assert interval["upper"] == 8.0
    assert interval["order_statistic"] == 2
    assert interval["achieved_confidence"] == pytest.approx(0.960938)
    assert interval["nominal_confidence"] == 0.95


def test_summary_requires_two_samples_for_an_interval():
    assert stats.summarize([1.0])["median_interval"] is None


def test_bootstrap_interval_is_deterministic_for_a_fixed_seed():
    values = [1.0, 1.1, 0.9, 1.05, 0.95]
    first = stats.bootstrap_interval(values, lambda sample: sum(sample) / len(sample),
                                     resamples=200, seed=7)
    second = stats.bootstrap_interval(values, lambda sample: sum(sample) / len(sample),
                                      resamples=200, seed=7)
    assert first == second
    assert first["lower"] <= first["upper"]


def test_bootstrap_groups_interval_resamples_per_group():
    groups = {10: [0.9, 1.0, 1.1], 100: [9.0, 10.0, 11.0]}

    def exponent(medians):
        return (medians[100] / medians[10]) / 10.0

    interval = stats.bootstrap_groups_interval(groups, exponent, resamples=200)
    assert interval is not None
    assert interval["lower"] <= 1.0 <= interval["upper"]


def test_median_span_reports_independent_batch_variation():
    span = stats.median_span([1.0, 1.2, 0.8])
    assert span["span"] == pytest.approx(0.4)
    assert span["relative_span"] == pytest.approx(0.4)
