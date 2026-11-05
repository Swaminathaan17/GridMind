"""Tests that need Pyomo + GLPK (skipped, with a reason, when unavailable)."""
import pytest

from gridmind.analysis import binding_constraints, congestion_analysis, overloaded_lines, run_sensitivity
from gridmind.benchmark import evaluate_scenario, run_benchmark
from gridmind.network import sample_network
from gridmind.optimizer import solve_dispatch
from gridmind.scenarios import apply_scenario, generate_random_scenarios, get_scenario, scale_demand
from helpers import SOLVER_MSG, SOLVER_OK, tiny_network, two_gen_network

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")
TOL = 1e-4


@needs_solver
def test_hand_computed_optimum_with_unmet_demand():
    # 30 MW fits through the line; 20 MW unserved: 30*10 + 20*1000
    res = solve_dispatch(tiny_network(line_cap=30.0, demand=50.0))
    assert res.status == "optimal" and res.ok
    assert res.objective == pytest.approx(20300.0, abs=1e-3)
    assert res.dispatch["cheap"] == pytest.approx(30.0, abs=TOL)
    assert res.unmet["B"] == pytest.approx(20.0, abs=TOL)


@needs_solver
def test_hand_computed_optimum_line_limited_local_backup():
    res = solve_dispatch(two_gen_network())  # 30 MW via line @10, 50 MW local @50
    assert res.objective == pytest.approx(30 * 10 + 50 * 50, abs=1e-3)
    assert res.flows["AB"] == pytest.approx(30.0, abs=TOL)
    assert res.unmet_demand_mw == pytest.approx(0.0, abs=TOL)


@needs_solver
def test_penalty_is_configurable():
    cheap_penalty = solve_dispatch(tiny_network(penalty=5.0))  # unmet (5) cheaper than gen (10)
    assert cheap_penalty.dispatch["cheap"] == pytest.approx(0.0, abs=TOL)
    assert cheap_penalty.objective == pytest.approx(50 * 5.0, abs=1e-3)


@needs_solver
def test_normal_scenario_satisfies_all_constraints():
    net = sample_network()
    res = solve_dispatch(net)
    assert res.status == "optimal" and res.validation.passed, res.validation.failed()
    assert res.unmet_demand_mw == pytest.approx(0.0, abs=TOL)
    for g in net.generators:
        assert -TOL <= res.dispatch[g.id] <= g.effective_limit_mw + TOL
    for l in net.lines:
        assert abs(res.flows[l.id]) <= l.capacity_mw + TOL
    assert overloaded_lines(net, res.flows) == []


@needs_solver
def test_line_outage_changes_optimal_dispatch():
    base = sample_network()
    normal = solve_dispatch(base)
    out_net = apply_scenario(base, get_scenario("line_outage"))
    outage = solve_dispatch(out_net)
    assert outage.status == "optimal" and outage.validation.passed
    assert outage.flows["line-central-east"] == pytest.approx(0.0, abs=TOL)
    assert outage.dispatch["gas-east"] > normal.dispatch["gas-east"] + 1.0
    assert outage.dispatch["gas-north"] < normal.dispatch["gas-north"] - 1.0
    assert outage.objective > normal.objective


@needs_solver
def test_generator_outage_zeroes_output():
    net = apply_scenario(sample_network(), get_scenario("generator_outage"))
    res = solve_dispatch(net)
    assert res.status == "optimal" and res.dispatch["gas-north"] == pytest.approx(0.0, abs=TOL)


@needs_solver
def test_unserved_demand_when_network_cannot_deliver():
    net = apply_scenario(sample_network(), get_scenario("feeder_outage"))
    res = solve_dispatch(net)
    assert res.status == "optimal" and res.validation.passed
    assert res.unmet["central-zone"] == pytest.approx(165.0, abs=TOL)
    assert res.objective >= 165.0 * net.unmet_penalty_per_mwh


@needs_solver
def test_strict_mode_is_infeasible_and_returns_no_numbers():
    net = apply_scenario(sample_network(), get_scenario("feeder_outage"))
    res = solve_dispatch(net, allow_unmet=False)
    assert res.status == "infeasible" and not res.ok
    assert res.objective is None and res.dispatch == {} and res.flows == {} and res.unmet == {}


@needs_solver
def test_peak_and_low_renewable_scenarios_are_valid():
    for sid in ("peak_demand", "low_renewable"):
        res = solve_dispatch(apply_scenario(sample_network(), get_scenario(sid)))
        assert res.status == "optimal" and res.validation.passed, sid


@needs_solver
def test_binding_line_detected():
    net = sample_network()
    res = solve_dispatch(net)
    ids = {(b["type"], b["id"]) for b in binding_constraints(net, res)}
    assert ("line_capacity", "line-north-central") in ids  # 150 MW limit binds in the base case


@needs_solver
def test_congestion_analysis_reports_cost_of_congestion():
    info = congestion_analysis(sample_network())
    assert info["congestion_cost"] > 0
    assert any(b["line"] == "line-north-central" for b in info["bottleneck_lines"])


@needs_solver
def test_matches_independent_scipy_reference():
    pytest.importorskip("scipy")
    from reference_lp import reference_solve
    for sid in ("normal", "peak_demand", "low_renewable", "line_outage", "generator_outage", "feeder_outage"):
        net = apply_scenario(sample_network(), get_scenario(sid))
        ref = reference_solve(net)
        res = solve_dispatch(net)
        assert ref.status == 0 and res.status == "optimal"
        assert res.objective == pytest.approx(ref.fun, rel=1e-6, abs=1e-4), sid


@needs_solver
def test_demand_sensitivity_is_monotone():
    df = run_sensitivity(sample_network(), "demand", (0.8, 1.0, 1.2))
    assert (df["status"] == "optimal").all()
    assert df["objective_usd"].is_monotonic_increasing


@needs_solver
def test_line_capacity_sensitivity_never_hurts():
    df = run_sensitivity(sample_network(), "line_capacity", (0.5, 1.0, 1.5), target="line-north-central")
    assert df["objective_usd"].is_monotonic_decreasing or (df["objective_usd"].diff().dropna() <= 1e-6).all()


@needs_solver
def test_benchmark_rows_and_determinism():
    net = sample_network()
    scen = generate_random_scenarios(net, 5, seed=3)
    a, b = run_benchmark(net, scen), run_benchmark(net, scen)
    assert list(a["objective_usd"]) == pytest.approx(list(b["objective_usd"]), rel=1e-9)
    for col in ("objective_usd", "runtime_s", "status", "unmet_mw"):
        assert col in a.columns
    assert (a["validation_passed"] == True).all()  # noqa: E712


@needs_solver
def test_evaluate_scenario_has_baseline_units():
    row = evaluate_scenario(sample_network(), get_scenario("normal"))
    assert row["baseline_cost_usd"] >= row["copperplate_bound_usd"]
    assert row["objective_usd"] >= row["copperplate_bound_usd"] - 1e-6
