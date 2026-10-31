"""Stage 5E scalability/memory guardrail classification."""
from __future__ import annotations

import os

import pytest

from benchmarks import scalability as sc


def _run(size, wall, rss, *, status="ok", solver_status="optimal", validation=True):
    return {"size": size, "wall_seconds": wall, "peak_rss_mb": rss,
            "status": status, "solver_status": solver_status,
            "validation_passed": validation, "objective_usd": 1.0}


def test_growth_exponent_matches_power_law():
    assert sc.growth_exponent(10, 2.0, 100, 20.0) == pytest.approx(1.0)
    assert sc.growth_exponent(10, 2.0, 40, 4.0) == pytest.approx(0.5)
    assert sc.growth_exponent(10, 0.0, 40, 4.0) is None
    assert sc.growth_exponent(10, 2.0, 10, 4.0) is None


def test_growth_guardrails_accept_linear_and_reject_pathological():
    linear = sc.assess_growth([_run(10, 1.0, 60.0), _run(100, 10.0, 100.0)])
    pathological = sc.assess_growth([_run(10, 1.0, 60.0), _run(100, 1e6, 2000.0)])
    assert all(check["passed"] for check in linear["checks"])
    assert not all(check["passed"] for check in pathological["checks"])


def test_growth_requires_successful_sizes():
    result = sc.assess_growth([_run(10, 1.0, 60.0), _run(100, None, None, status="error")])
    assert result["checks"][0]["passed"] is False


def test_leak_guardrail_detects_monotonic_growth():
    clean = sc.assess_leak({"rss_samples_mb": [60.0, 61.0, 60.5, 61.0]})
    leaky = sc.assess_leak({"rss_samples_mb": [60.0, 80.0, 100.0, 120.0]})
    assert clean["leak_suspected"] is False
    assert leaky["leak_suspected"] is True
    assert sc.assess_leak({"rss_samples_mb": [60.0]})["leak_suspected"] is True


def test_scalability_refuses_existing_output_directory(tmp_path):
    (tmp_path / "existing").mkdir()
    with pytest.raises(sc.QualificationError, match="refusing to overwrite"):
        sc.run_scalability(tmp_path / "existing")


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("GRIDMIND_RUN_PERF") != "1",
                    reason="set GRIDMIND_RUN_PERF=1 to run opt-in scalability benchmark")
def test_scalability_smoke_benchmark(tmp_path):
    payload = sc.run_scalability(
        tmp_path / "scalability", lp_sizes=(4, 8), mip_sizes=(2, 4),
        leak_repetitions=2, leak_size=2)
    assert payload["summary"]["passed"] is True
    assert payload["leak"]["leak_suspected"] is False
