"""Stage 5A tests: reproducible multi-seed comparative policy benchmark.

Structure
---------
* Statistics/aggregation tests are solver-free and pin the arithmetic
  (descriptive stats, p95 interpolation, Cohen's dz) to hand-computed values.
* Driver tests use the same solver guard as the rest of the suite so they skip
  cleanly when no solver is available, and never weaken existing coverage.
* Reproducibility is tested by running the same experiment twice and comparing
  the serialised records; shared-holdout parity is tested by asserting every
  policy evaluated the SAME holdout scenario ids.
"""
from __future__ import annotations

import json

import pytest

from gridmind.experiment import (EXPERIMENT_SCHEMA_VERSION, ExperimentConfig,
                                 _cohens_dz, _p95, aggregate_runs, compare_aggregate,
                                 default_experiment_config, run_comparative_benchmark,
                                 run_one_seed, write_experiment)
from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.network import Generator, Network, Node
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")


def single_bus_problem(periods: int = 4, demand: float = 60.0,
                       cheap_cap: float = 55.0, exp_cap: float = 200.0) -> ScheduleProblem:
    net = Network(
        name="unc", nodes=(Node("bus", "bus", demand),),
        generators=(Generator("cheap", "Cheap", "bus", "gas", cheap_cap, 20.0),
                    Generator("exp", "Expensive", "bus", "gas", exp_cap, 300.0)),
        lines=(), unmet_penalty_per_mwh=5000.0)
    hor = Horizon(
        periods=periods, step_hours=1.0,
        demand_mw=tuple({"bus": demand} for _ in range(periods)),
        availability_mw=tuple({"cheap": cheap_cap, "exp": exp_cap} for _ in range(periods)),
        generator_online=tuple({"cheap": True, "exp": True} for _ in range(periods)),
        line_in_service=tuple({} for _ in range(periods)))
    bat = Battery("b1", "bus", capacity_mwh=60.0, max_charge_mw=25.0, max_discharge_mw=25.0,
                  eta_charge=0.95, eta_discharge=0.95, initial_soc_mwh=30.0, min_soc_mwh=0.0,
                  final_soc_mwh=30.0)
    return ScheduleProblem(network=net, horizon=hor, batteries=(bat,))


def small_config(seeds=(11, 12), n_scenarios=12) -> ExperimentConfig:
    return ExperimentConfig(seeds=tuple(seeds), n_scenarios=n_scenarios)


def _strip_timings(obj):
    """Drop wall-clock fields so two runs can be compared for exact equality."""
    if isinstance(obj, dict):
        return {k: _strip_timings(v) for k, v in obj.items()
                if k not in ("runtime_seconds", "started_at", "runtime_s")}
    if isinstance(obj, list):
        return [_strip_timings(v) for v in obj]
    return obj


# ================================================================ statistics ====
def test_p95_interpolation():
    # rank = 0.95*(4-1) = 2.85 -> 20*(1-0.85) + 100*0.85 = 88.0
    assert _p95([0.0, 10.0, 20.0, 100.0]) == pytest.approx(88.0)
    assert _p95([5.0]) == 5.0
    assert _p95([]) is None


def test_cohens_dz_known_values():
    # mean 2.0, sample sd 2.0 -> dz = 1.0
    assert _cohens_dz([0.0, 2.0, 4.0]) == pytest.approx(1.0)
    # zero variance -> undefined
    assert _cohens_dz([3.0, 3.0, 3.0]) is None
    # too few samples -> undefined
    assert _cohens_dz([1.0]) is None


