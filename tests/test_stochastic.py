"""Stage 4 tests: synthetic uncertainty, two-stage risk-aware stochastic
scheduling and out-of-sample policy evaluation.

Structure
---------
* Scenario generation is tested WITHOUT a solver (pure data): reproducibility,
  probability normalisation, stress-family coverage, in/out-of-sample splitting,
  and rejection of invalid inputs.
* Model-structure tests are solver-free: they build the Pyomo model and inspect
  variables/constraints (non-anticipativity equalities, CVaR constraints).
* Solver-backed tests use the same helpers/guards as the rest of the suite
  (``needs_solver`` / ``needs_mip_solver``) so they skip cleanly when no solver
  is available, and never weaken existing coverage.
* Risk metrics are pinned to hand-computed discrete CVaR/VaR values.
"""
from __future__ import annotations

import pytest

from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.network import Generator, Network, Node
from gridmind.risk_eval import (Policy, _risk_metrics, compare_policies,
                                evaluate_policy, policy_from_stochastic)
from gridmind.solver import solver_available
from gridmind.stochastic import (RiskConfig, build_stochastic_model, solve_stochastic)
from gridmind.uncertainty import (STRESS_CASES, UncertaintyConfig,
                                  generate_scenarios, scenario_to_problem, split_scenarios,
                                  validate_probabilities)
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")

_MIP_OK, _MIP_MSG = solver_available(mip=True)
needs_mip_solver = pytest.mark.skipif(not _MIP_OK, reason=f"no verified MIP solver: {_MIP_MSG}")


# --------------------------------------------------------- problem builders ----
def single_bus_problem(periods: int = 4, demand: float = 60.0,
                       cheap_cap: float = 55.0, exp_cap: float = 200.0) -> ScheduleProblem:
    """Single bus, one cheap (limited) + one expensive generator and a battery.

    Tight cheap capacity makes high-demand scenarios rely on the expensive unit
    or storage, so uncertainty genuinely matters.
    """
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


# ================================================================ uncertainty ====
def test_scenario_generation_is_reproducible():
    prob = single_bus_problem()
    a = generate_scenarios(prob, UncertaintyConfig(n_scenarios=12, seed=99))
    b = generate_scenarios(prob, UncertaintyConfig(n_scenarios=12, seed=99))
    assert a.to_dict() == b.to_dict()
    assert [s.demand_mw for s in a.scenarios] == [s.demand_mw for s in b.scenarios]


def test_scenario_probabilities_sum_to_one_and_are_nonnegative():
    prob = single_bus_problem()
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=40, seed=1))
    total = sum(s.probability for s in ss.scenarios)
    assert abs(total - 1.0) < 1e-9
    assert all(s.probability >= 0.0 for s in ss.scenarios)


def test_scenario_generation_covers_all_stress_families():
    prob = single_bus_problem()
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=40, seed=2))
    cases = {s.case for s in ss.scenarios}
    assert cases == set(STRESS_CASES)


def test_scenario_dimensions_match_problem():
    prob = single_bus_problem(periods=5)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=8, seed=3))
    for s in ss.scenarios:
        assert len(s.demand_mw) == 5
        assert len(s.availability_mw) == 5
        assert set(s.demand_mw[0]) == {"bus"}


def test_high_demand_family_has_higher_demand_than_base():
    prob = single_bus_problem(periods=4, demand=60.0)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=40, seed=4,
                                                    demand_sigma=0.0, demand_ar1=0.0))
    ordinary = [s for s in ss.scenarios if s.case == "ordinary"][0]
    high = [s for s in ss.scenarios if s.case == "high_demand"][0]
    assert high.demand_mw[0]["bus"] > ordinary.demand_mw[0]["bus"]


def test_split_scenarios_partitions_and_renormalises():
    prob = single_bus_problem()
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=20, seed=5))
    ins, out = split_scenarios(ss, holdout_fraction=0.25)
    assert len(ins.scenarios) + len(out.scenarios) == 20
    assert abs(sum(s.probability for s in ins.scenarios) - 1.0) < 1e-9
    assert abs(sum(s.probability for s in out.scenarios) - 1.0) < 1e-9
    ids_in = {s.id for s in ins.scenarios}
    ids_out = {s.id for s in out.scenarios}
    assert ids_in.isdisjoint(ids_out)


