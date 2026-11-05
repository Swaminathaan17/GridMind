"""Targeted regression tests for Stage 5C corrections."""
from __future__ import annotations

from dataclasses import replace

import pytest

from gridmind.control import ControlConfig
from gridmind.multiperiod import sample_schedule_problem
from gridmind.stress import (
    Disturbance,
    ForecastErrorModel,
    StressEnvironment,
    StressSchedule,
    _advance,
    _finalize_method_result,
    _terminal_soc_deviation,
    _trace_totals,
    run_method_open_loop,
    run_method_rolling_horizon,
    run_method_stochastic,
)
from gridmind.solver import solver_available
from gridmind.validation import DEFAULT_TOL


SOLVER_OK, SOLVER_MSG = solver_available(mip=False)
needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")
MIP_OK, MIP_MSG = solver_available(mip=True)
needs_mip_solver = pytest.mark.skipif(not MIP_OK, reason=f"no verified MIP solver: {MIP_MSG}")


def _compound_env(seed: int = 20260301) -> StressEnvironment:
    problem = sample_schedule_problem()
    schedule = StressSchedule(
        disturbances=(
            Disturbance(kind="demand_spike", onset=8, duration=4,
                        severity=1.25, target=None),
            Disturbance(kind="renewable_shortfall", onset=10, duration=6,
                        severity=0.35, target=None),
        ),
        seed=seed, name="test_compound")
    return StressEnvironment(base_problem=problem, stress=schedule,
                             error_model=ForecastErrorModel(seed=seed))


def _assert_executed_battery_contract(res, problem):
    """Executed trace must follow the committed battery schedule over all periods."""
    dt = problem.step_hours
    previous = {b.id: float(b.initial_soc_mwh) for b in problem.batteries}
    assert len(res.trace) == problem.periods
    for row in res.trace:
        executed = row["executed"]
        committed = row["committed"]
        for b in problem.batteries:
            charge = executed["charge"][b.id]
            discharge = executed["discharge"][b.id]
            expected_soc = (previous[b.id] + b.eta_charge * charge * dt
                            - discharge * dt / b.eta_discharge)
            assert row["battery_soc_mwh"][b.id] == pytest.approx(expected_soc, abs=1e-6)
            # Re-dispatch may curtail committed charging, never add battery action.
            assert charge <= committed["charge"][b.id] + DEFAULT_TOL
            assert discharge <= committed["discharge"][b.id] + DEFAULT_TOL
            previous[b.id] = row["battery_soc_mwh"][b.id]
    for b in problem.batteries:
        if b.final_soc_mwh is not None:
            assert previous[b.id] == pytest.approx(b.final_soc_mwh, abs=DEFAULT_TOL)


@needs_solver
@pytest.mark.parametrize("risk_weight", [0.0, 0.5], ids=["risk_neutral", "risk_aware"])
def test_stochastic_open_loop_commits_and_executes_full_horizon_battery(risk_weight):
    env = _compound_env(seed=123)
    problem = env.truth_problem
    lead = 1
    res = run_method_stochastic(env, risk_weight=risk_weight, cvar_alpha=0.95,
                                lead_periods=lead, n_scenarios=4)
    assert res.solver_status == "optimal"
    assert res.policy_status == "completed"
    assert res.validation_status == "passed"
    assert res.feasible_for_comparison
    assert res.exclusion_reason is None
    assert res.policy_label == (
        "committed_full_horizon_open_loop_battery_then_realized_redispatch")
    assert res.first_stage["lead_periods"] == problem.periods
    assert res.first_stage["requested_lead_periods"] == lead
    assert res.config["committed_battery_periods"] == problem.periods
    assert res.config["commitment_horizon"] == "full"
    for bid, series in res.first_stage["charge"].items():
        assert len(series) == problem.periods
        assert len(res.first_stage["discharge"][bid]) == problem.periods
    # The committed schedule is genuinely full-horizon: it is not zeroed after
    # the caller's requested lead window.
    assert any(value > DEFAULT_TOL
               for bid in res.first_stage["charge"]
               for value in res.first_stage["charge"][bid][lead:])
    assert any(value > DEFAULT_TOL
               for bid in res.first_stage["discharge"]
               for value in res.first_stage["discharge"][bid][lead:])
    _assert_executed_battery_contract(res, problem)
    soc_check = next(c for c in res.validation_report["checks"]
                     if c["name"] == "battery_soc_dynamics")
    assert soc_check["passed"]
    assert soc_check["max_violation"] <= DEFAULT_TOL