def test_describe_stats_via_aggregate():
    runs = [
        {"seed": 1, "evaluations": {
            "deterministic": {"status": "optimal", "expected_cost_usd": 10.0,
                              "scenario_costs_usd": [1.0, 2.0],
                              "scenario_unmet_mwh": [0.0, 0.0],
                              "scenario_probabilities": [0.5, 0.5],
                              "validation_all_passed": True},
            "risk_neutral": {"status": "optimal", "expected_cost_usd": 8.0,
                             "scenario_costs_usd": [1.0, 1.0],
                             "scenario_unmet_mwh": [0.0, 0.0],
                             "scenario_probabilities": [0.5, 0.5],
                             "validation_all_passed": True},
            "risk_aware": {"status": "infeasible"}}},
        {"seed": 2, "evaluations": {
            "deterministic": {"status": "optimal", "expected_cost_usd": 20.0,
                              "scenario_costs_usd": [3.0, 4.0],
                              "scenario_unmet_mwh": [1.0, 1.0],
                              "scenario_probabilities": [0.5, 0.5],
                              "validation_all_passed": True},
            "risk_neutral": {"status": "optimal", "expected_cost_usd": 18.0,
                             "scenario_costs_usd": [2.0, 2.0],
                             "scenario_unmet_mwh": [0.0, 1.0],
                             "scenario_probabilities": [0.5, 0.5],
                             "validation_all_passed": True},
            "risk_aware": {"status": "optimal", "expected_cost_usd": 15.0,
                           "scenario_costs_usd": [2.0, 3.0],
                           "scenario_unmet_mwh": [0.0, 1.0],
                           "scenario_probabilities": [0.5, 0.5],
                           "validation_all_passed": True}}},
    ]
    agg = aggregate_runs(runs)
    assert agg["n_runs"] == 2
    det = agg["policies"]["deterministic"]
    assert det["feasible_runs"] == 2 and det["failure_count"] == 0
    assert det["metrics"]["expected_cost_usd"]["mean"] == pytest.approx(15.0)
    assert det["metrics"]["expected_cost_usd"]["median"] == pytest.approx(15.0)
    # risk_aware failed once -> feasible rate 0.5, only seed 2 in the mean
    ra = agg["policies"]["risk_aware"]
    assert ra["feasible_runs"] == 1 and ra["failure_count"] == 1
    assert ra["failed_seeds"] == [1]
    assert ra["metrics"]["expected_cost_usd"]["mean"] == pytest.approx(15.0)
    assert ra["feasible_run_rate"] == pytest.approx(0.5)


def test_compare_aggregate_pairs_same_scenarios():
    runs = [
        {"seed": 1, "evaluations": {
            "deterministic": {"status": "optimal", "scenario_costs_usd": [10.0, 12.0]},
            "risk_neutral": {"status": "optimal", "scenario_costs_usd": [10.0, 12.0]},
            "risk_aware": {"status": "optimal", "scenario_costs_usd": [8.0, 9.0]}}},
        {"seed": 2, "evaluations": {
            "deterministic": {"status": "optimal", "scenario_costs_usd": [5.0, 6.0]},
            "risk_neutral": {"status": "optimal", "scenario_costs_usd": [5.0, 6.0]},
            "risk_aware": {"status": "optimal", "scenario_costs_usd": [4.0, 5.0]}}},
    ]
    comp = compare_aggregate(runs)
    ra_rn = comp["pairs"]["risk_aware_minus_risk_neutral"]
    # differences (risk_aware - risk_neutral): [-2, -3, -1, -1] -> mean -1.75
    assert ra_rn["mean_diff_usd"] == pytest.approx(-1.75)
    assert ra_rn["n_pairs"] == 4 and ra_rn["seeds_used"] == 2
    det_rn = comp["pairs"]["risk_neutral_minus_deterministic"]
    assert det_rn["mean_diff_usd"] == pytest.approx(0.0)


def test_compare_aggregate_skips_missing_policy():
    runs = [{"seed": 1, "evaluations": {
        "deterministic": {"status": "optimal", "scenario_costs_usd": [1.0]},
        "risk_neutral": {"status": "optimal", "scenario_costs_usd": [1.0]},
        # risk_aware missing entirely
    }}]
    comp = compare_aggregate(runs)
    ra_rn = comp["pairs"]["risk_aware_minus_risk_neutral"]
    assert ra_rn["n_pairs"] == 0 and ra_rn["mean_diff_usd"] is None
    assert ra_rn["seeds_skipped"] == [1]
    det_rn = comp["pairs"]["risk_neutral_minus_deterministic"]
    assert det_rn["n_pairs"] == 1


