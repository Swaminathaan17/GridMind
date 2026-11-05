"""Stage 5E E9 reliability pipeline: statistics, gates and artifact schema."""
from __future__ import annotations

import io
import json
import statistics

import pytest

from benchmarks import progress
from benchmarks import qualification as q
from benchmarks import reliability, schema

HASH_A = "a" * 64
HASH_B = "b" * 64


def _baseline(wall_limit=1.0, solver_limit=1.0, rss_limit=200.0):
    return {
        "workloads": {
            "smoke": {
                "thresholds": {
                    "wall_seconds_max": wall_limit,
                    "solver_seconds_max": solver_limit,
                    "peak_rss_mb_max": rss_limit,
                }
            }
        }
    }


def _perf_record(batch, walls, *, solver="appsi_highs", digest=HASH_A,
                 warmups=(50.0,)):
    results = [
        {
            "status": "ok",
            "wall_seconds": wall,
            "solver_seconds": wall / 2,
            "peak_rss_mb": 60.0 + index,
            "decision_sha256": digest,
            "solver": solver,
        }
        for index, wall in enumerate(walls)
    ]
    warmup_results = [
        {
            "status": "ok",
            "wall_seconds": wall,
            "solver_seconds": wall / 2,
            "peak_rss_mb": 60.0,
            "decision_sha256": digest,
            "solver": solver,
        }
        for wall in warmups
    ]
    payload = {
        "harness": {
            "workloads": ["smoke"],
            "repetitions": len(walls),
            "warmup_repetitions": len(warmups),
        },
        "workloads": {
            "smoke": {
                "results": results,
                "warmups": warmup_results,
                "wall_seconds": {"median": statistics.median(walls)},
            }
        },
    }
    return {"batch": batch, "path": f"performance/batch-{batch:02d}",
            "payload": payload}


def _perf_records(batches=3, walls=(0.10, 0.11), **kwargs):
    return [_perf_record(batch, list(walls), **kwargs)
            for batch in range(1, batches + 1)]


def _scal_record(batch, *, lp_walls=None, mip_walls=None, samples=3,
                 lp_objective=100.0, mip_objective=200.0, rss=60.0,
                 leak_growth=0.5, leak_suspected=False):
    lp_walls = lp_walls or {10: 0.10, 40: 0.40}
    mip_walls = mip_walls or {8: 0.10, 24: 0.30}

    def runs(walls, objective):
        rows = []
        for size, wall in walls.items():
            rows.append({
                "size": size,
                "status": "ok",
                "solver_status": "optimal",
                "validation_passed": True,
                "solver": "appsi_highs",
                "wall_samples_seconds": [wall * (1.0 + 0.01 * i)
                                         for i in range(samples)],
                "solver_samples_seconds": [wall / 2 * (1.0 + 0.01 * i)
                                           for i in range(samples)],
                "rss_samples_mb": [rss + 0.1 * i for i in range(samples)],
                "wall_seconds": wall,
                "solver_seconds": wall / 2,
                "peak_rss_mb": rss,
                "objectives_usd": [objective],
                "objective_usd": objective,
            })
        return rows

    payload = {
        "thresholds": {
            "max_time_exponent": 3.0,
            "max_rss_growth_mb": 512.0,
            "max_rss_growth_factor": 4.0,
            "leak_tolerance_mb": 64.0,
            "leak_tolerance_fraction": 0.10,
        },
        "families": {
            "dispatch_lp": {"runs": runs(lp_walls, lp_objective)},
            "schedule_mip": {"runs": runs(mip_walls, mip_objective)},
        },
        "leak": {
            "size": 8,
            "repetitions": 5,
            "leak_suspected": leak_suspected,
            "rss_growth_mb": leak_growth,
            "rss_growth_fraction": 0.01,
        },
        "summary": {"passed": True, "checks": 0, "failures": 0},
    }
    return {"batch": batch, "path": f"scalability/batch-{batch:02d}",
            "payload": payload}


