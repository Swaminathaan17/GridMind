"""Multi-period scheduling tests: hand-computed optima, scipy cross-checks,
scenarios, resilience, data validation.

Hand-computed cases (arithmetic shown in each docstring) pin the LP to exact
numbers so a formulation regression cannot hide behind self-consistency.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gridmind.multiperiod import (Battery, Horizon, ScheduleProblem, constant_horizon,
                                  load_schedule_problem, sample_schedule_problem)
from gridmind.network import Generator, Network, Node
from gridmind.resilience import run_resilience_suite, schedule_metrics, storage_ablation
from gridmind.scenarios import (ALL_HORIZON_SCENARIOS, DemandSpike, HorizonScenario,
                                OutageWindow, RenewableDrop, apply_horizon_scenario,
                                generate_random_horizon_scenarios, get_horizon_scenario)
from gridmind.scheduling import build_schedule_model, solve_schedule
from gridmind.solver import solver_available
from gridmind.validation import validate_schedule
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")

_MIP_OK, _MIP_MSG = solver_available(mip=True)
needs_mip_solver = pytest.mark.skipif(not _MIP_OK, reason=f"no verified MIP solver: {_MIP_MSG}")

pytest.importorskip("scipy")
from reference_lp_mp import reference_solve_mp  # noqa: E402


# --------------------------------------------------------- problem builders ----
def arbitrage_problem(eta_charge: float = 1.0, eta_discharge: float = 1.0) -> ScheduleProblem:
    """2 periods, dt=1h, one node, demand 40 MW in both periods.

    'cheap' costs 10 USD/MWh and is available only in period 0 (availability
    100 MW then 0); 'expensive' costs 50 USD/MWh and is always available.
    A battery (50 MWh, 100 MW both ways, no min SoC, free terminal SoC = 0)
    can move cheap energy into the expensive period.
    """
    net = Network(
        name="arb",
        nodes=(Node("bus", "bus"),),
        generators=(Generator("cheap", "Cheap", "bus", "gas", 100.0, 10.0),
                    Generator("expensive", "Expensive", "bus", "gas", 100.0, 50.0)),
        lines=(),
        unmet_penalty_per_mwh=1000.0)
    hor = Horizon(
        periods=2, step_hours=1.0,
        demand_mw=({"bus": 40.0}, {"bus": 40.0}),
        availability_mw=({"cheap": 100.0, "expensive": 100.0},
                         {"cheap": 0.0, "expensive": 100.0}),
        generator_online=({"cheap": True, "expensive": True},) * 2,
        line_in_service=({}, {}))
    battery = Battery("b1", "bus", capacity_mwh=50.0, max_charge_mw=100.0, max_discharge_mw=100.0,
                      eta_charge=eta_charge, eta_discharge=eta_discharge,
                      initial_soc_mwh=0.0, min_soc_mwh=0.0, final_soc_mwh=0.0)
    return ScheduleProblem(network=net, horizon=hor, batteries=(battery,))


def ramp_problem(ramp_mw: float | None) -> ScheduleProblem:
    """3 periods, dt=1h, one node; demand 50/100/150 MW.

    'cheap' (10 USD/MWh, 100 MW) starts at 0 MW and may move only 'ramp_mw'
    per period; 'expensive' (100 USD/MWh, 200 MW) is unconstrained.
    With ramp 20: cheap can reach 20/40/60, so expensive covers 30/60/90 ->
    cost = 200+3000 + 400+6000 + 600+9000 = 19,200 USD.
    With ramp None: cheap covers 50/100/100 and expensive 0/0/50 ->
    cost = 500 + 1000 + 6000 = 7,500 USD.
    """
    net = Network(
        name="ramp",
        nodes=(Node("bus", "bus"),),
        generators=(Generator("cheap", "Cheap", "bus", "gas", 100.0, 10.0,
                              ramp_up_mw=ramp_mw, ramp_down_mw=ramp_mw),
                    Generator("expensive", "Expensive", "bus", "gas", 200.0, 100.0)),
        lines=(),
        unmet_penalty_per_mwh=5000.0)
    hor = Horizon(
        periods=3, step_hours=1.0,
        demand_mw=({"bus": 50.0}, {"bus": 100.0}, {"bus": 150.0}),
        availability_mw=({"cheap": 100.0, "expensive": 200.0},) * 3,
        generator_online=({"cheap": True, "expensive": True},) * 3,
        line_in_service=({}, {}, {}))
    return ScheduleProblem(network=net, horizon=hor, initial_dispatch_mw={"cheap": 0.0})


def sliced_sample_problem(periods: int) -> ScheduleProblem:
    """First 'periods' hours of the committed sample horizon."""
    full = sample_schedule_problem()
    hor = replace(
        full.horizon,
        periods=periods,
        demand_mw=full.horizon.demand_mw[:periods],
        availability_mw=full.horizon.availability_mw[:periods],
        generator_online=full.horizon.generator_online[:periods],
        line_in_service=full.horizon.line_in_service[:periods])
    out = replace(full, horizon=hor)
    out.validate()
    return out


def simultaneous_cycle_problem() -> ScheduleProblem:
    """Deterministic counterexample: the throughput tie-break does NOT prevent
    simultaneous charge/discharge.

    One period, dt=1h, one node with zero demand, one zero-cost generator.
    Battery: soc0=10, capacity=20, min_soc=0, final_soc=0, max_ch=max_dis=50,
    eta_charge=1.0, eta_discharge=0.5, throughput=0.5, eps=1e-3.

    Let ch, dis, gen >= 0 have gen + dis - ch = 0 (zero demand). The SoC step is
        e[0] = soc0 + eta_c*ch*dt - dis*dt/eta_d = 10 + ch - 2*dis = 0,
    so ch = 2*dis - 10. Balance with p >= 0 gives gen = ch - dis = dis - 10 >= 0,
    i.e. dis >= 10, hence ch >= 10. The SoC/terminal constraint therefore forces
    a simultaneous pair; the cheapest feasible point is dis = ch = 10 (objective
    0.5*10 + eps*(10+10) = 5.02). A pure discharge (ch=0, dis=5) satisfies the SoC
    but needs gen = -5 < 0, an infeasible negative generation.
    """
    net = Network(
        name="cycle",
        nodes=(Node("bus", "bus"),),
        generators=(Generator("free", "Free", "bus", "gas", 100.0, 0.0),),
        lines=(),
        unmet_penalty_per_mwh=1000.0)
    hor = Horizon(
        periods=1, step_hours=1.0,
        demand_mw=({"bus": 0.0},),
        availability_mw=({"free": 100.0},),
        generator_online=({"free": True},),
        line_in_service=({},))
    battery = Battery("b1", "bus", capacity_mwh=20.0, max_charge_mw=50.0, max_discharge_mw=50.0,
                      eta_charge=1.0, eta_discharge=0.5,
                      initial_soc_mwh=10.0, min_soc_mwh=0.0, final_soc_mwh=0.0,
                      throughput_cost_per_mwh=0.5)
    return ScheduleProblem(network=net, horizon=hor, batteries=(battery,),
                           tiebreak_epsilon=1e-3)


# ------------------------------------------------- hand-computed optima ----
@needs_solver
def test_battery_arbitrage_perfect_efficiency():
    """Eta=1: charge 40 MW in period 0 (cheap at 80 MW), discharge 40 MW in
    period 1 (expensive at 0). Cost = 10*80 + 50*0 = 800 USD exactly."""
    res = solve_schedule(arbitrage_problem())
    assert res.status == "optimal" and res.ok
    assert res.operating_cost_usd == pytest.approx(800.0, abs=1e-4)
    assert res.dispatch["cheap"] == pytest.approx([80.0, 0.0], abs=1e-4)
    assert res.dispatch["expensive"] == pytest.approx([0.0, 0.0], abs=1e-4)
    assert res.charge["b1"] == pytest.approx([40.0, 0.0], abs=1e-4)
    assert res.discharge["b1"] == pytest.approx([0.0, 40.0], abs=1e-4)
    assert res.soc["b1"] == pytest.approx([40.0, 0.0], abs=1e-4)
    assert res.unmet_energy_mwh == pytest.approx(0.0, abs=1e-6)


@needs_solver
def test_battery_arbitrage_efficiency_losses():
    """Eta_c = eta_d = 0.8: charge x=60 (cheap capacity binds: 40+60=100 MW),
    round-trip delivers 0.64*60 = 38.4 MWh, expensive covers the remaining
    1.6 MW. Cost = 10*100 + 50*1.6 = 1,080 USD exactly."""
    res = solve_schedule(arbitrage_problem(0.8, 0.8))
    assert res.status == "optimal" and res.ok
    assert res.operating_cost_usd == pytest.approx(1080.0, abs=1e-4)
    assert res.dispatch["cheap"] == pytest.approx([100.0, 0.0], abs=1e-4)
    assert res.dispatch["expensive"] == pytest.approx([0.0, 1.6], abs=1e-4)
    assert res.soc["b1"] == pytest.approx([48.0, 0.0], abs=1e-4)


@needs_solver
def test_no_battery_pays_full_price_spread():
    """Same problem without storage: 40*10 + 40*50 = 2,400 USD."""
    res = solve_schedule(arbitrage_problem().without_batteries())
    assert res.status == "optimal" and res.ok
    assert res.operating_cost_usd == pytest.approx(2400.0, abs=1e-4)


def test_reference_lp_shows_simultaneous_cycle_is_optimal():
    """Regression for the reported counterexample: with eta_c*eta_d < 1 the
    throughput tie-break does NOT exclude simultaneous charge/discharge.

    The independent scipy reference LP (same formulation) must return the forced
    simultaneous pair ch = dis = 10 with SoC 0 and objective 5.02 (see
    ``simultaneous_cycle_problem``). This test needs no Pyomo solver.
    """
    res = reference_solve_mp(simultaneous_cycle_problem())
    assert res["status"] == "optimal"
    assert res["charge"]["b1"] == pytest.approx([10.0], abs=1e-4)
    assert res["discharge"]["b1"] == pytest.approx([10.0], abs=1e-4)
    assert res["soc"]["b1"] == pytest.approx([0.0], abs=1e-4)
    assert res["objective"] == pytest.approx(5.02, abs=1e-4)


def test_validator_flags_simultaneous_cycle_solution():
    """The independent validator rejects the simultaneous pair the LP optimiser
    returns, contradicting the old 'every optimum is cycle-free' claim. This is
    the documented behaviour, so ``result.ok`` must be False on such a schedule.
    """
    problem = simultaneous_cycle_problem()
    dispatch = {"free": [0.0]}
    flows: dict[str, list[float]] = {}
    unmet = {"bus": [0.0]}
    charge = {"b1": [10.0]}
    discharge = {"b1": [10.0]}
    soc = {"b1": [0.0]}
    report = validate_schedule(problem, dispatch, flows, unmet, charge, discharge, soc)
    assert report.passed is False
    assert [c.name for c in report.failed()] == ["simultaneous_charge_discharge"]
    # facets separate physical/model feasibility from exclusivity
    assert report.physical_feasible is True
    assert report.model_valid is True
    assert report.battery_exclusive is False


def test_validator_exclusive_mode_same_stage1_outcome():
    """battery_mode does not change check outcomes in Stage 1; it only records
    that exclusivity was evaluated."""
    problem = simultaneous_cycle_problem()
    dispatch = {"free": [0.0]}
    unmet = {"bus": [0.0]}
    args = (problem, dispatch, {}, unmet, {"b1": [10.0]}, {"b1": [10.0]}, {"b1": [0.0]})
    rep = validate_schedule(*args, battery_mode="exclusive")
    assert rep.passed is False
    assert [c.name for c in rep.failed()] == ["simultaneous_charge_discharge"]
    assert rep.physical_feasible is True and rep.model_valid is True
    assert rep.battery_exclusive is False


def test_validator_early_return_leaves_exclusivity_none():
    """A short-circuit report must not infer exclusivity success from an empty
    category: battery_exclusive stays None."""
    problem = simultaneous_cycle_problem()
    report = validate_schedule(problem, {}, {}, {}, {}, {}, {})
    assert report.passed is False
    assert report.battery_exclusive is None
    assert "completeness" in {c.name for c in report.failed()}


@needs_solver
def test_pyomo_also_reports_simultaneous_cycle():
    """The Pyomo model shares the formulation, so its optimum also charges and
    discharges at once; ``ok`` is False because independent validation fails."""
    res = solve_schedule(simultaneous_cycle_problem())
    assert res.status == "optimal"
    assert res.charge["b1"][0] > 1e-6 and res.discharge["b1"][0] > 1e-6
    assert res.validation is not None and res.validation.passed is False
    assert res.ok is False


@needs_solver
def test_ramps_force_expensive_generation():
    """Ramp 20 MW/h from 0: cheap reaches 20/40/60, expensive covers the rest.
    Cost = 3,200 + 6,400 + 9,600 = 19,200 USD exactly."""
    res = solve_schedule(ramp_problem(20.0))
    assert res.status == "optimal" and res.ok
    assert res.operating_cost_usd == pytest.approx(19200.0, abs=1e-4)
    assert res.dispatch["cheap"] == pytest.approx([20.0, 40.0, 60.0], abs=1e-4)
    assert res.dispatch["expensive"] == pytest.approx([30.0, 60.0, 90.0], abs=1e-4)


@needs_solver
def test_without_ramps_cheaper_dispatch():
    """No ramp limits: cheap covers 50/100/100, expensive 0/0/50 ->
    500 + 1,000 + 6,000 = 7,500 USD; ramps cost 11,700 USD more."""
    free = solve_schedule(ramp_problem(None))
    limited = solve_schedule(ramp_problem(20.0))
    assert free.status == limited.status == "optimal"
    assert free.operating_cost_usd == pytest.approx(7500.0, abs=1e-4)
    assert limited.operating_cost_usd - free.operating_cost_usd == pytest.approx(11700.0, abs=1e-3)


@needs_solver
def test_constant_horizon_repeats_single_period_answer():
    """Flat 40 MW demand, cheap always available: 4 periods x 40 MW x 10 USD
    = 1,600 USD (no arbitrage incentive under flat prices and eps > 0)."""
    net = arbitrage_problem().without_batteries().network
    problem = constant_horizon(net, periods=4, step_hours=1.0)
    hor = replace(problem.horizon, demand_mw=tuple({"bus": 40.0} for _ in range(4)))
    problem = replace(problem, horizon=hor)
    problem.validate()
    res = solve_schedule(problem)
    assert res.status == "optimal" and res.ok
    assert res.operating_cost_usd == pytest.approx(1600.0, abs=1e-4)
    assert res.dispatch["cheap"] == pytest.approx([40.0] * 4, abs=1e-4)


# ------------------------------------------------------ failure handling ----
@needs_solver
def test_strict_mode_infeasible_reports_no_numbers():
    net = Network(
        name="short", nodes=(Node("bus", "bus", 100.0),),
        generators=(Generator("small", "Small", "bus", "gas", 50.0, 10.0),),
        lines=(), unmet_penalty_per_mwh=1000.0)
    problem = constant_horizon(net, periods=2, step_hours=1.0)
    res = solve_schedule(problem, allow_unmet=False)
    assert res.status == "infeasible"
    assert res.objective is None and res.operating_cost_usd is None
    assert res.dispatch == {} and res.charge == {} and res.soc == {}
    assert res.validation is None


def test_explicit_bad_solver_env_is_reported_not_fallback(monkeypatch):
    monkeypatch.setenv("GRIDMIND_SOLVER", "not-a-real-solver")
    monkeypatch.delenv("GRIDMIND_SOLVER_PATH", raising=False)
    res = solve_schedule(arbitrage_problem())
    assert res.status == "solver_unavailable"
    assert res.objective is None and res.operating_cost_usd is None


@needs_solver
def test_solve_schedule_fails_cleanly_on_invalid_problem():
    bad = arbitrage_problem()
    broken_horizon = replace(bad.horizon, periods=3)
    broken = replace(bad, horizon=broken_horizon)
    res = solve_schedule(broken)
    assert res.status == "error"
    assert res.objective is None and res.operating_cost_usd is None


# ------------------------------------------------------- scipy cross-check ----
@needs_solver
@pytest.mark.parametrize("problem_factory", [
    lambda: arbitrage_problem(),
    lambda: arbitrage_problem(0.8, 0.8),
    lambda: ramp_problem(20.0),
    lambda: ramp_problem(None),
    lambda: sliced_sample_problem(6),
], ids=["arbitrage-eta1", "arbitrage-eta08", "ramp-20", "ramp-none", "sample-6h"])
def test_pyomo_matches_independent_scipy_reference(problem_factory):
    problem = problem_factory()
    res = solve_schedule(problem)
    ref = reference_solve_mp(problem)
    assert res.status == "optimal" and ref["status"] == "optimal"
    assert res.objective == pytest.approx(ref["objective"], rel=1e-6, abs=1e-4)
    assert res.operating_cost_usd == pytest.approx(ref["operating_cost_usd"], rel=1e-6, abs=1e-4)
    for g in res.dispatch:
        assert res.dispatch[g] == pytest.approx(ref["dispatch"][g], abs=1e-4)
    for b in res.soc:
        assert res.soc[b] == pytest.approx(ref["soc"][b], abs=1e-4)
        assert res.charge[b] == pytest.approx(ref["charge"][b], abs=1e-4)
        assert res.discharge[b] == pytest.approx(ref["discharge"][b], abs=1e-4)
    for n in res.unmet:
        assert res.unmet[n] == pytest.approx(ref["unmet"][n], abs=1e-4)


@needs_solver
def test_scipy_reference_agrees_on_sample_day():
    problem = sample_schedule_problem()
    res = solve_schedule(problem)
    ref = reference_solve_mp(problem)
    assert res.status == ref["status"] == "optimal"
    assert res.objective == pytest.approx(ref["objective"], rel=1e-6, abs=1e-3)
    assert res.unmet_energy_mwh == pytest.approx(
        problem.step_hours * sum(sum(s) for s in ref["unmet"].values()), abs=1e-4)


# ------------------------------------------------------------- scenarios ----
@needs_solver
def test_stress_scenarios_never_cost_less_than_normal():
    problem = sample_schedule_problem()
    base = solve_schedule(problem)
    assert base.status == "optimal"
    for scenario in ALL_HORIZON_SCENARIOS:
        if scenario.id == "h-feeder-loss":
            continue  # drops load, so cost falls by construction
        stressed = solve_schedule(apply_horizon_scenario(problem, scenario))
        assert stressed.status == "optimal", scenario.id
        assert stressed.operating_cost_usd >= base.operating_cost_usd - 1e-6, scenario.id


@needs_solver
def test_feeder_loss_leaves_central_unserved():
    """The central feeder is out all day: central demand is physically
    unreachable, so it is fully unserved while the rest of the grid is fine."""
    problem = sample_schedule_problem()
    res = solve_schedule(apply_horizon_scenario(problem, get_horizon_scenario("h-feeder-loss")))
    assert res.status == "optimal" and res.ok
    assert res.unmet_energy_mwh > 100.0
    central_id = "central-zone"
    assert central_id in res.unmet
    dt = res.step_hours
    central_unmet = dt * sum(res.unmet[central_id])
    other_unmet = dt * sum(v for n, s in res.unmet.items() if n != central_id for v in s)
    assert central_unmet > 0.5 * res.unmet_energy_mwh
    assert other_unmet == pytest.approx(0.0, abs=1e-4)


@needs_solver
def test_evening_spike_creates_unmet_energy():
    res = solve_schedule(apply_horizon_scenario(sample_schedule_problem(),
                                               get_horizon_scenario("h-evening-spike")))
    assert res.status == "optimal" and res.ok
    assert res.unmet_energy_mwh > 1.0
    assert any(t >= 18 for t in res.max_unmet_periods)


def test_random_horizon_scenarios_are_seed_deterministic():
    problem = sample_schedule_problem()
    a = generate_random_horizon_scenarios(problem, 5, seed=11)
    b = generate_random_horizon_scenarios(problem, 5, seed=11)
    assert a == b
    assert generate_random_horizon_scenarios(problem, 5, seed=12) != a


def test_horizon_scenario_validates_windows_and_references():
    problem = sample_schedule_problem()
    with pytest.raises(ValueError, match="window"):
        apply_horizon_scenario(problem, HorizonScenario(
            "bad", "Bad", "", demand_spikes=(DemandSpike(20, 30, 1.2),)))
    with pytest.raises(KeyError):
        apply_horizon_scenario(problem, HorizonScenario(
            "bad2", "Bad", "", generator_outages=(OutageWindow("no-such-gen", 0, 2),)))
    with pytest.raises(ValueError, match="not renewable"):
        apply_horizon_scenario(problem, HorizonScenario(
            "bad3", "Bad", "", renewable_drops=(RenewableDrop(0, 2, 0.5, generator="gas-north"),)))


def test_horizon_scenario_returns_new_problem():
    problem = sample_schedule_problem()
    stressed = apply_horizon_scenario(problem, get_horizon_scenario("h-line-storm"))
    assert stressed is not problem
    t = 12
    assert stressed.horizon.line_in_service[t]["line-central-east"] is False
    assert problem.horizon.line_in_service[t]["line-central-east"] is True


# ------------------------------------------------------- resilience ----
@needs_solver
def test_storage_ablation_storage_helps_compound_stress():
    problem = sample_schedule_problem()
    row = storage_ablation(problem, get_horizon_scenario("h-compound"))
    with_s, without_s = row["with_storage"], row["without_storage"]
    assert with_s["status"] == without_s["status"] == "optimal"
    assert with_s["operating_cost_usd"] <= without_s["operating_cost_usd"] + 1e-6
    assert with_s["unmet_energy_mwh"] <= without_s["unmet_energy_mwh"] + 1e-6
    assert row["cost_savings_usd"] >= -1e-6
    assert with_s["validation_passed"] is True and without_s["validation_passed"] is True


@needs_solver
def test_resilience_suite_shape_and_metrics():
    problem = sample_schedule_problem()
    scenarios = [get_horizon_scenario("h-normal"), get_horizon_scenario("h-compound")]
    df = run_resilience_suite(problem, scenarios)
    assert len(df) == 4
    assert set(df["storage"]) == {"with_storage", "without_storage"}
    normal_with = df[(df.scenario_id == "h-normal") & (df.storage == "with_storage")].iloc[0]
    assert normal_with["demand_served_pct"] == pytest.approx(100.0, abs=1e-6)
    assert bool(normal_with["validation_passed"]) is True


@needs_solver
def test_schedule_metrics_fields_present_and_consistent():
    problem = sample_schedule_problem()
    res = solve_schedule(problem)
    m = schedule_metrics(problem, res)
    assert m["status"] == "optimal" and m["validation_passed"] is True
    assert m["total_demand_mwh"] == pytest.approx(problem.total_demand_mwh)
    assert m["unmet_energy_mwh"] == pytest.approx(0.0, abs=1e-6)
    assert m["renewable_curtailed_mwh"] >= -1e-6
    assert m["battery_simultaneous_periods"] == 0


@needs_solver
def test_schedule_metrics_on_failed_solve_returns_none_costs():
    net = Network(name="short", nodes=(Node("bus", "bus", 100.0),),
                  generators=(Generator("small", "Small", "bus", "gas", 50.0, 10.0),),
                  lines=(), unmet_penalty_per_mwh=1000.0)
    problem = constant_horizon(net, periods=2, step_hours=1.0)
    res = solve_schedule(problem, allow_unmet=False)
    m = schedule_metrics(problem, res)
    assert m["operating_cost_usd"] is None
    assert m["unmet_energy_mwh"] is None


# ---------------------------------------------------------- data model ----
def test_sample_horizon_json_round_trip():
    problem = sample_schedule_problem()
    again = ScheduleProblem.from_dict(problem.to_dict(), network=problem.network)
    assert again.to_dict() == problem.to_dict()


def test_sample_horizon_file_loads_and_validates():
    problem = load_schedule_problem(Path(__file__).resolve().parents[1] / "data" / "sample_horizon.json")
    problem.validate()
    assert problem.periods == 24 and problem.step_hours == 1.0
    assert {b.id for b in problem.batteries} == {"battery-central", "battery-south"}


def test_schedule_problem_validation_rejects_bad_data():
    problem = arbitrage_problem()
    with pytest.raises(ValueError, match="periods"):
        replace(problem, horizon=replace(problem.horizon, periods=0)).validate()
    with pytest.raises(ValueError, match="step_hours"):
        replace(problem, horizon=replace(problem.horizon, step_hours=0.0)).validate()
    bad_batt = replace(problem.batteries[0], eta_charge=1.5)
    with pytest.raises(ValueError, match="efficiencies"):
        replace(problem, batteries=(bad_batt,)).validate()
    bad_batt2 = replace(problem.batteries[0], node="nowhere")
    with pytest.raises(ValueError, match="unknown node"):
        replace(problem, batteries=(bad_batt2,)).validate()
    with pytest.raises(ValueError, match="tiebreak"):
        replace(problem, tiebreak_epsilon=-1.0).validate()


def test_profile_shapes_accept_both_column_and_row_forms():
    problem = arbitrage_problem()
    data = problem.to_dict()
    rows_demand = [{"bus": 40.0}, {"bus": 40.0}]
    data["demand_mw"] = rows_demand
    rebuilt = ScheduleProblem.from_dict(data, network=problem.network)
    assert rebuilt.horizon.demand_mw == problem.horizon.demand_mw
    cols = {"bus": [40.0, 40.0]}
    data["demand_mw"] = cols
    assert ScheduleProblem.from_dict(data, network=problem.network).horizon.demand_mw == problem.horizon.demand_mw


# =============================================== Stage 3: battery exclusivity ====
# --- solver-free: formulation structure -------------------------------------
def test_lp_mode_has_no_binary_mode_variable_or_exclusivity_constraints():
    model = build_schedule_model(arbitrage_problem(), battery_mode="lp")
    assert not hasattr(model, "z")
    assert not hasattr(model, "exclusive_charge")
    assert not hasattr(model, "exclusive_discharge")


def test_exclusive_mode_adds_binary_z_and_tight_exclusivity_constraints():
    problem = arbitrage_problem()
    model = build_schedule_model(problem, battery_mode="exclusive")
    assert hasattr(model, "z") and hasattr(model, "exclusive_charge") and hasattr(model, "exclusive_discharge")
    b = problem.batteries[0]
    for t in range(problem.periods):
        assert model.z[b.id, t].is_binary()
        assert model.z[b.id, t].bounds == (0, 1)
    # tight big-M: coefficients equal the battery's own power limits
    for rule_name, cap in (("exclusive_charge", b.max_charge_mw), ("exclusive_discharge", b.max_discharge_mw)):
        body = str(getattr(model, rule_name)[b.id, 0].expr)
        assert str(cap) in body  # max power appears as the big-M


def test_invalid_battery_mode_raises_value_error():
    problem = arbitrage_problem()
    with pytest.raises(ValueError, match="battery_mode"):
        build_schedule_model(problem, battery_mode="nonsense")
    res = solve_schedule(problem, battery_mode="nonsense")
    assert res.status == "error" and res.termination_condition == "invalid_battery_mode"
    assert res.objective is None


def test_exclusive_mode_needs_no_z_when_no_batteries():
    problem = arbitrage_problem().without_batteries()
    model = build_schedule_model(problem, battery_mode="exclusive")
    assert not hasattr(model, "z")  # no battery -> no binary variables, still solvable


def test_exclusive_model_is_lp_plus_exclusivity_constraints():
    """The MIP is the LP plus binary constraints; all LP constraints are retained
    and exactly two exclusivity constraint blocks are added."""
    import pyomo.environ as pyo

    def constraint_names(model):
        return {c.name for c in model.component_objects(pyo.Constraint)}

    problem = arbitrage_problem()
    lp = build_schedule_model(problem, battery_mode="lp")
    ex = build_schedule_model(problem, battery_mode="exclusive")
    assert constraint_names(lp) <= constraint_names(ex)
    assert constraint_names(ex) - constraint_names(lp) == {"exclusive_charge", "exclusive_discharge"}


# --- fake MIP solver: end-to-end without a real binary backend --------------
class _FakeMIPSolver:
    """Solver stub that returns a caller-supplied solution snapshot.

    The provided function sets the model's variable values; solution loading is
    neutralised (as Pyomo's exact results container is irrelevant here)."""

    def __init__(self, setter):
        self._setter = setter
        self.name = "fake_mip"

    def solve(self, model, *args, **kwargs):
        from pyomo.opt import SolverStatus, TerminationCondition

        self._setter(model)

        class _Solver:
            status = SolverStatus.ok
            termination_condition = TerminationCondition.optimal

        class _Results:
            pass

        r = _Results()
        r.solver = _Solver()
        return r


def _install_fake_solver(monkeypatch, solver):
    from pyomo.core.base.PyomoModel import ModelSolutions
    from gridmind.scheduling import resolve_solver
    monkeypatch.setattr("gridmind.scheduling.resolve_solver", lambda mip=False: ("fake_mip", solver))
    monkeypatch.setattr(ModelSolutions, "load_from", lambda self, *a, **k: None)


def test_fake_mip_exclusive_solve_respects_exclusivity(monkeypatch):
    """End-to-end: a MIP-consistent solution (charge-only then discharge-only)
    solves, validates and reports battery_exclusive=True."""
    problem = arbitrage_problem()  # cheap in period 0, expensive in period 1

    def setter(model):
        # charge 40 MW in period 0, discharge 40 MW in period 1 (eta=1 -> lossless)
        model.p["cheap", 0].set_value(80.0); model.p["cheap", 1].set_value(0.0)
        model.p["expensive", 0].set_value(0.0); model.p["expensive", 1].set_value(0.0)
        model.ch["b1", 0].set_value(40.0); model.ch["b1", 1].set_value(0.0)
        model.dis["b1", 0].set_value(0.0); model.dis["b1", 1].set_value(40.0)
        model.e["b1", 0].set_value(40.0); model.e["b1", 1].set_value(0.0)
        model.u["bus", 0].set_value(0.0); model.u["bus", 1].set_value(0.0)
        model.z["b1", 0].set_value(0.0); model.z["b1", 1].set_value(1.0)

    _install_fake_solver(monkeypatch, _FakeMIPSolver(setter))
    res = solve_schedule(problem, battery_mode="exclusive")
    assert res.status == "optimal"
    assert res.battery_mode == "exclusive"
    assert res.validation is not None and res.validation.passed
    assert res.validation.battery_exclusive is True
    assert res.charge["b1"] == pytest.approx([40.0, 0.0], abs=1e-6)
    assert res.discharge["b1"] == pytest.approx([0.0, 40.0], abs=1e-6)


def test_fake_mip_exclusive_detects_simultaneous_pair_from_solution(monkeypatch):
    """A (non-MIP) solution that charges and discharges in the same period must
    fail independent validation and report battery_exclusive=False."""
    problem = simultaneous_cycle_problem()

    def setter(model):
        model.p["free", 0].set_value(0.0)
        model.ch["b1", 0].set_value(10.0)
        model.dis["b1", 0].set_value(10.0)
        model.e["b1", 0].set_value(0.0)
        model.u["bus", 0].set_value(0.0)
        model.z["b1", 0].set_value(0.0)

    _install_fake_solver(monkeypatch, _FakeMIPSolver(setter))
    res = solve_schedule(problem, battery_mode="exclusive")
    assert res.status == "optimal"
    assert res.validation is not None and res.validation.passed is False
    assert res.validation.battery_exclusive is False
    assert {c.name for c in res.validation.failed()} == {"simultaneous_charge_discharge"}
    assert res.ok is False


def test_exclusive_mode_reports_infeasible_without_numbers(monkeypatch):
    """When the binary model is infeasible the normal infeasibility contract must
    hold: no fabricated trajectories, no variable reads."""
    from pyomo.opt import SolverStatus, TerminationCondition

    class _InfeasibleSolver:
        name = "fake_mip"

        def solve(self, model, *args, **kwargs):
            class _Solver:
                status = SolverStatus.ok
                termination_condition = TerminationCondition.infeasible

            class _Results:
                pass

            r = _Results()
            r.solver = _Solver()
            return r

    from gridmind.scheduling import resolve_solver
    monkeypatch.setattr("gridmind.scheduling.resolve_solver",
                        lambda mip=False: ("fake_mip", _InfeasibleSolver()))
    res = solve_schedule(simultaneous_cycle_problem(), battery_mode="exclusive")
    assert res.status == "infeasible"
    assert res.objective is None and res.operating_cost_usd is None
    assert res.dispatch == {} and res.charge == {} and res.discharge == {} and res.soc == {}
    assert res.validation is None


def test_exclusive_mode_without_mip_solver_is_solver_unavailable(monkeypatch):
    """Explicit exclusive mode must not fall back to an LP-only solver."""
    monkeypatch.setenv("GRIDMIND_SOLVER", "not-a-real-solver")
    monkeypatch.delenv("GRIDMIND_SOLVER_PATH", raising=False)
    res = solve_schedule(arbitrage_problem(), battery_mode="exclusive")
    assert res.status == "solver_unavailable"
    assert res.battery_mode == "exclusive"
    assert res.objective is None and res.charge == {}


# --- real MIP solver (HiGHS/GLPK/CBC) ---------------------------------------
@needs_mip_solver
def test_real_exclusive_mode_optimal_and_validated():
    problem = arbitrage_problem()  # lossless arbitrage is feasible under exclusivity
    res = solve_schedule(problem, battery_mode="exclusive")
    assert res.status == "optimal", res.message
    assert res.validation is not None and res.validation.passed
    assert res.validation.battery_exclusive is True
    assert res.battery_mode == "exclusive"
    # no simultaneous operation anywhere
    for b in res.charge:
        for ch, dis in zip(res.charge[b], res.discharge[b]):
            assert min(ch, dis) <= 1e-6


@needs_mip_solver
def test_real_exclusive_objective_not_below_lp_objective():
    """Exclusivity only removes feasible points, so the exclusive optimum cannot
    beat the LP optimum (beyond tolerance)."""
    for factory in (arbitrage_problem, lambda: arbitrage_problem(0.8, 0.8)):
        problem = factory()
        lp = solve_schedule(problem, battery_mode="lp")
        ex = solve_schedule(problem, battery_mode="exclusive")
        assert lp.status == ex.status == "optimal"
        assert ex.objective >= lp.objective - 1e-6


@needs_mip_solver
def test_real_exclusive_cycle_fixture_is_infeasible():
    """The LP cycle counterexample needs a simultaneous pair, so it is infeasible
    under genuine exclusivity; the result must be the normal infeasible result."""
    res = solve_schedule(simultaneous_cycle_problem(), battery_mode="exclusive")
    assert res.status == "infeasible"
    assert res.objective is None and res.charge == {} and res.soc == {}
    assert res.validation is None


@needs_mip_solver
def test_real_sample_horizon_solves_in_both_modes():
    problem = sample_schedule_problem()
    lp = solve_schedule(problem, battery_mode="lp")
    ex = solve_schedule(problem, battery_mode="exclusive")
    assert lp.status == "optimal" and lp.validation.passed
    assert ex.status == "optimal" and ex.validation.passed
    assert ex.validation.battery_exclusive is True
    assert ex.objective >= lp.objective - 1e-3


@needs_mip_solver
def test_real_exclusive_zero_throughput_and_zero_power_boundaries():
    """Zero throughput cost and a zero-power battery: exclusivity still forbids
    simultaneous operation and a degenerate battery stays idle."""
    problem = arbitrage_problem()
    idle = replace(problem.batteries[0], max_charge_mw=0.0, max_discharge_mw=0.0)
    problem = replace(problem, batteries=(idle,), tiebreak_epsilon=0.0)
    problem.validate()
    res = solve_schedule(problem, battery_mode="exclusive")
    assert res.status == "optimal" and res.validation.passed
    assert res.charge["b1"] == pytest.approx([0.0] * problem.periods, abs=1e-6)
    assert res.discharge["b1"] == pytest.approx([0.0] * problem.periods, abs=1e-6)


@needs_mip_solver
def test_real_exclusive_multi_battery_exclusivity():
    problem = sample_schedule_problem()
    assert len(problem.batteries) >= 2
    res = solve_schedule(problem, battery_mode="exclusive")
    assert res.status == "optimal" and res.validation.battery_exclusive is True
    for b in res.charge:
        for ch, dis in zip(res.charge[b], res.discharge[b]):
            assert min(ch, dis) <= 1e-6
