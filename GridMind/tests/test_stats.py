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


def test_clean_samples_drops_only_missing_and_nonfinite_values():
    cleaned = stats.clean_samples([3, None, float("nan"), float("inf"), 1, 2])
    assert cleaned == [1.0, 2.0, 3.0]


def test_percentile_rejects_invalid_fraction_and_empty_input():
    with pytest.raises(ValueError):
        stats.percentile([], 0.5)
    with pytest.raises(ValueError):
        stats.percentile([1.0], 1.5)


def test_minimum_median_interval_sample_size_is_explicit():
    assert stats.minimum_median_interval_samples() == 6
    assert stats.minimum_median_interval_samples(0.99) == 8
    with pytest.raises(ValueError):
        stats.minimum_median_interval_samples(1.0)


def test_median_interval_is_unavailable_below_minimum_and_defined_at_minimum():
    assert stats.median_interval([1, 2, 3, 4, 5]) is None
    interval = stats.median_interval([1, 2, 3, 4, 5, 6])
    assert interval["lower"] == 1.0
    assert interval["upper"] == 6.0
    assert interval["achieved_confidence"] == pytest.approx(0.96875)


def test_median_interval_at_fifteen_samples_uses_order_statistics():
    interval = stats.median_interval(list(range(1, 16)))
    assert interval["order_statistic"] == 4
    assert interval["lower"] == 4.0
    assert interval["upper"] == 12.0
    assert interval["achieved_confidence"] == pytest.approx(0.964844)


def test_constant_samples_have_zero_dispersion_and_stable_interval():
    summary = stats.summarize([2.0] * 8)
    assert summary["median"] == 2.0
    assert summary["iqr"] == 0.0
    assert summary["mad"] == 0.0
    assert summary["stdev"] == 0.0
    assert summary["median_interval"]["lower"] == 2.0
    assert summary["median_interval"]["upper"] == 2.0


def test_bootstrap_requires_two_samples_and_records_seed():
    assert stats.bootstrap_interval([1.0], lambda sample: sample[0]) is None
    first = stats.bootstrap_interval([1.0, 2.0, 3.0], lambda sample: sorted(sample)[1],
                                     resamples=50, seed=11)
    second = stats.bootstrap_interval([1.0, 2.0, 3.0], lambda sample: sorted(sample)[1],
                                      resamples=50, seed=11)
    assert first == second
    assert first["seed"] == 11
    assert first["nominal_confidence"] == pytest.approx(0.95)


def test_bootstrap_groups_rejects_an_empty_group():
    assert stats.bootstrap_groups_interval(
        {10: [1.0], 100: []}, lambda medians: medians[10]) is None


def test_summary_excludes_missing_values_without_deleting_outliers():
    summary = stats.summarize([1.0, None, 2.0, 3.0, 4.0, float("nan"), 100.0])
    assert summary["count"] == 5
    assert summary["samples"] == [1.0, 2.0, 3.0, 4.0, 100.0]
    assert summary["outliers"]["count"] == 1
    assert summary["outliers"]["removed"] is False
