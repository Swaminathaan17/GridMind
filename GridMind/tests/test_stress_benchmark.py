"""Integrity tests for the Stage 5C stress benchmark (schema v2)."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gridmind.multiperiod import sample_schedule_problem
from gridmind.stress import (
    Disturbance,
    ForecastErrorModel,
    MethodResult,
    StressEnvironment,
    StressSchedule,
    _finalize_method_result,
    _terminal_soc_deviation,
    _trace_totals,
    run_method_open_loop,
    run_method_stochastic_rolling_horizon,
)
from gridmind.stress_bench import (
    STRESS_BENCH_SCHEMA_VERSION,
    StressBenchmarkConfig,
    _classify,
    run_stress_benchmark,
    write_stress_benchmark,
)
from gridmind.solver import solver_available
from gridmind.validation import DEFAULT_TOL


SOLVER_OK, SOLVER_MSG = solver_available(mip=False)
pytestmark = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")


def _compound_env(seed: int = 20260301) -> StressEnvironment:
    schedule = StressSchedule(
        disturbances=(
            Disturbance(kind="demand_spike", onset=8, duration=4,
                        severity=1.25, target=None),
            Disturbance(kind="renewable_shortfall", onset=10, duration=6,
                        severity=0.35, target=None),
        ),
        seed=seed, name="test_compound")
    return StressEnvironment(base_problem=sample_schedule_problem(), stress=schedule,
                             error_model=ForecastErrorModel(seed=seed))


def _finalize(result: MethodResult, env: StressEnvironment) -> MethodResult:
    realized_d, realized_a = env.realized()
    return _finalize_method_result(
        result, env.truth_problem, realized_d, realized_a, allow_unmet=True,
        tol=DEFAULT_TOL, battery_mode="lp")


class _FutureShiftEnv(StressEnvironment):
    """Environment whose realized conditions change only from ``cutoff`` onward."""

    def __init__(self, base_problem, stress, error_model, cutoff: int):
        super().__init__(base_problem=base_problem, stress=stress, error_model=error_model)
        self.cutoff = cutoff

    def realized(self):
        demand, avail = super().realized()
        demand = [dict(row) for row in demand]
        avail = [dict(row) for row in avail]
        for t in range(self.cutoff, len(demand)):
            for node in demand[t]:
                demand[t][node] *= 1.5
            for gen in avail[t]:
                avail[t][gen] *= 0.75
        return tuple(demand), tuple(avail)


def test_schema_v2_includes_only_validated_solver_optimal_methods(tmp_path):
    record = run_stress_benchmark(
        config=StressBenchmarkConfig(n_scenarios_stochastic=4))
    assert record.schema_version == STRESS_BENCH_SCHEMA_VERSION == 2
    expected = [
        "deterministic_open_loop", "stochastic_risk_neutral",
        "stochastic_risk_aware", "stochastic_rolling_risk_neutral",
        "stochastic_rolling_risk_aware", "rolling_horizon"]
    assert record.included_methods == expected
    assert record.excluded_methods == []
    assert set(record.included_methods).isdisjoint(record.excluded_methods)
    for method in record.included_methods:
        payload = record.results[method]
        assert payload["validation_status"] == "passed"
        assert payload["feasible_for_comparison"]
        assert payload["solver_status"] == "optimal"
        assert payload["exclusion_reason"] is None
    for method in ("stochastic_risk_neutral", "stochastic_risk_aware"):
        payload = record.results[method]
        assert payload["solver_status"] == "optimal"
        assert payload["policy_status"] == "completed"
        assert payload["validation_status"] == "passed"
        assert payload["terminal_soc_deviation_mwh"] <= DEFAULT_TOL
        assert payload["config"]["commitment_horizon"] == "full"
        assert payload["first_stage"]["lead_periods"] == record.problem["periods"]
    assert record.comparison["included_methods"] == record.included_methods
    assert record.comparison["excluded_methods"] == record.excluded_methods

    paths = write_stress_benchmark(record, tmp_path)
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    assert summary["schema_version"] == 2
    assert summary["included_methods"] == record.included_methods
    assert summary["excluded_methods"] == record.excluded_methods
    assert summary["comparison"]["attempted_methods"] == list(record.comparison["attempted_methods"])
    for method, payload in summary["results"].items():
        assert payload["validation_status"] == record.results[method]["validation_status"]
        assert payload["included_for_comparison"] == record.results[method]["included_for_comparison"]


def test_configured_method_selection_uses_canonical_order():
    config = StressBenchmarkConfig(
        methods=("rolling_horizon", "deterministic_open_loop"),
        lookahead=4, n_scenarios_stochastic=4)
    record = run_stress_benchmark(config=config)
    assert list(record.results) == ["deterministic_open_loop", "rolling_horizon"]
    assert record.comparison["attempted_methods"] == [
        "deterministic_open_loop", "rolling_horizon"]


def test_missing_and_incomplete_traces_are_excluded():
    env = _compound_env(seed=11)
    missing = MethodResult(
        method="probe", status="optimal", solver_status="optimal",
        policy_status="completed")
    out = _finalize(missing, env)
    assert out.validation_status == "missing_trace"
    assert not out.feasible_for_comparison
    assert out.exclusion_reason == "missing_executed_trace"

    full = run_method_open_loop(env)
    trace = full.trace[:5]
    totals = _trace_totals(env.truth_problem, trace)
    terminal = dict(trace[-1]["battery_soc_mwh"])
    partial = MethodResult(
        method="probe", status="optimal", solver_status="optimal",
        policy_status="completed", trace=trace,
        realized_operating_cost_usd=totals["operating_cost_usd"],
        generation_cost_usd=totals["generation_cost_usd"],
        unmet_penalty_cost_usd=totals["unmet_penalty_cost_usd"],
        battery_throughput_cost_usd=totals["battery_throughput_cost_usd"],
        unmet_energy_mwh=totals["unmet_energy_mwh"],
        battery_throughput_mwh=totals["battery_throughput_mwh"],
        terminal_soc_deviation_mwh=_terminal_soc_deviation(
            env.truth_problem, terminal))
    out = _finalize(partial, env)
    assert out.validation_status == "incomplete"
    assert not out.feasible_for_comparison
    assert out.exclusion_reason.startswith("incomplete_executed_trace: 5/")


def test_open_loop_traces_and_provenance_are_reproducible():
    config = StressBenchmarkConfig(methods=("deterministic_open_loop",))
    first = run_stress_benchmark(config=config)
    second = run_stress_benchmark(config=config)
    a = first.results["deterministic_open_loop"]
    b = second.results["deterministic_open_loop"]
    assert a["realized_conditions_sha256"] == b["realized_conditions_sha256"]
    assert a["problem_sha256"] == b["problem_sha256"]
    assert a["forecast_sha256"] == b["forecast_sha256"]
    assert a["config_sha256"] == b["config_sha256"]
    assert a["trace"] == b["trace"]


def test_custom_problem_provenance_is_preserved():
    custom = replace(
        sample_schedule_problem(),
        network=replace(sample_schedule_problem().network, name="custom_provenance_network"))
    config = StressBenchmarkConfig(methods=("deterministic_open_loop",))
    record = run_stress_benchmark(problem=custom, config=config)
    payload = record.results["deterministic_open_loop"]
    assert record.problem["network"] == "custom_provenance_network"
    assert payload["problem_sha256"]
    assert payload["config_sha256"]
    assert payload["forecast_sha256"]
    assert payload["realized_conditions_sha256"]


def test_stochastic_rolling_hybrid_is_selected_validated_and_eligible():
    config = StressBenchmarkConfig(
        methods=("stochastic_rolling_risk_neutral",),
        lookahead=3, n_scenarios_stochastic=2)
    record = run_stress_benchmark(config=config)
    assert list(record.results) == ["stochastic_rolling_risk_neutral"]
    assert record.included_methods == ["stochastic_rolling_risk_neutral"]
    assert record.excluded_methods == []
    payload = record.results["stochastic_rolling_risk_neutral"]
    assert payload["solver_status"] == "optimal"
    assert payload["validation_status"] == "passed"
    assert payload["feasible_for_comparison"]
    assert payload["policy_label"] == (
        "causal_receding_horizon_stochastic_commitment_no_scenario_tree")
    assert len(payload["trace"]) == record.problem["periods"]
    for row in payload["trace"]:
        committed = row["committed"]
        executed = row["executed"]
        for battery in committed["charge"]:
            assert executed["charge"][battery] <= committed["charge"][battery] + DEFAULT_TOL
            assert executed["discharge"][battery] <= committed["discharge"][battery] + DEFAULT_TOL


def test_hybrid_current_actions_do_not_depend_on_future_realized_data():
    base = _compound_env(seed=77)
    shifted = _FutureShiftEnv(
        base_problem=base.base_problem, stress=base.stress,
        error_model=base.error_model, cutoff=2)
    kwargs = dict(risk_weight=0.0, cvar_alpha=0.95, lead_periods=1,
                  n_scenarios=2, lookahead=2)
    a = run_method_stochastic_rolling_horizon(base, **kwargs)
    b = run_method_stochastic_rolling_horizon(shifted, **kwargs)
    assert a.validation_status == "passed"
    assert b.validation_status == "passed"
    assert a.trace[:2] == b.trace[:2]


def _infeasible_payload(method: str, reason: str) -> dict:
    return MethodResult(
        method=method, status="policy_infeasible", solver_status="optimal",
        policy_status="policy_infeasible", validation_status="failed",
        feasible_for_comparison=False, exclusion_reason=reason,
        trace=[{}], periods_executed=24, periods_expected=24).to_dict()


def test_classification_excludes_infeasible_methods_and_names_reason():
    reason = "physical_validation_failed: battery_soc_dynamics"
    results = {
        "deterministic_open_loop": MethodResult(
            method="deterministic_open_loop", status="optimal", solver_status="optimal",
            policy_status="optimal", validation_status="passed",
            feasible_for_comparison=True, trace=[{}],
            periods_executed=24, periods_expected=24).to_dict(),
        "stochastic_risk_neutral": _infeasible_payload(
            "stochastic_risk_neutral", reason),
    }
    selected = ("deterministic_open_loop", "stochastic_risk_neutral")
    included, exclusions, comparison = _classify(results, selected)
    assert included == ["deterministic_open_loop"]
    assert [e["method"] for e in exclusions] == ["stochastic_risk_neutral"]
    assert exclusions[0]["reason"] == reason
    assert not results["stochastic_risk_neutral"]["included_for_comparison"]
    assert comparison["included_methods"] == ["deterministic_open_loop"]
    assert comparison["excluded_methods"] == ["stochastic_risk_neutral"]
    assert comparison["solver_optimal_methods"] == [
        "deterministic_open_loop", "stochastic_risk_neutral"]
    assert comparison["validation_passed_methods"] == ["deterministic_open_loop"]


def test_benchmark_with_zero_eligible_methods_is_truthful():
    reason = "physical_validation_failed: battery_soc_dynamics"
    results = {"stochastic_risk_neutral": _infeasible_payload(
        "stochastic_risk_neutral", reason)}
    included, exclusions, comparison = _classify(
        results, ("stochastic_risk_neutral",))
    assert included == []
    assert [e["method"] for e in exclusions] == ["stochastic_risk_neutral"]
    assert comparison["included_methods"] == []
    assert comparison["excluded_methods"] == ["stochastic_risk_neutral"]
    assert not results["stochastic_risk_neutral"]["feasible_for_comparison"]
    assert results["stochastic_risk_neutral"]["exclusion_reason"] == reason


def test_finalize_does_not_re_solve_or_rerun_a_method(monkeypatch):
    env = _compound_env(seed=19)
    full = run_method_open_loop(env)

    def _forbidden(*args, **kwargs):
        raise AssertionError("validation attempted a second solve/run")

    monkeypatch.setattr("gridmind.stress.run_rolling_horizon", _forbidden)
    monkeypatch.setattr("gridmind.stress.solve_stochastic", _forbidden)
    monkeypatch.setattr("gridmind.scheduling.solve_schedule", _forbidden)
    monkeypatch.setattr("gridmind.solver.resolve_solver", _forbidden)
    out = _finalize(full, env)
    assert out.validation_status == "passed"
    assert out.feasible_for_comparison
