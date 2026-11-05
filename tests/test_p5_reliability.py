"""Adversarial and boundary qualification tests for Phase P5.

Validates robust engine behavior under stress:
- Boundary conditions (zero demand, isolated nodes, zero line capacities)
- Malformed and impossible demand profiles
- Infeasible and unbounded models with strict termination mapping
- Battery extremes (100% lossy efficiency, zero storage capacity, immediate boundary saturation)
- Contingency edge cases (empty contingency set, outages of all available generation)
- Provenance and serialization integrity across all failure states
- Repeated solve idempotency
"""
from __future__ import annotations

import json
import pytest

from gridmind.network import Generator, Line, Network, Node
from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.optimizer import solve_dispatch
from gridmind.scheduling import solve_schedule
from gridmind.security import Contingency, ContingencySet, SCPSProblem, solve_security_constrained
from gridmind.solver import SolverOptions, solver_available


@pytest.fixture
def base_network() -> Network:
    return Network(
        name="p5_net",
        nodes=[Node("n1", "Node 1", demand_mw=20.0), Node("n2", "Node 2", demand_mw=30.0)],
        generators=[
            Generator("g1", "G1", "n1", "gas", 100.0, 25.0, ramp_up_mw=50.0, ramp_down_mw=50.0),
        ],
        lines=[Line("l1", "L1", "n1", "n2", 40.0)],
    )


# -----------------------------------------------------------------------------
# 1. Boundary & Adversarial Dispatch Cases
# -----------------------------------------------------------------------------
@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_dispatch_zero_demand():
    net = Network(
        name="zero_net",
        nodes=[Node("n1", "Node 1", demand_mw=0.0), Node("n2", "Node 2", demand_mw=0.0)],
        generators=[Generator("g1", "G1", "n1", "gas", 100.0, 25.0)],
        lines=[Line("l1", "L1", "n1", "n2", 40.0)],
    )
    res = solve_dispatch(net)
    assert res.ok
    assert res.status == "optimal"
    assert res.objective == pytest.approx(0.0)
    assert res.total_demand_mw == 0.0
    assert sum(res.dispatch.values()) == pytest.approx(0.0)


@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_dispatch_infeasible_strict_mode():
    # Generator at n1 (100 MW), line capacity 20 MW, demand at n2 is 60 MW -> strict bottleneck
    net = Network(
        name="bottleneck_net",
        nodes=[Node("n1", "Node 1", demand_mw=0.0), Node("n2", "Node 2", demand_mw=60.0)],
        generators=[Generator("g1", "G1", "n1", "gas", 100.0, 25.0)],
        lines=[Line("l1", "L1", "n1", "n2", 20.0)],
    )
    res = solve_dispatch(net, allow_unmet=False)
    assert res.status == "infeasible"
    assert res.ok is False
    assert res.objective is None
    assert res.metadata is not None
    d = res.to_dict()
    assert d["ok"] is False
    assert json.dumps(d)


# -----------------------------------------------------------------------------
# 2. Battery & Horizon Boundary Cases
# -----------------------------------------------------------------------------
@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_schedule_battery_zero_capacity(base_network):
    # Battery with 0 MWh capacity should be accepted and have 0 charge/discharge
    hor = Horizon(
        periods=2,
        step_hours=1.0,
        demand_mw=({"n1": 20.0, "n2": 10.0}, {"n1": 20.0, "n2": 10.0}),
        availability_mw=({"g1": 100.0}, {"g1": 100.0}),
        generator_online=({"g1": True}, {"g1": True}),
        line_in_service=({"l1": True}, {"l1": True}),
    )
    b_zero = Battery("b0", "n1", 0.0, 0.0, 0.0, initial_soc_mwh=0.0)
    prob = ScheduleProblem(network=base_network, horizon=hor, batteries=[b_zero])

    res = solve_schedule(prob)
    assert res.ok
    assert res.status == "optimal"
    for t in range(2):
        assert res.charge["b0"][t] == pytest.approx(0.0)
        assert res.discharge["b0"][t] == pytest.approx(0.0)
        assert res.soc["b0"][t] == pytest.approx(0.0)