def _scal_records(batches=3, samples=3, **kwargs):
    return [_scal_record(batch, samples=samples, **kwargs)
            for batch in range(1, batches + 1)]


def _failed(checks, category):
    return [check for check in checks
            if check["category"] == category and not check["passed"]]


def test_performance_summary_uses_all_batches_and_retains_samples():
    records = _perf_records(batches=3, walls=(0.10, 0.11, 0.12))
    result = reliability.analyze_performance(records, _baseline(), 9)
    workload = result["workloads"]["smoke"]
    assert workload["summary"]["wall_seconds"]["count"] == 9
    assert workload["summary"]["wall_seconds"]["samples"] == sorted(
        [0.10, 0.11, 0.12] * 3)
    assert len(workload["batch_wall_medians"]) == 3
    assert workload["summary"]["wall_seconds"]["median_interval"] is not None
    assert _failed(result["checks"], "functional") == []
    assert _failed(result["checks"], "statistical") == []
    assert _failed(result["checks"], "performance") == []


def test_performance_gate_separates_regression_from_uncertainty():
    records = _perf_records(batches=3, walls=(5.0, 5.1, 5.2))
    result = reliability.analyze_performance(records, _baseline(wall_limit=1.0), 9)
    assert _failed(result["checks"], "statistical") == []
    performance_failures = _failed(result["checks"], "performance")
    assert {check["name"].split(":")[-1] for check in performance_failures} >= {
        "wall_median_point", "wall_median_upper_bound"}


def test_performance_with_too_few_samples_reports_no_interval():
    records = _perf_records(batches=2, walls=(0.10,))
    result = reliability.analyze_performance(records, _baseline(), 2)
    workload = result["workloads"]["smoke"]
    assert workload["summary"]["wall_seconds"]["median_interval"] is None
    assert any(check["name"] == "smoke:wall_median_interval"
               and not check["passed"] for check in result["checks"])
    assert any(check["name"] == "smoke:wall_median_upper_bound"
               and not check["passed"] for check in result["checks"])


def test_performance_flags_solver_drift():
    records = _perf_records(batches=2, walls=(0.10, 0.11))
    records[1]["payload"]["workloads"]["smoke"]["results"][0]["solver"] = "glpk"
    result = reliability.analyze_performance(records, _baseline(), 4)
    assert any(check["name"] == "smoke:solver_stable"
               and not check["passed"] for check in result["checks"])


def test_performance_flags_incomplete_batches():
    records = _perf_records(batches=1, walls=(0.10, 0.11))
    result = reliability.analyze_performance(records, _baseline(), 4)
    assert any(check["name"] == "smoke:batches_complete"
               and not check["passed"] for check in result["checks"])


def test_performance_requires_recorded_warmups():
    records = _perf_records(batches=3, walls=(0.10, 0.11), warmups=())
    result = reliability.analyze_performance(records, _baseline(), 6)
    assert any(check["name"] == "smoke:warmups_recorded"
               and not check["passed"] for check in result["checks"])


def test_performance_with_no_successful_repetitions_fails_clearly():
    records = _perf_records(batches=2, walls=(0.10,))
    for record in records:
        for result in record["payload"]["workloads"]["smoke"]["results"]:
            result["status"] = "error"
    with pytest.raises(q.QualificationError, match="no successful measured repetitions"):
        reliability.analyze_performance(records, _baseline(), 2)


def test_scalability_summary_pools_batches_and_reports_interval():
    records = _scal_records(batches=3, samples=3)
    result = reliability.analyze_scalability(records, 3)
    assert result["thresholds_consistent"] is True
    for kind in ("dispatch_lp", "schedule_mip"):
        family = result["families"][kind]
        expected = {10: 9, 40: 9} if kind == "dispatch_lp" else {8: 9, 24: 9}
        assert family["replicates_per_size"] == expected
        assert family["growth"]["time_exponent"] == pytest.approx(1.0)
        interval = family["growth"]["time_exponent_bootstrap_interval"]
        assert interval is not None
        assert interval["lower"] <= interval["upper"]
    assert _failed(result["checks"], "functional") == []
    assert _failed(result["checks"], "statistical") == []
    assert _failed(result["checks"], "scalability") == []


