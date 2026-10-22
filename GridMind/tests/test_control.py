"""Stage 5B tests: rolling-horizon control.

Structure
---------
* Solver-free tests: forecast-error model determinism, realized-vs-forecast
  distinction, window slicing, SoC carry-forward arithmetic, safe fallback.
* Solver-backed tests (guarded by ``needs_solver``): end-to-end controller runs,
  open-loop comparison, exclusivity, and the future-information-leakage test.
* Failure-handling tests: a deliberately infeasible strict window must trigger
  the explicit failure policy and never fabricate a plan.
"""
from __future__ import annotations

import pytest

from gridmind.control import (CONTROL_SCHEMA_VERSION, ControlConfig,
                              ForecastErrorModel, _build_window_problem, _clamp_soc,
                              _safe_hold_dispatch, compare_control, forecast_at, realize,
                              run_open_loop_baseline, run_rolling_horizon)
from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.network import Generator, Network, Node
from gridmind.solver import solver_available
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")
_MIP_OK, _MIP_MSG = solver_available(mip=True)
needs_mip_solver = pytest.mark.skipif(not _MIP_OK, reason=f"no verified MIP solver: {_MIP_MSG}")


# --------------------------------------------------------- problem builders ----
def control_problem(periods: int = 6, demand: float = 40.0, final_soc: float | None = 10.0
                    ) -> ScheduleProblem:
    """One bus; cheap generator available only early, a battery, a demand ramp.

    Demand rises over the horizon so *look-ahead matters*: a myopic controller
    cannot prepare the battery for the late expensive periods.
    """
    net = Network(
        name="ctrl", nodes=(Node("bus", "bus"),),
        generators=(Generator("cheap", "Cheap", "bus", "gas", 200.0, 10.0),
                    Generator("exp", "Expensive", "bus", "gas", 200.0, 120.0)),
        lines=(), unmet_penalty_per_mwh=5000.0)
    demand_by_t = tuple({"bus": demand + 8.0 * t} for t in range(periods))
    availability = tuple({"cheap": 60.0, "exp": 200.0} for _ in range(periods))
    hor = Horizon(
        periods=periods, step_hours=1.0, demand_mw=demand_by_t,
        availability_mw=availability,
        generator_online=tuple({"cheap": True, "exp": True} for _ in range(periods)),
        line_in_service=tuple({} for _ in range(periods)))
    bat = Battery("b1", "bus", capacity_mwh=80.0, max_charge_mw=30.0, max_discharge_mw=30.0,
                  eta_charge=0.95, eta_discharge=0.95, initial_soc_mwh=20.0, min_soc_mwh=0.0,
                  final_soc_mwh=final_soc)
    return ScheduleProblem(network=net, horizon=hor, batteries=(bat,))


def infeasible_strict_problem(periods: int = 4) -> ScheduleProblem:
    """No generation at all but positive demand -> strict mode is infeasible."""
    net = Network(
        name="dead", nodes=(Node("bus", "bus", 50.0),),
        generators=(Generator("g", "G", "bus", "gas", 0.0, 10.0),),
        lines=(), unmet_penalty_per_mwh=5000.0)
    hor = Horizon(
        periods=periods, step_hours=1.0,
        demand_mw=tuple({"bus": 50.0} for _ in range(periods)),
        availability_mw=tuple({"g": 0.0} for _ in range(periods)),
        generator_online=tuple({"g": True} for _ in range(periods)),
        line_in_service=tuple({} for _ in range(periods)))
    return ScheduleProblem(network=net, horizon=hor)


# =============================================================== forecast model ====
def test_realization_is_reproducible():
    prob = control_problem()
    a_d, a_r = realize(prob, ForecastErrorModel(seed=5))
    b_d, b_r = realize(prob, ForecastErrorModel(seed=5))
    assert a_d == b_d and a_r == b_r