def test_invalid_lead_periods_rejected():
    env = _compound_env(seed=1)
    with pytest.raises(Exception):
        run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                              lead_periods=-1, n_scenarios=2)
    with pytest.raises(Exception):
        run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                              lead_periods=env.truth_problem.periods + 1,
                              n_scenarios=2)


def test_advance_does_not_silently_clamp_invalid_soc():
    problem = sample_schedule_problem()
    soc0 = {b.id: b.initial_soc_mwh for b in problem.batteries}
    with pytest.raises(Exception):
        _advance(problem, soc0, {b.id: 0.0 for b in problem.batteries},
                 {b.id: 10**9 for b in problem.batteries}, 0)


def test_advance_allows_lp_simultaneity_but_exclusive_rejects_it():
    problem = sample_schedule_problem()
    soc0 = {b.id: b.initial_soc_mwh for b in problem.batteries}
    charge = {b.id: 0.01 for b in problem.batteries}
    discharge = {b.id: 0.01 for b in problem.batteries}
    _advance(problem, soc0, charge, discharge, 0, battery_mode="lp")
    with pytest.raises(Exception):
        _advance(problem, soc0, charge, discharge, 0, battery_mode="exclusive")


@needs_solver
def test_open_loop_throughput_uses_actual_executed_actions():
    env = _compound_env(seed=5)
    res = run_method_open_loop(env)
    assert res.status == "optimal"
    assert res.validation_status == "passed"
    assert res.feasible_for_comparison
    assert res.trace_metrics_match
    assert res.battery_throughput_mwh is not None
    assert res.battery_throughput_mwh >= 0
    dt = env.truth_problem.step_hours
    expected = dt * sum(
        sum(row["executed"]["charge"].values()) + sum(row["executed"]["discharge"].values())
        for row in res.trace)
    assert res.battery_throughput_mwh == pytest.approx(expected)
    previous = {b.id: b.initial_soc_mwh for b in env.truth_problem.batteries}
    for row in res.trace:
        for battery in env.truth_problem.batteries:
            charge = row["executed"]["charge"][battery.id]
            discharge = row["executed"]["discharge"][battery.id]
            advanced = (previous[battery.id] + battery.eta_charge * charge * dt
                        - discharge * dt / battery.eta_discharge)
            assert row["battery_soc_mwh"][battery.id] == pytest.approx(advanced)
            previous[battery.id] = row["battery_soc_mwh"][battery.id]


@needs_mip_solver
def test_selected_battery_mode_reaches_execution_and_validation():
    env = _compound_env(seed=23)
    res = run_method_open_loop(env, battery_mode="exclusive")
    assert res.config["battery_mode"] == "exclusive"
    assert res.validation_report["battery_exclusive"] is not False
    assert res.validation_status == "passed"
    assert res.feasible_for_comparison


@needs_solver
def test_recovered_controller_is_not_reported_as_solver_optimal(monkeypatch):
    import gridmind.control as control_module

    env = _compound_env(seed=31)
    original = control_module._solve_window
    calls = {"count": 0}

    def _flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("forced first-window solve failure")
        return original(*args, **kwargs)

    monkeypatch.setattr("gridmind.control._solve_window", _flaky)
    res = run_method_rolling_horizon(env, ControlConfig(lookahead=3))
    assert res.policy_status == "completed_with_recovery"
    assert res.solver_status == "mixed"
    assert res.solver_status != "optimal"