def test_scalability_flags_objective_drift_and_insufficient_replicates():
    drifted = _scal_records(batches=3, samples=3)
    drifted[1]["payload"]["families"]["dispatch_lp"]["runs"][0]["objectives_usd"] = [999.0]
    result = reliability.analyze_scalability(drifted, 3)
    assert any(check["name"] == "dispatch_lp:objectives_stable"
               and not check["passed"] for check in result["checks"])

    sparse = _scal_records(batches=2, samples=1)
    sparse_result = reliability.analyze_scalability(sparse, 3)
    assert any(check["name"] == "dispatch_lp:replicates_per_size"
               and not check["passed"] for check in sparse_result["checks"])


def test_scalability_flags_leak_and_missing_sizes():
    leaky = _scal_records(batches=3, samples=3, leak_suspected=True)
    result = reliability.analyze_scalability(leaky, 3)
    assert any(check["name"] == "leak:not_suspected"
               and not check["passed"] for check in result["checks"])

    broken = _scal_records(batches=3, samples=3)
    for record in broken:
        for family in record["payload"]["families"].values():
            for run in family["runs"]:
                run["status"] = "error"
                run["wall_samples_seconds"] = []
                run["wall_seconds"] = None
    with pytest.raises(q.QualificationError, match="fewer than two successful"):
        reliability.analyze_scalability(broken, 3)


def test_run_reliability_rejects_invalid_configuration(tmp_path):
    with pytest.raises(q.QualificationError, match="at least 2"):
        reliability.run_reliability(tmp_path / "a", batches=1)
    with pytest.raises(q.QualificationError, match="at least 2"):
        reliability.run_reliability(tmp_path / "b", repetitions=1)
    with pytest.raises(q.QualificationError, match="warmups"):
        reliability.run_reliability(tmp_path / "c", warmups=0)
    with pytest.raises(q.QualificationError, match="refusing to overwrite"):
        (tmp_path / "d").mkdir()
        reliability.run_reliability(tmp_path / "d")


def test_run_reliability_rejects_invalid_baseline(tmp_path):
    bad = tmp_path / "bad-baseline.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(q.QualificationError, match="invalid baseline"):
        reliability.run_reliability(tmp_path / "run", baseline_path=bad)


def test_run_reliability_end_to_end_with_controlled_collectors(tmp_path, monkeypatch):
    performance = _perf_records(batches=3, walls=(0.10, 0.11))
    scalability = _scal_records(batches=3, samples=2)
    monkeypatch.setattr(reliability, "collect_performance",
                        lambda *args, **kwargs: performance)
    monkeypatch.setattr(reliability, "collect_scalability",
                        lambda *args, **kwargs: scalability)
    payload = reliability.run_reliability(
        tmp_path / "run", performance_workloads=("smoke",), batches=3,
        repetitions=2, warmups=1, scalability_repetitions=2)
    assert payload["acceptance"]["passed"] is True
    assert payload["methodology"]["minimum_samples_for_median_interval"] == 6
    assert payload["methodology"]["measured_repetitions_per_workload"] == 6
    written = json.loads((tmp_path / "run" / "reliability.json").read_text(
        encoding="utf-8"))
    assert schema.validate_statistical_artifact(written) == []
    assert written["performance"]["workloads"]["smoke"][
        "summary"]["wall_seconds"]["count"] == 6


def test_statistical_artifact_rejects_inconsistent_methodology(tmp_path, monkeypatch):
    performance = _perf_records(batches=3, walls=(0.10, 0.11))
    scalability = _scal_records(batches=3, samples=2)
    monkeypatch.setattr(reliability, "collect_performance",
                        lambda *args, **kwargs: performance)
    monkeypatch.setattr(reliability, "collect_scalability",
                        lambda *args, **kwargs: scalability)
    payload = reliability.run_reliability(
        tmp_path / "run", performance_workloads=("smoke",), batches=3,
        repetitions=2, warmups=1, scalability_repetitions=2)
    payload["methodology"]["batches"] = 1
    payload["acceptance"]["categories"].pop("statistical")
    errors = schema.validate_statistical_artifact(payload)
    assert any("methodology.batches" in error for error in errors)
    assert any("categories" in error for error in errors)