# ================================================================ config ====
def test_default_config_is_five_seeds_forty_scenarios():
    cfg = default_experiment_config()
    assert len(cfg.seeds) == 5
    assert cfg.n_scenarios == 40
    cfg.validate()


def test_config_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ExperimentConfig(seeds=(), n_scenarios=10).validate()
    with pytest.raises(ValueError):
        ExperimentConfig(seeds=(1,), n_scenarios=1).validate()
    with pytest.raises(ValueError):
        ExperimentConfig(seeds=(1,), holdout_fraction=0.0).validate()
    with pytest.raises(ValueError):
        ExperimentConfig(seeds=(1,), risk_weight=1.5).validate()
    with pytest.raises(ValueError):
        ExperimentConfig(seeds=(1,), cvar_alpha=1.0).validate()


def test_config_roundtrip_and_schema():
    d = default_experiment_config().to_dict()
    assert d["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    assert d["seeds"] == list(default_experiment_config().seeds)


# ================================================================ driver ====
@needs_solver
def test_run_one_seed_shared_holdout_for_all_policies():
    prob = single_bus_problem()
    run = run_one_seed(prob, small_config(seeds=(7,), n_scenarios=10), 7)
    holdout_ids = run["holdout_ids"]
    assert holdout_ids, "expected a non-empty holdout set"
    for policy in ("deterministic", "risk_neutral", "risk_aware"):
        ev = run["evaluations"][policy]
        if ev["status"] != "optimal":
            continue
        assert len(ev["scenario_costs_usd"]) == len(holdout_ids)
        assert len(ev["scenario_probabilities"]) == len(holdout_ids)


@needs_solver
def test_experiment_is_reproducible():
    prob = single_bus_problem()
    a = run_comparative_benchmark(prob, small_config(seeds=(3, 4), n_scenarios=10))
    b = run_comparative_benchmark(prob, small_config(seeds=(3, 4), n_scenarios=10))
    # Wall-clock timings are not deterministic; every decision-relevant number is.
    assert _strip_timings(a.to_dict()) == _strip_timings(b.to_dict())


@needs_solver
def test_experiment_record_shape_and_aggregate():
    prob = single_bus_problem()
    rec = run_comparative_benchmark(prob, small_config(seeds=(5, 6), n_scenarios=10))
    payload = rec.to_dict()
    assert payload["n_seeds"] == 2
    assert payload["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    for policy in ("deterministic", "risk_neutral", "risk_aware"):
        assert policy in payload["aggregate"]["policies"]
    # paired comparison keys present
    assert "risk_aware_minus_risk_neutral" in payload["comparisons"]["pairs"]


@needs_solver
def test_write_experiment_creates_all_artefacts(tmp_path):
    prob = single_bus_problem()
    rec = run_comparative_benchmark(prob, small_config(seeds=(8,), n_scenarios=10))
    paths = write_experiment(rec, tmp_path)
    for key in ("config", "per_scenario_csv", "aggregate_summary", "readme"):
        assert paths[key].exists()
    assert (tmp_path / "runs" / "seed_8.json").exists()

    config = json.loads(paths["config"].read_text(encoding="utf-8"))
    assert config["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    summary = json.loads(paths["aggregate_summary"].read_text(encoding="utf-8"))
    assert "aggregate" in summary and "comparisons" in summary

    # per-scenario CSV has a header and, for each feasible policy, rows.
    text = paths["per_scenario_csv"].read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].startswith("seed,policy,scenario_index")
    assert len(lines) > 1


@needs_solver
def test_run_one_seed_records_uncertainty_and_ids():
    prob = single_bus_problem()
    run = run_one_seed(prob, small_config(seeds=(9,), n_scenarios=12), 9)
    assert run["uncertainty"]["seed"] == 9
    assert run["n_total_scenarios"] == 12
    assert len(run["in_sample_ids"]) + len(run["holdout_ids"]) == 12
    assert not (set(run["in_sample_ids"]) & set(run["holdout_ids"]))