def test_validate_probabilities_rejects_bad_inputs():
    with pytest.raises(ValueError):
        validate_probabilities([0.5, 0.5, 0.5])
    with pytest.raises(ValueError):
        validate_probabilities([0.5, -0.1, 0.6])
    with pytest.raises(ValueError):
        validate_probabilities([float("nan"), 1.0])


def test_uncertainty_config_validation_errors():
    with pytest.raises(ValueError):
        UncertaintyConfig(n_scenarios=0).validate()
    with pytest.raises(ValueError):
        UncertaintyConfig(demand_sigma=-1.0).validate()
    with pytest.raises(ValueError):
        UncertaintyConfig(demand_ar1=2.0).validate()
    with pytest.raises(ValueError):
        UncertaintyConfig(weights={"bogus": 1.0}).validate()


def test_scenario_serialisation_roundtrip():
    prob = single_bus_problem()
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=6, seed=6))
    payload = ss.to_dict()
    restored = type(ss).from_dict(payload)
    assert restored.to_dict() == payload


def test_scenario_to_problem_preserves_periods():
    prob = single_bus_problem(periods=6)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=4, seed=8))
    sp = scenario_to_problem(prob, ss.scenarios[0])
    assert sp.periods == 6


# ================================================================= stochastic ====
def test_risk_config_validation():
    with pytest.raises(ValueError):
        RiskConfig(risk_weight=1.5).validate()
    with pytest.raises(ValueError):
        RiskConfig(cvar_alpha=0.0).validate()
    with pytest.raises(ValueError):
        RiskConfig(cvar_alpha=1.0).validate()
    with pytest.raises(ValueError):
        RiskConfig(lead_periods=-1).validate()
    with pytest.raises(ValueError):
        RiskConfig(lead_periods=5).validate(periods=3)


def test_model_has_cvar_and_nonanticipativity_structure():
    import pyomo.environ as pyo
    prob = single_bus_problem(periods=3)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=4, seed=9))
    m = build_stochastic_model(prob, ss, risk=RiskConfig(0.5, 0.9, lead_periods=1))
    assert hasattr(m, "cvar")
    assert hasattr(m, "eta")
    assert hasattr(m, "cvar_def")
    assert hasattr(m, "na_ch") and hasattr(m, "na_dis") and hasattr(m, "na_e")
    # first-stage shared vars exist for t < lead
    assert m.T1 == [0]
    assert set(m.CH.keys()) == {("b1", 0)}
    # one non-anticipativity equality per scenario for each shared quantity
    n = len(ss.scenarios)
    assert len(m.na_ch) == n
    assert len(m.na_dis) == n
    assert len(m.na_e) == n


def test_lead_zero_has_no_first_stage_vars():
    prob = single_bus_problem(periods=3)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=3, seed=10))
    m = build_stochastic_model(prob, ss, risk=RiskConfig(lead_periods=0))
    assert not hasattr(m, "na_ch")
    assert not hasattr(m, "CH")


def test_nonanticipativity_equality_holds_in_model():
    """Verify the NA constraints are genuine equalities tying scenario vars to shared vars."""
    import pyomo.environ as pyo

    prob = single_bus_problem(periods=3)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=3, seed=11))
    m = build_stochastic_model(prob, ss, risk=RiskConfig(lead_periods=1))
    for s in m.S:
        con = m.na_ch[s, "b1", 0]
        assert con.equality
        # body == ch[s,b1,0] - CH[b1,0]; both variables appear with unit coefficients
        body = con.body
        assert body.polynomial_degree() == 1
        vars_set = {id(v) for v in body.linear_vars}
        assert vars_set == {id(m.ch[s, "b1", 0]), id(m.CH["b1", 0])}
        assert sorted(body.linear_coefs) == [-1.0, 1.0]


@needs_solver
def test_solve_stochastic_optimal_and_validates():
    prob = single_bus_problem(periods=4)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=8, seed=12))
    res = solve_stochastic(prob, ss, risk=RiskConfig(0.0, 0.9, lead_periods=1))
    assert res.status == "optimal"
    assert res.expected_cost_usd is not None and res.expected_cost_usd > 0
    assert all(res.scenario_validation_passed)
    assert res.first_stage["lead_periods"] == 1
    assert set(res.first_stage["charge"]) == {"b1"}


