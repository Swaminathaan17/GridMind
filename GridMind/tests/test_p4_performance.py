"""Performance and scalability characterization tests for GridMind.

Follows the opt-in pattern: skipped unless GRIDMIND_RUN_PERF=1 is set.
Measures dispatch, multi-period LP/MIP scheduling, and SCPS scalability without
imposing fragile brittle timing assertions.
"""
from __future__ import annotations

import os
import time
import pytest

from gridmind.network import Generator, Line, Network, Node
from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.optimizer import solve_dispatch
from gridmind.scheduling import solve_schedule
from gridmind.security import Contingency, ContingencySet, SCPSProblem, solve_security_constrained
from gridmind.solver import solver_available


pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(
        os.environ.get("GRIDMIND_RUN_PERF") != "1",
        reason="set GRIDMIND_RUN_PERF=1 to run opt-in performance benchmarks",
    ),
]


def test_dispatch_scalability_characterization():
    if not solver_available():
        pytest.skip("No solver available")

    sizes = [10, 50, 100]
    results = []
    for sz in sizes:
        nodes = [Node(f"n{i}", f"Node {i}", demand_mw=10.0 + i) for i in range(sz)]
        gens = [Generator(f"g{i}", f"G{i}", f"n{i}", "gas", 50.0 + i, 20.0) for i in range(sz)]
        lines = [Line(f"l{i}", f"L{i}", f"n{i}", f"n{(i+1)%sz}", 100.0) for i in range(sz)]
        net = Network(name=f"net_{sz}", nodes=nodes, generators=gens, lines=lines)

        t0 = time.perf_counter()
        res = solve_dispatch(net)
        wall_sec = time.perf_counter() - t0

        assert res.ok
        assert res.status == "optimal"
        results.append((sz, wall_sec, res.runtime_seconds))

    # Guardrail: check that larger problems solve successfully and runtime is positive
    for sz, wall, solv in results:
        assert wall > 0.0
        assert solv > 0.0


def test_scheduling_lp_vs_mip_characterization():
    if not solver_available():
        pytest.skip("No solver available")

    n_nodes = 8
    periods = 12
    nodes = [Node(f"n{i}", f"Node {i}") for i in range(n_nodes)]
    gens = [Generator(f"g{i}", f"G{i}", f"n{i}", "gas", 50.0, 20.0) for i in range(n_nodes)]
    lines = [Line(f"l{i}", f"L{i}", f"n{i}", f"n{(i+1)%n_nodes}", 100.0) for i in range(n_nodes)]
    net = Network(name="sched_bench_net", nodes=nodes, generators=gens, lines=lines)

    demands = [{f"n{i}": 15.0 for i in range(n_nodes)} for _ in range(periods)]
    avails = [{f"g{i}": 50.0 for i in range(n_nodes)} for _ in range(periods)]
    onlines = [{f"g{i}": True for i in range(n_nodes)} for _ in range(periods)]
    in_serv = [{f"l{i}": True for i in range(n_nodes)} for _ in range(periods)]
    hor = Horizon(periods=periods, step_hours=1.0, demand_mw=tuple(demands), availability_mw=tuple(avails),
                  generator_online=tuple(onlines), line_in_service=tuple(in_serv))
    batts = [Battery("b0", "n0", 40.0, 15.0, 15.0, initial_soc_mwh=20.0)]
    prob = ScheduleProblem(network=net, horizon=hor, batteries=batts)

    res_lp = solve_schedule(prob, battery_mode="lp")
    assert res_lp.ok
    assert res_lp.status == "optimal"

    res_mip = solve_schedule(prob, battery_mode="exclusive")
    assert res_mip.ok
    assert res_mip.status == "optimal"
    # MIP objective is bounded by LP relaxation
    assert res_mip.objective >= res_lp.objective - 1e-4


def test_scps_contingency_scaling_characterization():
    if not solver_available():
        pytest.skip("No solver available")

    n_nodes = 6
    periods = 4
    nodes = [Node(f"n{i}", f"Node {i}") for i in range(n_nodes)]
    gens = [Generator(f"g{i}", f"G{i}", f"n{i}", "gas", 60.0, 25.0, ramp_up_mw=25.0, ramp_down_mw=25.0) for i in range(n_nodes)]
    lines = [Line(f"l{i}", f"L{i}", f"n{i}", f"n{(i+1)%n_nodes}", 120.0) for i in range(n_nodes)]
    net = Network(name="scps_bench_net", nodes=nodes, generators=gens, lines=lines)

    demands = [{f"n{i}": 12.0 for i in range(n_nodes)} for _ in range(periods)]
    avails = [{f"g{i}": 60.0 for i in range(n_nodes)} for _ in range(periods)]
    onlines = [{f"g{i}": True for i in range(n_nodes)} for _ in range(periods)]
    in_serv = [{f"l{i}": True for i in range(n_nodes)} for _ in range(periods)]
    hor = Horizon(periods=periods, step_hours=1.0, demand_mw=tuple(demands), availability_mw=tuple(avails),
                  generator_online=tuple(onlines), line_in_service=tuple(in_serv))
    batts = [Battery("b0", "n0", 40.0, 10.0, 10.0, initial_soc_mwh=20.0)]
    prob = ScheduleProblem(network=net, horizon=hor, batteries=batts)

    conts = [Contingency(f"c_g{i}", "generator", f"g{i}") for i in range(3)]
    scps_prob = SCPSProblem(prob, ContingencySet(conts))

    res = solve_security_constrained(scps_prob)
    assert res.ok
    assert res.status == "optimal"
    assert res.n1_secure is True
    assert len(res.contingencies) == 3