def test_forecast_differs_from_realization_with_noise():
    prob = control_problem()
    model = ForecastErrorModel(demand_sigma=0.1, renewable_sigma=0.1, seed=9)
    real_d, real_r = realize(prob, model)
    fc_d, fc_r = forecast_at(prob, model, 0, real_d, real_r)
    # With sigma > 0 the future forecast must differ somewhere from the realization.
    assert any(fc_d[t] != real_d[t] for t in range(prob.periods))


def test_forecast_with_past_is_pinned_to_realized_past():
    prob = control_problem()
    model = ForecastErrorModel(seed=3)
    real_d, real_r = realize(prob, model)
    fc_d, fc_r = forecast_at(prob, model, 3, real_d, real_r)
    for t in range(3):
        assert fc_d[t] == real_d[t]
        assert fc_r[t] == real_r[t]


def test_zero_sigma_forecast_equals_realization():
    prob = control_problem()
    model = ForecastErrorModel(demand_sigma=0.0, renewable_sigma=0.0, seed=1)
    real_d, real_r = realize(prob, model)
    fc_d, fc_r = forecast_at(prob, model, 2, real_d, real_r)
    assert fc_d == real_d and fc_r == real_r


# =============================================================== window / state ====
def test_window_problem_slices_horizon_and_carries_state():
    prob = control_problem(periods=8, final_soc=10.0)
    demand = prob.horizon.demand_mw
    avail = prob.horizon.availability_mw
    window = _build_window_problem(prob, 3, 4, demand, avail,
                                   soc_now={"b1": 33.0}, init_dispatch={"cheap": 5.0},
                                   apply_final_soc=False)
    assert window.periods == 4
    assert window.battery("b1").initial_soc_mwh == 33.0
    # intermediate window releases the terminal SoC
    assert window.battery("b1").final_soc_mwh is None
    assert window.initial_dispatch_mw == {"cheap": 5.0}


def test_final_window_keeps_terminal_soc():
    prob = control_problem(periods=8, final_soc=10.0)
    demand, avail = prob.horizon.demand_mw, prob.horizon.availability_mw
    window = _build_window_problem(prob, 5, 4, demand, avail, soc_now={"b1": 20.0},
                                   init_dispatch={}, apply_final_soc=True)
    assert window.periods == 3  # clamped to horizon end
    assert window.battery("b1").final_soc_mwh == 10.0


def test_clamp_soc_enforces_physical_bounds():
    b = Battery("b", "bus", 50.0, 10.0, 10.0, min_soc_mwh=5.0)
    assert _clamp_soc(b, -1e-9) == 5.0
    assert _clamp_soc(b, 50.0000001) == 50.0
    assert _clamp_soc(b, 20.0) == 20.0


def test_safe_hold_dispatch_is_feasible_and_frozen():
    prob = control_problem()
    real_d, real_r = realize(prob, ForecastErrorModel(seed=2))
    plan = _safe_hold_dispatch(prob, 0, real_d, real_r, soc_now={"b1": 20.0})
    assert plan["charge"]["b1"] == 0.0 and plan["discharge"]["b1"] == 0.0
    assert plan["soc"]["b1"] == 20.0
    # demand covered where possible
    assert plan["unmet"]["bus"] >= 0.0


# =============================================================== config validation ====
def test_config_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ControlConfig(lookahead=0).validate()
    with pytest.raises(ValueError):
        ControlConfig(mode="nope").validate()
    with pytest.raises(ValueError):
        ControlConfig(failure_policy="nope").validate()
    with pytest.raises(ValueError):
        ControlConfig(battery_mode="nope").validate()
    with pytest.raises(ValueError):
        ControlConfig(cvar_confidence=1.0).validate()


# =============================================================== controller ====
@needs_solver
def test_rolling_horizon_runs_all_periods_deterministic():
    res = run_rolling_horizon(control_problem(periods=6), ControlConfig(lookahead=3))
    assert res.status == "completed"
    assert res.periods_executed == 6
    assert len(res.steps) == 6
    assert res.failed_replans == 0
    assert res.realized_operating_cost_usd > 0
    assert res.schema_version == CONTROL_SCHEMA_VERSION
    # each step records status, runtime, realized cost, battery SoC
    for step in res.steps:
        assert step.status == "optimal"
        assert step.runtime_seconds >= 0.0
        assert step.realized_cost_usd is not None
        assert "b1" in step.battery_soc_mwh
        assert set(step.executed) == {"dispatch", "charge", "discharge", "unmet"}


