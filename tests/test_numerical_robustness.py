"""Stage 5E numerical robustness across scales, ties and degenerate cases."""
from __future__ import annotations

import math

import pytest

from gridmind.multiperiod import Battery, Horizon, ScheduleProblem, constant_horizon
from gridmind.network import Generator, Network, Node
from gridmind.optimizer import solve_dispatch
from gridmind.scheduling import solve_schedule
from gridmind.validation import validate_solution
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")


def _single_node_network(demand: float, *, capacity: float | None = None,
                         cost: float = 10.0) -> Network:
    return Network(
        name="numerical",
        nodes=(Node("bus", "bus", demand),),
        generators=(Generator("g", "G", "bus", "gas",
                              capacity if capacity is not None else max(2.0 * demand, 1e-9),
                              cost),),
        lines=(),
        unmet_penalty_per_mwh=1000.0,
    )


def _assert_valid(result) -> None:
    assert result.status == "optimal", result.message
    assert result.validation is not None
    assert result.validation.passed, result.validation.failed()


def test_validation_tolerance_boundary_is_enforced():
    network = _single_node_network(1.0, capacity=2.0, cost=1.0)
    within = validate_solution(network, {"g": 1.0}, {}, {"bus": 1e-7}, tol=1e-5)
    outside = validate_solution(network, {"g": 1.0}, {}, {"bus": 1e-3}, tol=1e-5)
    assert within.passed is True
    assert outside.passed is False
    assert any(check.name == "power_balance" and not check.passed
               for check in outside.checks)


@needs_solver
def test_tiny_and_large_demand_scales_remain_finite_and_valid():
    tiny = solve_dispatch(_single_node_network(1e-6, capacity=1e-3, cost=1.0))
    large = solve_dispatch(_single_node_network(1e6, capacity=2e6, cost=1e-6))
    _assert_valid(tiny)
    _assert_valid(large)
    assert math.isfinite(tiny.objective) and tiny.objective == pytest.approx(1e-6, rel=1e-6)
    assert math.isfinite(large.objective) and large.objective == pytest.approx(1.0, rel=1e-6)


@needs_solver
def test_ill_conditioned_cost_range_still_selects_the_cheap_generator():
    network = Network(
        name="ill-conditioned",
        nodes=(Node("bus", "bus", 5.0),),
        generators=(
            Generator("cheap", "Cheap", "bus", "gas", 10.0, 1e-6),
            Generator("expensive", "Expensive", "bus", "gas", 10.0, 1e6),
        ),
        lines=(),
        unmet_penalty_per_mwh=1e9,
    )
    result = solve_dispatch(network)
    _assert_valid(result)
    assert result.dispatch["cheap"] == pytest.approx(5.0, abs=1e-5)
    assert abs(result.dispatch["expensive"]) <= 1e-6
    assert result.objective == pytest.approx(5e-6, abs=1e-9)


@needs_solver
def test_zero_demand_is_degenerate_but_valid():
    result = solve_dispatch(_single_node_network(0.0, capacity=10.0, cost=10.0))
    _assert_valid(result)
    assert abs(result.dispatch["g"]) <= 1e-7
    assert result.objective == pytest.approx(0.0, abs=1e-7)


@needs_solver
def test_strict_infeasibility_does_not_fabricate_a_solution():
    result = solve_dispatch(_single_node_network(100.0, capacity=1.0), allow_unmet=False)
    assert result.status != "optimal"
    assert result.objective is None
    assert result.dispatch == {}
    assert result.validation is None


@needs_solver
def test_repeated_solves_are_deterministic():
    network = _single_node_network(7.0, capacity=10.0, cost=3.0)
    first = solve_dispatch(network)
    second = solve_dispatch(network)
    _assert_valid(first)
    _assert_valid(second)
    assert first.objective == pytest.approx(second.objective, abs=1e-12)
    assert first.dispatch.keys() == second.dispatch.keys()
    for key in first.dispatch:
        assert first.dispatch[key] == pytest.approx(second.dispatch[key], abs=1e-12)


def _schedule_problem(demand: float, periods: int = 3) -> ScheduleProblem:
    network = _single_node_network(demand, capacity=max(2.0 * demand, 1e-9), cost=10.0)
    return constant_horizon(network, periods=periods)


@needs_solver
def test_schedule_demand_scaling_preserves_validation():
    small = solve_schedule(_schedule_problem(1e-3))
    large = solve_schedule(_schedule_problem(1e3))
    _assert_valid(small)
    _assert_valid(large)
    assert small.objective == pytest.approx(0.03, rel=1e-6)
    assert large.objective == pytest.approx(30000.0, rel=1e-6)
    assert small.objective * 1e6 == pytest.approx(large.objective, rel=1e-6)


@needs_solver
def test_mip_with_near_zero_battery_power_stays_valid_and_exclusive():
    network = _single_node_network(1.0, capacity=2.0, cost=10.0)
    horizon = Horizon(
        periods=4, step_hours=1.0,
        demand_mw=tuple({"bus": 1.0} for _ in range(4)),
        availability_mw=tuple({"g": 2.0} for _ in range(4)),
        generator_online=tuple({"g": True} for _ in range(4)),
        line_in_service=tuple({} for _ in range(4)))
    battery = Battery("b", "bus", capacity_mwh=1e-6, max_charge_mw=1e-6,
                      max_discharge_mw=1e-6, initial_soc_mwh=0.0, final_soc_mwh=0.0)
    problem = ScheduleProblem(network=network, horizon=horizon, batteries=(battery,))
    problem.validate()
    result = solve_schedule(problem, battery_mode="exclusive")
    _assert_valid(result)
    assert result.validation.battery_exclusive is True
    assert result.objective == pytest.approx(40.0, rel=1e-6)
    assert max(abs(v) for v in result.charge["b"]) <= 1e-6
    assert max(abs(v) for v in result.discharge["b"]) <= 1e-6
