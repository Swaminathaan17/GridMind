"""Stage 5E qualification harness: manifests, determinism and gate decisions."""
from __future__ import annotations

import json

import pytest

from benchmarks import qualification as q
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _rep(*, wall=1.0, solver=0.1, rss=20.0, digest=HASH_A, status="ok",
         solver_name="appsi_highs", description="synthetic workload"):
    return {
        "schema_version": q.SCHEMA_VERSION,
        "kind": "gridmind_workload_result",
        "workload": "smoke",
        "description": description,
        "status": status,
        "parameters": {"seed": 42},
        "solver": solver_name,
        "solver_identity": {"solver": solver_name, "version": "1.0", "available": True,
                            "supports_mip": True},
        "solver_seconds": solver,
        "wall_seconds": wall,
        "peak_rss_mb": rss,
        "decision_sha256": digest if status == "ok" else None,
        "config_sha256": HASH_C,
        "input_hashes": {"data/sample_network.json": HASH_C},
        "started_at_utc": "2026-01-01T00:00:00Z",
        "finished_at_utc": "2026-01-01T00:00:01Z",
        "artifacts": {},
        "summary": {},
        "error": None if status == "ok" else "boom",
    }


def _provenance():
    return {
        "schema_version": q.PROVENANCE_SCHEMA_VERSION,
        "harness_version": q.HARNESS_VERSION,
        "legacy": False,
        "started_at_utc": "2026-01-01T00:00:00Z",
        "finished_at_utc": "2026-01-01T00:00:01Z",
        "git": {"commit": "d" * 40, "branch": "test", "dirty": False},
        "environment_sha256": HASH_C,
        "input_hashes": {"data/sample_network.json": HASH_C},
        "solver_identity": [
            {"solver": "appsi_highs", "version": "1.0", "available": True,
             "supports_mip": True}],
    }


def _measurement(workloads):
    return {
        "schema_version": q.SCHEMA_VERSION,
        "kind": q.MEASUREMENT_KIND,
        "generated_at_utc": "2026-01-01T00:00:01Z",
        "environment": {"python": {"version": "3.11.0"}},
        "provenance": _provenance(),
        "harness": {"repetitions": 3},
        "workloads": workloads,
    }


def _aggregate(name="smoke", reps=None):
    return q.aggregate_repetitions(name, reps or [_rep(wall=1.0), _rep(wall=1.1),
                                                  _rep(wall=0.9)])


def _baseline(tmp_path, workloads):
    path = tmp_path / "baseline.json"
    return q.build_baseline(_measurement(workloads), path)


def test_canonical_hash_ignores_timings_and_rounds():
    first = {"status": "optimal", "runtime_seconds": 1.2,
             "nested": {"runtime_s": 3.4, "value": 1.23456789, "other": 2.0}}
    second = {"status": "optimal", "runtime_seconds": 99.0,
              "nested": {"runtime_s": 0.1, "value": 1.23456789, "other": 2.0}}
    assert q.sha256_canonical(first) == q.sha256_canonical(second)
    assert "runtime_seconds" not in q.canonical_json(first)


def test_canonical_hash_rejects_nonfinite_values():
    with pytest.raises(ValueError):
        q.sha256_canonical({"cost": float("nan")})
    with pytest.raises(ValueError):
        q.sha256_canonical({"cost": float("inf")})


def test_environment_manifest_has_required_provenance():
    env = q.collect_environment()
    assert env["schema_version"] == q.SCHEMA_VERSION
    assert env["python"]["version"]
    assert env["python"]["implementation"]
    assert env["platform"]["system"]
    assert isinstance(env["packages"], dict)
    assert "pyomo" in env["packages"]
    assert isinstance(env["solvers"], list)
    assert "commit" in env["git"] and "dirty" in env["git"]


def test_peak_rss_is_reported_when_supported():
    value = q.peak_rss_mb()
    assert value is None or value > 0


def test_aggregate_marks_identical_hashes_deterministic():
    record = _aggregate()
    assert record["deterministic"] is True
    assert record["decision_sha256"] == HASH_A
    assert record["wall_seconds"]["median"] == pytest.approx(1.0)
    assert record["wall_seconds"]["max"] == pytest.approx(1.1)


def test_aggregate_marks_hash_drift_nondeterministic():
    record = q.aggregate_repetitions("smoke", [_rep(digest=HASH_A), _rep(digest=HASH_B)])
    assert record["deterministic"] is False
    assert record["decision_sha256"] is None


def test_baseline_thresholds_are_derived_from_measured_statistics(tmp_path):
    reps = [_rep(wall=2.0, solver=1.0, rss=10.0),
            _rep(wall=3.0, solver=2.0, rss=20.0),
            _rep(wall=4.0, solver=3.0, rss=30.0)]
    baseline = _baseline(tmp_path, {"smoke": _aggregate(reps=reps)})
    thresholds = baseline["workloads"]["smoke"]["thresholds"]
    assert thresholds["wall_seconds_max"] == pytest.approx(4.5)
    assert thresholds["solver_seconds_max"] == pytest.approx(3.0)
    assert thresholds["peak_rss_mb_max"] == pytest.approx(53.5)
    assert baseline["threshold_policy"]["wall_headroom_factor"] == q.WALL_HEADROOM


def test_build_baseline_refuses_to_overwrite(tmp_path):
    measurement = _measurement({"smoke": _aggregate()})
    out = tmp_path / "baseline.json"
    q.build_baseline(measurement, out)
    with pytest.raises(q.QualificationError, match="refusing to overwrite"):
        q.build_baseline(measurement, out)