@needs_solver
def test_soc_is_carried_forward_not_reset():
    res = run_rolling_horizon(control_problem(periods=6), ControlConfig(lookahead=3))
    # SoC sequence must reflect the executed actions, i.e. it must change over time
    socs = [res.steps[t].battery_soc_mwh["b1"] for t in range(len(res.steps))]
    assert any(abs(socs[t] - socs[0]) > 1e-6 for t in range(1, len(socs)))


@needs_solver
def test_controller_is_reproducible():
    a = run_rolling_horizon(control_problem(periods=6), ControlConfig(lookahead=4, seed=11))
    b = run_rolling_horizon(control_problem(periods=6), ControlConfig(lookahead=4, seed=11))
    assert a.realized_operating_cost_usd == pytest.approx(b.realized_operating_cost_usd)
    assert [s.executed for s in a.steps] == [s.executed for s in b.steps]


@needs_solver
def test_no_future_information_leakage():
    """An executed decision at period k must not depend on realized values > k.

    We run the controller twice on the same seed, then overwrite the realized
    profiles for periods strictly after period 0 with a wildly different series
    (keeping the controller's *forecasts* re-derived from the modified realization,
    which is exactly what a real leak would exploit). Decisions at period 0 must be
    identical because at k=0 no realized future value is available.
    """
    prob = control_problem(periods=6)
    cfg = ControlConfig(lookahead=3, seed=21)
    model = ForecastErrorModel(demand_sigma=cfg.demand_sigma,
                               renewable_sigma=cfg.renewable_sigma, seed=cfg.seed)
    real_d, real_r = realize(prob, model)
    base = run_rolling_horizon(prob, cfg, realized_demand=real_d, realized_avail=real_r)

    # Corrupt every FUTURE realized period (>0); period 0 unchanged.
    corrupt_d = list(real_d)
    for t in range(1, prob.periods):
        corrupt_d[t] = {n: v * 3.0 + 100.0 for n, v in real_d[t].items()}
    corrupt_d = tuple(corrupt_d)
    corrupt = run_rolling_horizon(prob, cfg, realized_demand=corrupt_d, realized_avail=real_r)

    assert base.steps[0].executed == corrupt.steps[0].executed


@needs_mip_solver
def test_exclusive_mode_preserves_binary_exclusivity():
    res = run_rolling_horizon(control_problem(periods=6), ControlConfig(lookahead=3,
                                                                        battery_mode="exclusive"))
    assert res.status == "completed"
    assert res.periods_executed == 6
    for step in res.steps:
        for b in step.executed["charge"]:
            assert min(step.executed["charge"][b], step.executed["discharge"][b]) <= 1e-6


@needs_solver
def test_stochastic_mode_runs():
    res = run_rolling_horizon(control_problem(periods=6),
                              ControlConfig(lookahead=3, mode="stochastic", n_scenarios=8))
    assert res.status == "completed"
    assert res.periods_executed == 6
    assert res.failed_replans == 0


@needs_solver
def test_stochastic_execution_realises_commitment_without_fabricated_unmet():
    """A stochastic first-stage battery commitment must be executed against the
    realised conditions via a real re-dispatch; on a feasible problem the controller
    must not invent unserved energy."""
    res = run_rolling_horizon(control_problem(periods=6),
                              ControlConfig(lookahead=4, mode="stochastic", n_scenarios=10))
    assert res.realized_unmet_energy_mwh == pytest.approx(0.0, abs=1e-6)
    # every step has a full, network-consistent plan recorded
    for step in res.steps:
        assert set(step.executed["dispatch"])
        assert all(v >= -1e-9 for v in step.executed["dispatch"].values())