@needs_solver
def test_stochastic_terminal_soc_is_met_by_actual_executed_trace():
    env = _compound_env()
    problem = env.truth_problem
    res = run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                                lead_periods=1, n_scenarios=4)
    assert res.solver_status == "optimal"
    assert res.validation_status == "passed"
    assert res.status == "completed"
    assert res.feasible_for_comparison
    assert res.terminal_soc_deviation_mwh is not None
    assert res.terminal_soc_deviation_mwh <= DEFAULT_TOL
    _assert_executed_battery_contract(res, problem)
    trace_terminal = _terminal_soc_deviation(
        problem, dict(res.trace[-1]["battery_soc_mwh"]))
    assert trace_terminal == pytest.approx(res.terminal_soc_deviation_mwh, abs=1e-9)


@needs_mip_solver
def test_stochastic_full_horizon_battery_in_exclusive_mode():
    env = _compound_env(seed=23)
    problem = env.truth_problem
    res = run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                                lead_periods=1, n_scenarios=4,
                                battery_mode="exclusive")
    assert res.solver_status == "optimal"
    assert res.config["battery_mode"] == "exclusive"
    assert res.validation_status == "passed"
    assert res.feasible_for_comparison
    assert res.validation_report["battery_exclusive"] is not False
    assert res.terminal_soc_deviation_mwh <= DEFAULT_TOL
    _assert_executed_battery_contract(res, problem)


@needs_solver
def test_deliberately_infeasible_stochastic_policy_stays_infeasible():
    """A trace that abandons the committed schedule must fail validation."""
    env = _compound_env(seed=123)
    problem = env.truth_problem
    good = run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                                 lead_periods=1, n_scenarios=4)
    assert good.validation_status == "passed"

    trace = [dict(row) for row in good.trace]
    held_soc = dict(trace[0]["battery_soc_mwh"])
    for row in trace[1:]:
        executed = dict(row["executed"])
        executed["charge"] = {b.id: 0.0 for b in problem.batteries}
        executed["discharge"] = {b.id: 0.0 for b in problem.batteries}
        row["executed"] = executed
        row["committed"] = {
            "charge": {b.id: 0.0 for b in problem.batteries},
            "discharge": {b.id: 0.0 for b in problem.batteries}}
        row["battery_soc_mwh"] = dict(held_soc)
    # The deliberately abandoned schedule ends away from the declared terminal SoC.
    trace[-1]["battery_soc_mwh"] = {
        b.id: (float(b.final_soc_mwh) if b.final_soc_mwh is not None
               else float(b.initial_soc_mwh)) + 1.0
        for b in problem.batteries}

    totals = _trace_totals(problem, trace)
    terminal = dict(trace[-1]["battery_soc_mwh"])
    infeasible = replace(
        good, trace=trace,
        realized_operating_cost_usd=totals["operating_cost_usd"],
        generation_cost_usd=totals["generation_cost_usd"],
        unmet_penalty_cost_usd=totals["unmet_penalty_cost_usd"],
        battery_throughput_cost_usd=totals["battery_throughput_cost_usd"],
        unmet_energy_mwh=totals["unmet_energy_mwh"],
        battery_throughput_mwh=totals["battery_throughput_mwh"],
        terminal_soc_deviation_mwh=_terminal_soc_deviation(problem, terminal),
        status="optimal", policy_status="completed", solver_status="optimal",
        validation_status="skipped", feasible_for_comparison=False,
        exclusion_reason=None)
    realized_d, realized_a = env.realized()
    out = _finalize_method_result(
        infeasible, problem, realized_d, realized_a, allow_unmet=True,
        tol=DEFAULT_TOL, battery_mode="lp")
    assert out.validation_status == "failed"
    assert out.policy_status == "policy_infeasible"
    assert not out.feasible_for_comparison
    assert out.exclusion_reason == "terminal_soc_violation"
    assert out.validation_report["passed"] is False