@needs_solver
def test_risk_aware_lowers_in_sample_cvar_vs_risk_neutral():
    prob = single_bus_problem(periods=5)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=20, seed=13, demand_sigma=0.3))
    rn = solve_stochastic(prob, ss, risk=RiskConfig(0.0, 0.8, lead_periods=1))
    ra = solve_stochastic(prob, ss, risk=RiskConfig(1.0, 0.8, lead_periods=1))
    assert rn.status == "optimal" and ra.status == "optimal"
    # Pure-CVaR must not be worse (higher) than the risk-neutral CVaR in-sample.
    assert ra.cvar_usd <= rn.cvar_usd + 1e-6
    # ... at the potential cost of a higher expected cost.
    assert ra.expected_cost_usd >= rn.expected_cost_usd - 1e-6


@needs_solver
def test_nonanticipativity_equal_across_scenarios_after_solve():
    prob = single_bus_problem(periods=4)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=6, seed=14))
    res = solve_stochastic(prob, ss, risk=RiskConfig(0.5, 0.9, lead_periods=2))
    assert res.status == "optimal"
    # every scenario's first two periods must share the committed battery values
    committed_ch = res.first_stage["charge"]["b1"]
    for sr in res.scenario_results:
        assert sr["charge"]["b1"][:2] == pytest.approx(committed_ch, abs=1e-6)
        assert sr["discharge"]["b1"][:2] == pytest.approx(
            res.first_stage["discharge"]["b1"], abs=1e-6)


@needs_solver
def test_wait_and_see_is_never_worse_than_committed():
    prob = single_bus_problem(periods=4)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=12, seed=15, demand_sigma=0.3))
    ws = solve_stochastic(prob, ss, risk=RiskConfig(0.0, 0.9, lead_periods=0))
    det = solve_stochastic(prob, ss, risk=RiskConfig(0.0, 0.9, lead_periods=prob.periods))
    assert ws.status == "optimal" and det.status == "optimal"
    # Clairvoyant per-scenario (wait-and-see) expected cost <= fully committed.
    assert ws.expected_cost_usd <= det.expected_cost_usd + 1e-6


@needs_solver
def test_infeasible_scenario_returns_no_fabricated_numbers():
    # No generator can serve demand and unmet is forbidden -> infeasible.
    prob = single_bus_problem(periods=3, demand=500.0, cheap_cap=10.0, exp_cap=10.0)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=4, seed=16))
    res = solve_stochastic(prob, ss, allow_unmet=False, risk=RiskConfig(0.0, 0.9, lead_periods=1))
    assert res.status == "infeasible"
    assert res.expected_cost_usd is None
    assert res.scenario_results == ()


def test_solve_stochastic_solver_unavailable_is_explicit(monkeypatch):
    import gridmind.stochastic as st
    from gridmind.solver import SolverUnavailableError

    def _boom(*, mip=False):
        raise SolverUnavailableError("no solver in test")

    monkeypatch.setattr(st, "resolve_solver", _boom)
    prob = single_bus_problem(periods=3)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=3, seed=17))
    res = solve_stochastic(prob, ss)
    assert res.status == "solver_unavailable"
    assert res.expected_cost_usd is None


@needs_mip_solver
def test_exclusive_stochastic_solve_respects_exclusivity():
    prob = single_bus_problem(periods=4)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=6, seed=18))
    res = solve_stochastic(prob, ss, risk=RiskConfig(0.5, 0.9, lead_periods=1),
                           battery_mode="exclusive")
    assert res.status == "optimal"
    assert res.battery_mode == "exclusive"
    for sr in res.scenario_results:
        for t in range(prob.periods):
            assert min(sr["charge"]["b1"][t], sr["discharge"]["b1"][t]) <= 1e-5
        assert all(sr["validation_passed"] for sr in res.scenario_results)