# =============================================================== failure handling ====
@needs_solver
def test_failure_policy_hold_recovers_and_records():
    # strict mode + no generation -> every replan is infeasible.
    res = run_rolling_horizon(infeasible_strict_problem(periods=4),
                              ControlConfig(lookahead=2, allow_unmet=False,
                                            failure_policy="hold"))
    assert res.status == "completed_with_recovery"
    assert res.failed_replans == 4
    assert res.recovered_replans == 4
    assert res.periods_executed == 4
    for step in res.steps:
        assert step.status == "recovered"
        assert step.failure_recovered is True
        # no fabricated plan objective
        assert step.objective_usd is None
        # realized cost is recomputed independently and is finite
        assert step.realized_cost_usd is not None


@needs_solver
def test_failure_policy_abort_stops_and_marks_not_executed():
    res = run_rolling_horizon(infeasible_strict_problem(periods=4),
                              ControlConfig(lookahead=2, allow_unmet=False,
                                            failure_policy="abort"))
    assert res.status == "aborted"
    assert res.periods_executed == 1  # aborted at first failed replan
    assert len(res.steps) == 1
    assert res.steps[0].failure_recovered is False


# =============================================================== comparison ====
@needs_solver
def test_compare_control_uses_identical_realized_conditions():
    prob = control_problem(periods=6)
    cmp = compare_control(prob, ControlConfig(lookahead=6, seed=4))
    assert cmp.controller.status in ("completed", "completed_with_recovery")
    assert cmp.baseline.status == "optimal"
    assert cmp.cost_difference_usd is not None
    # both directions consistent with the recorded totals
    assert cmp.cost_difference_usd == pytest.approx(
        cmp.controller.realized_operating_cost_usd - cmp.baseline.realized_operating_cost_usd)


@needs_solver
def test_full_lookahead_no_noise_matches_open_loop():
    """With no forecast error and a full-horizon look-ahead, receding-horizon
    control must reproduce the open-loop optimum (certainty equivalence)."""
    prob = control_problem(periods=6)
    cmp = compare_control(prob, ControlConfig(lookahead=6, demand_sigma=0.0,
                                              renewable_sigma=0.0))
    assert cmp.cost_difference_usd == pytest.approx(0.0, abs=1e-4)
    assert cmp.unmet_difference_mwh == pytest.approx(0.0, abs=1e-6)


@needs_solver
def test_myopic_worse_than_open_loop_without_noise():
    """A one-period look-ahead cannot prepare for a rising demand ramp, so it
    must cost strictly more than the full-horizon open-loop plan."""
    prob = control_problem(periods=6)
    cmp = compare_control(prob, ControlConfig(lookahead=1, demand_sigma=0.0,
                                              renewable_sigma=0.0))
    assert cmp.cost_difference_usd is not None
    assert cmp.cost_difference_usd > 0.0


@needs_solver
def test_open_loop_baseline_records_terminal_soc():
    prob = control_problem(periods=6, final_soc=10.0)
    base = run_open_loop_baseline(prob, config=ControlConfig(demand_sigma=0.0,
                                                             renewable_sigma=0.0))
    assert base.status == "optimal"
    assert base.terminal_soc_mwh["b1"] == pytest.approx(10.0, abs=1e-4)


# =============================================================== record shape ====
@needs_solver
def test_control_result_to_dict_roundtrip_fields():
    res = run_rolling_horizon(control_problem(periods=5), ControlConfig(lookahead=2))
    d = res.to_dict()
    for key in ("status", "mode", "failure_policy", "lookahead", "battery_mode", "solver",
                "periods_executed", "steps", "realized_operating_cost_usd",
                "realized_unmet_energy_mwh", "realized_battery_throughput_mwh",
                "realized_cvar_usd", "mean_solve_seconds", "max_solve_seconds",
                "failed_replans", "schema_version", "ok"):
        assert key in d
    assert isinstance(d["steps"], list) and len(d["steps"]) == 5