def test_build_baseline_rejects_nondeterministic_measurements(tmp_path):
    record = q.aggregate_repetitions("smoke", [_rep(digest=HASH_A), _rep(digest=HASH_B)])
    with pytest.raises(q.QualificationError, match="not deterministic"):
        q.build_baseline(_measurement({"smoke": record}), tmp_path / "baseline.json")


def test_gate_passes_within_thresholds(tmp_path):
    baseline = _baseline(tmp_path, {"smoke": _aggregate()})
    measured = _measurement({"smoke": _aggregate(
        reps=[_rep(wall=1.2), _rep(wall=1.15), _rep(wall=1.1)])})
    result = q.evaluate_gate(measured, baseline)
    assert result["passed"] is True
    assert result["failures"] == []


def test_gate_fails_on_wall_regression(tmp_path):
    baseline = _baseline(tmp_path, {"smoke": _aggregate()})
    measured = _measurement({"smoke": _aggregate(
        reps=[_rep(wall=5.0), _rep(wall=5.0), _rep(wall=5.0)])})
    result = q.evaluate_gate(measured, baseline)
    assert result["passed"] is False
    assert any(f["metric"] == "wall_seconds" for f in result["failures"])


def test_gate_fails_on_decision_hash_drift(tmp_path):
    baseline = _baseline(tmp_path, {"smoke": _aggregate()})
    measured = _measurement({"smoke": _aggregate(
        reps=[_rep(digest=HASH_B), _rep(digest=HASH_B), _rep(digest=HASH_B)])})
    result = q.evaluate_gate(measured, baseline)
    assert result["passed"] is False
    assert any(f["metric"] == "decision_sha256" for f in result["failures"])


def test_gate_fails_when_baseline_workload_is_missing(tmp_path):
    baseline = _baseline(tmp_path, {
        "smoke": _aggregate(),
        "benchmark": q.aggregate_repetitions("benchmark", [_rep()]),
    })
    measured = _measurement({"smoke": _aggregate()})
    result = q.evaluate_gate(measured, baseline)
    assert result["passed"] is False
    assert any(f["workload"] == "benchmark" and f["metric"] == "present"
               for f in result["failures"])


def test_gate_rejects_invalid_baseline(tmp_path):
    bad = {"schema_version": q.SCHEMA_VERSION, "kind": q.BASELINE_KIND,
           "workloads": {"smoke": {"thresholds": {"wall_seconds_max": -1}}}}
    assert q.validate_baseline(bad)
    with pytest.raises(q.QualificationError, match="invalid baseline"):
        q.evaluate_gate(_measurement({"smoke": _aggregate()}), bad)


def test_measure_refuses_existing_directory(tmp_path):
    with pytest.raises(q.QualificationError, match="refusing to overwrite"):
        q.measure(tmp_path, ["smoke"], repetitions=1)


def test_measure_rejects_invalid_repetitions(tmp_path):
    with pytest.raises(q.QualificationError, match="repetitions"):
        q.measure(tmp_path / "run", ["smoke"], repetitions=0)


def test_measure_rejects_unknown_workload(tmp_path):
    with pytest.raises(q.QualificationError, match="unknown workloads"):
        q.measure(tmp_path / "run", ["not-a-workload"], repetitions=1)


def test_run_workload_rejects_unknown_name(tmp_path):
    with pytest.raises(q.QualificationError, match="unknown workload"):
        q.run_workload("not-a-workload", tmp_path)


def test_run_workload_reports_missing_solver(tmp_path, monkeypatch):
    import gridmind.solver

    monkeypatch.setattr(gridmind.solver, "solver_available", lambda: (False, "no solver"))
    result = q.run_workload("smoke", tmp_path)
    assert result["status"] == "error"
    assert "solver unavailable" in result["error"]
    assert (tmp_path / "workload_result.json").exists()


def test_measurement_without_provenance_is_rejected():
    measurement = _measurement({"smoke": _aggregate()})
    measurement.pop("provenance")
    errors = q.validate_measurement(measurement)
    assert any("provenance" in error for error in errors)


def test_measurement_with_malformed_hashes_is_rejected():
    measurement = _measurement({"smoke": _aggregate()})
    measurement["provenance"]["input_hashes"]["data/sample_network.json"] = "not-a-hash"
    measurement["workloads"]["smoke"]["results"][0]["config_sha256"] = "x"
    errors = q.validate_measurement(measurement)
    assert any("input_hashes" in error for error in errors)
    assert any("config_sha256" in error for error in errors)


def test_workload_result_requires_timestamps_and_solver_identity():
    rep = _rep()
    rep.pop("started_at_utc")
    rep["solver_identity"] = None
    errors = q.validate_measurement(_measurement({"smoke": _aggregate(reps=[rep])}))
    assert any("started_at_utc" in error for error in errors)
    assert any("solver_identity" in error for error in errors)


def test_baseline_provenance_is_preserved_from_measurements(tmp_path):
    baseline = _baseline(tmp_path, {"smoke": _aggregate()})
    assert baseline["provenance"] == _provenance()
    assert baseline["workloads"]["smoke"]["parameters_sha256"]


@needs_solver
def test_smoke_workload_is_reproducible(tmp_path):
    first = q.run_workload("smoke", tmp_path / "first")
    second = q.run_workload("smoke", tmp_path / "second")
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert first["decision_sha256"] == second["decision_sha256"]
    assert json.loads((tmp_path / "first" / "workload_result.json").read_text(
        encoding="utf-8"))["decision_sha256"] == first["decision_sha256"]