def test_run_reliability_progress_covers_batches_analysis_and_artifact(
        tmp_path, monkeypatch):
    stream = io.StringIO()
    reporter = progress.ProgressReporter(stream=stream)
    performance = [record["payload"] for record in
                   _perf_records(batches=3, walls=(0.10, 0.11))]
    scalability = [record["payload"] for record in
                   _scal_records(batches=3, samples=2)]

    def fake_measure(directory, workloads, repetitions, timeout_seconds, *,
                     warmup_repetitions, progress=None):
        assert progress is reporter
        progress("workload smoke (1/1) starting")
        return performance.pop(0)

    def fake_scalability(directory, *, repetitions, timeout_seconds, progress=None):
        assert progress is reporter
        progress("scalability family dispatch_lp: sizes=[10, 40] repetitions=2")
        return scalability.pop(0)

    monkeypatch.setattr(reliability.q, "measure", fake_measure)
    monkeypatch.setattr(reliability.sc, "run_scalability", fake_scalability)
    payload = reliability.run_reliability(
        tmp_path / "run", performance_workloads=("smoke",), batches=3,
        repetitions=2, warmups=1, scalability_repetitions=2, progress=reporter)
    text = stream.getvalue()
    for fragment in (
            "E9 reliability run starting:",
            "performance batch 1/3 starting",
            "performance batch 3/3 completed",
            "scalability batch 1/3 starting",
            "scalability batch 3/3 completed",
            "statistical analysis starting: performance",
            "statistical analysis completed: performance",
            "statistical analysis starting: scalability",
            "statistical analysis completed: scalability",
            "artifact written:",
            "acceptance passed=True"):
        assert fragment in text
    assert payload["acceptance"]["passed"] is True
    assert (tmp_path / "run" / "reliability.json").exists()


def test_run_reliability_interruption_reports_batch_and_writes_no_artifact(
        tmp_path, monkeypatch):
    stream = io.StringIO()
    reporter = progress.ProgressReporter(stream=stream)

    def interrupted(directory, workloads, repetitions, timeout_seconds, *,
                    warmup_repetitions, progress=None):
        progress("workload smoke (1/1) starting")
        raise KeyboardInterrupt

    monkeypatch.setattr(reliability.q, "measure", interrupted)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        reliability.run_reliability(
            out, performance_workloads=("smoke",), batches=2, repetitions=2,
            warmups=1, scalability_repetitions=2, progress=reporter)
    text = stream.getvalue()
    assert "performance batch 1/2 starting" in text
    assert "interrupted during performance batch 1/2" in text
    assert "E9 reliability run interrupted" in text
    assert "artifact written:" not in text
    assert not (out / "reliability.json").exists()


def test_run_reliability_interruption_during_write_leaves_no_artifact(
        tmp_path, monkeypatch):
    stream = io.StringIO()
    reporter = progress.ProgressReporter(stream=stream)
    performance = _perf_records(batches=3, walls=(0.10, 0.11))
    scalability = _scal_records(batches=3, samples=2)
    monkeypatch.setattr(reliability, "collect_performance",
                        lambda *args, **kwargs: performance)
    monkeypatch.setattr(reliability, "collect_scalability",
                        lambda *args, **kwargs: scalability)

    def interrupted_write(path, payload):
        path.write_text("partial", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(reliability.q, "_write_json", interrupted_write)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        reliability.run_reliability(
            out, performance_workloads=("smoke",), batches=3, repetitions=2,
            warmups=1, scalability_repetitions=2, progress=reporter)
    assert not (out / "reliability.json").exists()
    assert not (out / ".reliability.json.tmp").exists()
    assert "interrupted while writing artifact" in stream.getvalue()
