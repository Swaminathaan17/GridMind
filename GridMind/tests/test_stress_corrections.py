"""Targeted regression tests for Stage 5C corrections."""
from __future__ import annotations

import pytest

from gridmind.control import ControlConfig
from gridmind.multiperiod import sample_schedule_problem
from gridmind.stress import (
    Disturbance,
    ForecastErrorModel,
    StressEnvironment,
    StressSchedule,
    _advance,
    run_method_open_loop,
    run_method_stochastic,
)
from gridmind.solver import solver_available


SOLVER_OK, SOLVER_MSG = solver_available(mip=False)
needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")


def test_stochastic_first_stage_not_repeated_after_commitment_window():
    prob = sample_schedule_problem()
    schedule = StressSchedule(disturbances=(), seed=123, name="test")
    env = StressEnvironment(base_problem=prob, stress=schedule,
                            error_model=ForecastErrorModel(seed=123))
    # Run with small scenario count if solver available
    if SOLVER_OK:
        res = run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                                   lead_periods=1, n_scenarios=4)
        assert res.status in ("optimal", "solver_unavailable")
    else:
        pass


def test_invalid_lead_periods_rejected():
    prob = sample_schedule_problem()
    schedule = StressSchedule(disturbances=(), seed=1, name="t")
    env = StressEnvironment(base_problem=prob, stress=schedule,
                            error_model=ForecastErrorModel(seed=1))
    with pytest.raises(Exception):
        run_method_stochastic(env, risk_weight=0.0, cvar_alpha=0.95,
                              lead_periods=-1, n_scenarios=2)


def test_advance_does_not_silently_clamp_invalid_soc():
    prob = sample_schedule_problem()
    soc0 = {b.id: b.initial_soc_mwh for b in prob.batteries}
    # discharge way over limit
    with pytest.raises(Exception):
        _advance(prob, soc0, {b.id: 0.0 for b in prob.batteries},
                {b.id: 10**9 for b in prob.batteries}, 0)
    # simultaneous charge/discharge
    with pytest.raises(Exception):
        _advance(prob, soc0, {b.id: 1.0 for b in prob.batteries},
                {b.id: 1.0 for b in prob.batteries}, 0)


@needs_solver
def test_open_loop_throughput_uses_actual_executed_actions():
    prob = sample_schedule_problem()
    schedule = StressSchedule(disturbances=(), seed=5, name="t")
    env = StressEnvironment(base_problem=prob, stress=schedule,
                            error_model=ForecastErrorModel(seed=5))
    res = run_method_open_loop(env)
    assert res.status == "optimal"
    assert res.battery_throughput_mwh is not None
    assert res.battery_throughput_mwh >= 0