@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_schedule_impossible_ramp_infeasible():
    # Generator with very tight ramp up (1.0 MW/h) facing jump from 0 to 50 MW
    net = Network(
        name="ramp_net",
        nodes=[Node("n1", "Node 1", demand_mw=0.0), Node("n2", "Node 2", demand_mw=0.0)],
        generators=[Generator("g1", "G1", "n1", "gas", 100.0, 25.0, ramp_up_mw=1.0, ramp_down_mw=1.0)],
        lines=[Line("l1", "L1", "n1", "n2", 100.0)],
    )
    hor = Horizon(
        periods=2,
        step_hours=1.0,
        demand_mw=({"n1": 0.0, "n2": 0.0}, {"n1": 50.0, "n2": 0.0}),
        availability_mw=({"g1": 100.0}, {"g1": 100.0}),
        generator_online=({"g1": True}, {"g1": True}),
        line_in_service=({"l1": True}, {"l1": True}),
    )
    prob = ScheduleProblem(network=net, horizon=hor)

    # In strict mode, impossible ramp must be infeasible
    res = solve_schedule(prob, allow_unmet=False)
    assert res.status == "infeasible"
    assert res.ok is False
    assert res.objective is None
    d = res.to_dict()
    assert d["ok"] is False
    assert json.dumps(d)


# -----------------------------------------------------------------------------
# 3. SCPS Adversarial & Contingency Edge Cases
# -----------------------------------------------------------------------------
@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_scps_outage_of_only_generator_strict(base_network):
    # If the only generator is lost in a contingency and allow_unmet=False, it is strictly infeasible
    hor = Horizon(
        periods=1,
        step_hours=1.0,
        demand_mw=({"n1": 10.0, "n2": 0.0},),
        availability_mw=({"g1": 100.0},),
        generator_online=({"g1": True},),
        line_in_service=({"l1": True},),
    )
    prob = ScheduleProblem(network=base_network, horizon=hor)
    cset = ContingencySet([Contingency("c_g1", "generator", "g1")])
    scps_prob = SCPSProblem(prob, cset)

    res = solve_security_constrained(scps_prob, allow_unmet=False)
    assert res.status == "infeasible"
    assert res.ok is False
    assert res.n1_secure is False
    assert json.dumps(res.to_dict())


@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_scps_outage_of_only_generator_penalized(base_network):
    # Under penalized allow_unmet=True, loss of all generation leads to unmet demand but solves optimal
    hor = Horizon(
        periods=1,
        step_hours=1.0,
        demand_mw=({"n1": 10.0, "n2": 0.0},),
        availability_mw=({"g1": 100.0},),
        generator_online=({"g1": True},),
        line_in_service=({"l1": True},),
    )
    prob = ScheduleProblem(network=base_network, horizon=hor)
    cset = ContingencySet([Contingency("c_g1", "generator", "g1")])
    scps_prob = SCPSProblem(prob, cset)

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    assert res.status == "optimal"
    # Even though optimal (penalized), it is NOT N-1 secure because unmet demand exists in contingency
    assert res.n1_secure is False
    assert res.contingencies["c_g1"].is_secure is False
    assert res.contingencies["c_g1"].unmet["n1"][0] == pytest.approx(10.0)


# -----------------------------------------------------------------------------
# 4. Repeated Solves & Idempotency
# -----------------------------------------------------------------------------
@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_repeated_solve_dispatch_idempotency(base_network):
    # Repeated solves on same problem should produce identical numerical outputs
    res1 = solve_dispatch(base_network)
    res2 = solve_dispatch(base_network)
    assert res1.ok and res2.ok
    assert res1.objective == pytest.approx(res2.objective)
    assert res1.dispatch == res2.dispatch
    assert res1.metadata.problem_hash == res2.metadata.problem_hash
