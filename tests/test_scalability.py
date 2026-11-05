"""Stage 5E scalability/memory guardrail classification."""
from __future__ import annotations

import io
import os

import pytest

from benchmarks import progress
from benchmarks import scalability as sc


def _run(size, wall, rss, *, status="ok", solver_status="optimal", validation=True):
    return {"size": size, "wall_seconds": wall, "peak_rss_mb": rss,
            "status": status, "solver_status": solver_status,
            "validation_passed": validation, "objective_usd": 1.0}


def _worker_run(wall, rss, *, objective=1.0, solver="appsi_highs", status="ok"):
    return {
        "schema_version": sc.SCALABILITY_SCHEMA_VERSION,
        "kind": "gridmind_scalability_worker",
        "workload": "dispatch_lp",
        "size": 10,
        "status": status,
        "error": None if status == "ok" else "boom",
        "solver": solver,
        "solver_status": "optimal" if status == "ok" else None,
        "solver_seconds": wall / 2,
        "solver_samples_seconds": [wall / 2],
        "objective_usd": objective,
        "validation_passed": status == "ok",
        "wall_seconds": wall,
        "wall_samples_seconds": [wall],
        "rss_samples_mb": [rss],
        "peak_rss_mb": rss,
        "started_at_utc": "2026-01-01T00:00:00Z",
        "finished_at_utc": "2026-01-01T00:00:01Z",
    }


def test_combine_replicates_uses_pooled_median_and_samples():
    combined = sc.combine_replicate_runs(
        "dispatch_lp", 10,
        [_worker_run(1.0, 60.0), _worker_run(2.0, 61.0), _worker_run(3.0, 62.0)])
    assert combined["repetitions"] == 3
    assert combined["wall_seconds"] == pytest.approx(2.0)
    assert combined["wall_samples_seconds"] == [1.0, 2.0, 3.0]
    assert combined["objective_usd"] == pytest.approx(1.0)
    assert combined["status"] == "ok"


def test_combine_replicates_flags_objective_drift():
    combined = sc.combine_replicate_runs(
        "dispatch_lp", 10,
        [_worker_run(1.0, 60.0), _worker_run(1.0, 60.0, objective=2.0)])
    assert combined["status"] == "error"
    assert combined["objective_usd"] is None
    assert combined["objectives_usd"] == [1.0, 2.0]
    assert "objective drift" in combined["error"]


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


def _fake_worker(kind, size, repetitions, out_dir, timeout_seconds, probe_name=None):
    return {
        "schema_version": sc.SCALABILITY_SCHEMA_VERSION,
        "kind": "gridmind_scalability_worker",
        "workload": kind,
        "size": int(size),
        "repetitions": int(repetitions),
        "status": "ok",
        "started_at_utc": "2026-01-01T00:00:00Z",
        "finished_at_utc": "2026-01-01T00:00:01Z",
        "solver": "appsi_highs",
        "solver_status": "optimal",
        "solver_seconds": 0.05,
        "objective_usd": 1.0,
        "objectives_usd": [1.0],
        "validation_passed": True,
        "wall_seconds": 0.1,
        "wall_samples_seconds": [0.1] * max(1, int(repetitions)),
        "rss_samples_mb": [60.0] * max(2, int(repetitions)),
        "peak_rss_mb": 60.0,
        "error": None,
    }


def test_run_scalability_progress_reports_sizes_repetitions_and_artifact(
        tmp_path, monkeypatch):
    stream = io.StringIO()
    reporter = progress.ProgressReporter(stream=stream)
    monkeypatch.setattr(sc, "_run_child", _fake_worker)
    payload = sc.run_scalability(
        tmp_path / "run", lp_sizes=(4, 8), mip_sizes=(2, 4),
        leak_repetitions=2, leak_size=2, repetitions=2, progress=reporter)
    text = stream.getvalue()
    for fragment in (
            "scalability collection starting:",
            "scalability family dispatch_lp:",
            "scalability dispatch_lp size=4 repetition=1/2 starting",
            "scalability dispatch_lp size=4 repetition=2/2 completed",
            "scalability schedule_mip size=4 repetition=1/2 starting",
            "scalability leak probe size=2 repetitions=2 starting",
            "scalability leak probe size=2 repetitions=2 completed",
            "artifact written:",
            "scalability summary passed="):
        assert fragment in text
    assert payload["summary"]["passed"] is True
    assert (tmp_path / "run" / "scalability.json").exists()


def test_run_scalability_interruption_reports_size_and_writes_no_artifact(
        tmp_path, monkeypatch):
    stream = io.StringIO()
    reporter = progress.ProgressReporter(stream=stream)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(sc, "_run_child", interrupted)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        sc.run_scalability(out, lp_sizes=(4, 8), mip_sizes=(2, 4),
                           leak_repetitions=2, leak_size=2, progress=reporter)
    text = stream.getvalue()
    assert "scalability dispatch_lp size=4 repetition=1/1 starting" in text
    assert ("interrupted during scalability dispatch_lp size=4 repetition=1/1"
            in text)
    assert not (out / "scalability.json").exists()