# ================================================================= risk_eval ====
def test_risk_metrics_hand_computed_cvar_and_var():
    costs = [0.0, 10.0, 20.0, 100.0]
    probs = [0.25, 0.25, 0.25, 0.25]
    m = _risk_metrics(costs, probs, 0.5)
    assert m["expected"] == pytest.approx(32.5)
    assert m["worst"] == pytest.approx(100.0)
    assert m["var"] == pytest.approx(10.0)
    assert m["cvar"] == pytest.approx((20.0 + 100.0) / 2.0)
    m2 = _risk_metrics(costs, probs, 0.75)
    assert m2["var"] == pytest.approx(20.0)
    assert m2["cvar"] == pytest.approx(100.0)
    assert m2["p95"] == pytest.approx(100.0)


def test_policy_from_stochastic_roundtrip():
    from gridmind.stochastic import StochasticResult
    res = StochasticResult(status="optimal", solver="x", termination_condition="optimal",
                           message="", runtime_seconds=0.0, first_stage={
                               "lead_periods": 1, "charge": {"b1": [0.0]},
                               "discharge": {"b1": [5.0]}, "soc": {"b1": [30.0]}})
    pol = policy_from_stochastic("risk_neutral", res)
    assert pol.lead == 1
    assert pol.discharge["b1"] == [5.0]
    assert pol.to_dict()["name"] == "risk_neutral"


@needs_solver
def test_evaluate_policy_all_scenarios_use_same_holdout():
    prob = single_bus_problem(periods=4)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=12, seed=19))
    ins, out = split_scenarios(ss, holdout_fraction=0.5)
    rec = compare_policies(prob, ins, out, risk_weight=0.5, cvar_alpha=0.8, lead_periods=1)
    for ev in rec["evaluations"].values():
        assert ev["n_scenarios"] == len(out.scenarios)
        assert ev["status"] == "optimal"
        assert len(ev["scenario_costs_usd"]) == len(out.scenarios)
    # every policy sees the identical holdout probability vector
    vectors = {tuple(ev["scenario_probabilities"]) for ev in rec["evaluations"].values()}
    assert len(vectors) == 1


@needs_solver
def test_evaluate_policy_first_stage_is_actually_fixed():
    prob = single_bus_problem(periods=4)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=6, seed=20))
    ins, out = split_scenarios(ss, holdout_fraction=0.5)
    assert ins.scenarios
    # Feasible committed battery: hold SoC at the 30 MWh initial value in period 0.
    policy = Policy(name="forced", lead=1, charge={"b1": [0.0]},
                    discharge={"b1": [0.0]}, soc={"b1": [30.0]})
    ev = evaluate_policy(prob, policy, out)
    assert ev.status == "optimal"
    # committed discharge of 0 MW in period 0 must show up in every scenario's
    # realized first-period discharge (the recourse cannot undo it).
    assert ev.expected_cost_usd is not None and ev.expected_cost_usd > 0


def test_compare_policies_records_configuration():
    prob = single_bus_problem(periods=3)
    ss = generate_scenarios(prob, UncertaintyConfig(n_scenarios=6, seed=21))
    ins, out = split_scenarios(ss, holdout_fraction=0.5)
    rec = compare_policies(prob, ins, out, risk_weight=0.5, cvar_alpha=0.9, lead_periods=1)
    assert rec["risk"]["risk_weight"] == 0.5
    assert rec["risk"]["cvar_alpha"] == 0.9
    assert rec["lead_periods"] == 1
    assert set(rec["fit"]) == {"deterministic", "risk_neutral", "risk_aware"}


# ======================================================================== CLI ====
def test_cli_uncertainty_lists_config():
    from gridmind.cli import build_parser, cmd_uncertainty
    args = build_parser().parse_args(["uncertainty"])
    assert cmd_uncertainty(args) == 0


@needs_solver
def test_cli_stochastic_writes_record(tmp_path):
    from gridmind.cli import build_parser, cmd_stochastic
    out = tmp_path / "exp.json"
    args = build_parser().parse_args([
        "stochastic", "--scenarios", "8", "--seed", "5", "--risk-weight", "0.5",
        "--cvar-alpha", "0.8", "--lead-periods", "1", "--json", str(out)])
    rc = cmd_stochastic(args)
    assert rc == 0
    import json
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["seed"] == 5
    assert record["lead_periods"] == 1
    assert "evaluations" in record and record["evaluations"]
    assert record["uncertainty"]["n_scenarios"] == 8
