"""End-to-end CLI tests (subprocess against the installed package).

Covers the new multi-period commands (schedule, resilience, mp-benchmark) plus
backward compatibility of the pre-existing single-period commands.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import SOLVER_MSG, SOLVER_OK

ROOT = Path(__file__).resolve().parents[1]
needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")


def _run(*args: str, expect_ok: bool = True) -> str:
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-m", "gridmind", *args],
                          capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
    if expect_ok:
        assert proc.returncode == 0, f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return proc.stdout


def _run_expect_fail(*args: str) -> str:
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-m", "gridmind", *args],
                          capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
    assert proc.returncode != 0, f"expected non-zero exit, got 0\nstdout:\n{proc.stdout}"
    return proc.stdout


# ------------------------------------------------------- backward compatible ----
@needs_solver
def test_check_solver_lists_candidates():
    out = _run("check-solver")
    assert "solver=" in out
    assert "available=" in out
    assert "glpk" in out and "cbc" in out and "appsi_highs" in out


def test_scenarios_lists_single_and_multi_period():
    out = _run("scenarios")
    assert "normal" in out and "peak_demand" in out
    assert "-- multi-period (horizon) scenarios --" in out
    assert "h-normal" in out and "h-compound" in out


@needs_solver
def test_solve_backward_compatible_output():
    out = _run("solve", "--scenario", "normal")
    assert "status=optimal" in out
    assert "objective" in out


@needs_solver
def test_solve_json_output(tmp_path):
    target = tmp_path / "solve.json"
    _run("solve", "--scenario", "normal", "--json", str(target))
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["result"]["status"] == "optimal"
    assert data["result"]["objective"] is not None


def test_solve_missing_scenario_fails_cleanly():
    proc = subprocess.run([sys.executable, "-m", "gridmind", "solve", "--scenario", "no-such-scenario"],
                          capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert proc.returncode != 0
    combined = (str(proc.stdout) + str(proc.stderr)).lower()
    assert "unknown" in combined or "no such" in combined or proc.returncode == 2


# ------------------------------------------------------------- new commands ----
@needs_solver
def test_schedule_h_normal_reports_cost_and_validation():
    out = _run("schedule", "--scenario", "h-normal")
    assert "status=optimal" in out
    assert "validation" in out.lower()
    assert "operating cost" in out.lower() or "operating_cost" in out.lower()
    assert "unmet" in out.lower()


@needs_solver
def test_schedule_strict_on_feeder_loss_is_infeasible_or_fails_cleanly():
    proc = subprocess.run([sys.executable, "-m", "gridmind", "schedule",
                           "--scenario", "h-feeder-loss", "--strict"],
                          capture_output=True, text=True, cwd=ROOT, timeout=300)
    assert proc.returncode != 0
    combined = (proc.stdout + proc.stderr).lower()
    assert "infeasible" in combined or "error" in combined


@needs_solver
def test_schedule_json_file(tmp_path):
    target = tmp_path / "schedule.json"
    _run("schedule", "--scenario", "h-normal", "--json", str(target))
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["result"]["status"] == "optimal"
    assert data["result"]["validation"]["passed"] is True
    assert data["scenario"] == "h-normal"


@needs_solver
def test_schedule_accepts_horizon_and_network_override(tmp_path):
    out = _run("schedule", "--scenario", "h-normal",
               "--horizon", str(ROOT / "data" / "sample_horizon.json"),
               "--network", str(ROOT / "data" / "sample_network.json"))
    assert "status=optimal" in out


@needs_solver
def test_resilience_writes_csv_and_json(tmp_path):
    out = _run("resilience", "--scenario", "h-normal", "--out", str(tmp_path))
    csv_path = tmp_path / "resilience.csv"
    json_path = tmp_path / "resilience.json"
    assert csv_path.exists() and json_path.exists()
    with open(csv_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2  # with_storage + without_storage
    assert {r["storage"] for r in rows} == {"with_storage", "without_storage"}
    assert all(r["status"] == "optimal" for r in rows)
    assert "wrote" in out


@needs_solver
def test_mp_benchmark_writes_csv_and_summary(tmp_path):
    target = tmp_path / "mp.csv"
    out = _run("mp-benchmark", "--n", "2", "--seed", "7", "--out", str(target))
    assert target.exists()
    summary_path = target.with_suffix(".summary.json")
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["rows"] == 7 + 2  # 7 core + 2 random
    assert "status_counts" in summary
    with open(target, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 7 + 2
    assert all(r["status"] == "optimal" for r in rows)
    assert all(r["validation_passed"] == "True" for r in rows)
    assert "horizon scenarios" in out


@needs_solver
def test_experiment_writes_artefacts_and_is_reproducible(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    args = ("experiment", "--seed", "31", "--seed", "32", "--scenarios", "10")
    out = _run(*args, "--out", str(out_a))
    assert "stage 5A comparative benchmark" in out
    for name in ("experiment_config.json", "per_scenario_metrics.csv",
                 "aggregate_summary.json", "README.txt"):
        assert (out_a / name).exists(), name
    assert (out_a / "runs" / "seed_31.json").exists()

    summary = json.loads((out_a / "aggregate_summary.json").read_text(encoding="utf-8"))
    for policy in ("deterministic", "risk_neutral", "risk_aware"):
        assert policy in summary["aggregate"]["policies"]
    assert "pairs" in summary["comparisons"]

    # Rerun with a different output dir; decision-relevant numbers must match.
    _run(*args, "--out", str(out_b))
    assert (out_a / "per_scenario_metrics.csv").read_text(encoding="utf-8") == \
        (out_b / "per_scenario_metrics.csv").read_text(encoding="utf-8")
    assert (out_a / "experiment_config.json").read_text(encoding="utf-8") == \
        (out_b / "experiment_config.json").read_text(encoding="utf-8")


@needs_solver
def test_control_writes_artefact_and_is_reproducible(tmp_path):
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    args = ("control", "--lookahead", "4", "--seed", "77")
    out = _run(*args, "--out", str(out_a))
    assert "rolling-horizon control" in out
    assert "difference (controller - baseline)" in out
    rec = json.loads((out_a / "control.json").read_text(encoding="utf-8"))
    assert rec["schema_version"] >= 2
    ctrl = rec["controller"]
    assert ctrl["lookahead"] == 4
    assert ctrl["status"] in ("completed", "completed_with_recovery")
    assert ctrl["periods_executed"] == 24
    assert ctrl["solver"]
    assert ctrl["realized_physical_feasible"] is True
    assert rec["baseline"]["realized_physical_feasible"] is True
    assert rec["baseline"]["redispatch_applied"] is True
    assert len(ctrl.get("realized_demand_mw", [])) == 24
    assert ctrl.get("problem_sha256")
    assert rec.get("provenance") or ctrl.get("realized_conditions_sha256")
    # Reproducible: same command into a different dir yields identical decisions.
    _run(*args, "--out", str(out_b))
    assert _strip_timings(rec) == _strip_timings(
        json.loads((out_b / "control.json").read_text(encoding="utf-8")))


def _strip_timings(obj):
    if isinstance(obj, dict):
        return {k: _strip_timings(v) for k, v in obj.items()
                if k not in ("runtime_seconds", "started_at", "runtime_s",
                             "mean_solve_seconds", "max_solve_seconds")}
    if isinstance(obj, list):
        return [_strip_timings(v) for v in obj]
    return obj

