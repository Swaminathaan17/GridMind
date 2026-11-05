"""Out-of-sample evaluation of scheduling policies under uncertainty.

A *policy* is the set of first-stage commitments produced by a two-stage solve:
the committed generator output and battery charge/discharge/SoC for the first
``lead_periods`` periods. This module evaluates such a policy against a set of
*held-out* scenarios that were not used during optimisation.

Method (correct, non-clairvoyant):
    For each evaluation scenario the first-stage commitments are FIXED to the
    policy and the later periods are re-optimised against that scenario's
    realized demand/availability. The realized cost, unmet energy and validation
    results are then aggregated into risk statistics. Fixing the first stage is
    exactly what "committing to a policy before uncertainty resolves" means, so
    no evaluation scenario can influence an earlier decision.

Policies compared:
    * ``deterministic`` - solve the mean/base problem and commit its first stage.
    * ``risk_neutral``  - two-stage expected-cost solve (``risk_weight = 0``).
    * ``risk_aware``    - two-stage CVaR-weighted solve (``risk_weight = w > 0``).

All policies are evaluated on the SAME held-out scenario set with identical
probabilities, so the comparison is apples-to-apples.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from gridmind.multiperiod import ScheduleProblem
from gridmind.scheduling import normalize_battery_mode
from gridmind.solver import SolverUnavailableError, resolve_solver
from gridmind.stochastic import RiskConfig, _validate_scenario
from gridmind.uncertainty import ScenarioSet, UncertaintyScenario, scenario_to_problem

CLEAN_EPS = 1e-9


@dataclass(frozen=True)
class Policy:
    """A committed first-stage policy: battery charge/discharge/SoC for ``t < lead``.

    ``charge[b]``, ``discharge[b]`` and ``soc[b]`` are lists over the first
    ``lead_periods`` periods. A policy is produced by
    :func:`policy_from_stochastic` (or an empty policy with ``lead=0``).
    """
    name: str
    lead: int
    charge: Mapping[str, Sequence[float]] = field(default_factory=dict)
    discharge: Mapping[str, Sequence[float]] = field(default_factory=dict)
    soc: Mapping[str, Sequence[float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lead": self.lead,
                "charge": {k: list(v) for k, v in self.charge.items()},
                "discharge": {k: list(v) for k, v in self.discharge.items()},
                "soc": {k: list(v) for k, v in self.soc.items()}}


@dataclass
class PolicyEvaluation:
    """Out-of-sample metrics for one policy over one scenario set."""
    policy: str
    status: str
    n_scenarios: int
    solver: str = ""
    runtime_seconds: float = 0.0
    message: str = ""
    expected_cost_usd: float | None = None
    expected_unmet_energy_mwh: float | None = None
    probability_of_unmet: float | None = None
    worst_case_cost_usd: float | None = None
    var_usd: float | None = None            # (1-alpha) quantile of scenario costs
    cvar_usd: float | None = None           # mean of costs above the VaR
    p95_cost_usd: float | None = None
    scenario_costs_usd: tuple[float, ...] = ()
    scenario_unmet_mwh: tuple[float, ...] = ()
    scenario_probabilities: tuple[float, ...] = ()
    validation_all_passed: bool | None = None
    validation_failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy, "status": self.status, "n_scenarios": self.n_scenarios,
            "solver": self.solver, "runtime_seconds": self.runtime_seconds, "message": self.message,
            "expected_cost_usd": self.expected_cost_usd,
            "expected_unmet_energy_mwh": self.expected_unmet_energy_mwh,
            "probability_of_unmet": self.probability_of_unmet,
            "worst_case_cost_usd": self.worst_case_cost_usd,
            "var_usd": self.var_usd, "cvar_usd": self.cvar_usd, "p95_cost_usd": self.p95_cost_usd,
            "scenario_costs_usd": list(self.scenario_costs_usd),
            "scenario_unmet_mwh": list(self.scenario_unmet_mwh),
            "scenario_probabilities": list(self.scenario_probabilities),
            "validation_all_passed": self.validation_all_passed,
            "validation_failures": list(self.validation_failures),
        }


# --------------------------------------------------------------- policies ----
def policy_from_stochastic(name: str, result: Any) -> Policy:
    """Build a :class:`Policy` from a :class:`~gridmind.stochastic.StochasticResult`."""
    fs = result.first_stage or {}
    lead = int(fs.get("lead_periods", 0))
    return Policy(name=name, lead=lead,
                  charge=fs.get("charge", {}), discharge=fs.get("discharge", {}),
                  soc=fs.get("soc", {}))


def deterministic_policy(problem: ScheduleProblem, lead: int, *,
                         battery_mode: str = "lp", allow_unmet: bool = True) -> tuple[Policy, Any]:
    """Commit the first ``lead`` periods of the deterministic (mean) battery schedule.

    Returns ``(policy, schedule_result)``. Uses the base problem unchanged.
    """
    from gridmind.scheduling import solve_schedule
    res = solve_schedule(problem, allow_unmet=allow_unmet, battery_mode=battery_mode)
    if res.status != "optimal" or lead <= 0:
        return Policy(name="deterministic", lead=0), res
    lead_T = list(range(lead))
    return Policy(
        name="deterministic", lead=lead,
        charge={b: [res.charge[b][t] for t in lead_T] for b in res.charge},
        discharge={b: [res.discharge[b][t] for t in lead_T] for b in res.discharge},
        soc={b: [res.soc[b][t] for t in lead_T] for b in res.soc}), res


# --------------------------------------------------------------- evaluation ----
def evaluate_policy(problem: ScheduleProblem, policy: Policy, scenarios: ScenarioSet, *,
                    risk: RiskConfig | None = None, battery_mode: str = "lp",
                    allow_unmet: bool = True) -> PolicyEvaluation:
    """Evaluate ``policy`` on ``scenarios`` by fixing its first-stage commitments.

    Each evaluation scenario is solved with the policy's first ``policy.lead``
    periods pinned; later periods adapt. Returns risk statistics in USD/MWh.
    """
    risk = risk or RiskConfig(lead_periods=policy.lead)
    battery_mode = normalize_battery_mode(battery_mode)
    if policy.lead > problem.periods:
        return PolicyEvaluation(policy=policy.name, status="error", n_scenarios=len(scenarios.scenarios),
                                message="policy lead exceeds problem periods")
    mip = battery_mode == "exclusive"
    try:
        name, solver = resolve_solver(mip=mip)
    except SolverUnavailableError as exc:
        return PolicyEvaluation(policy=policy.name, status="solver_unavailable",
                                n_scenarios=len(scenarios.scenarios), message=str(exc))

    import pyomo.environ as pyo
    from pyomo.opt import SolverStatus, TerminationCondition

    costs: list[float] = []
    unmet_energies: list[float] = []
    probs: list[float] = []
    failures: list[str] = []
    T = list(range(problem.periods))
    t0 = time.perf_counter()
    for s in scenarios.scenarios:
        scen_prob = scenario_to_problem(problem, s)
        model = _build_pinned_model(problem, scen_prob, policy, risk, allow_unmet, battery_mode, pyo)
        try:
            results = solver.solve(model, load_solutions=False, tee=False)
        except Exception as exc:  # noqa: BLE001
            return PolicyEvaluation(policy=policy.name, status="error",
                                    n_scenarios=len(scenarios.scenarios), solver=name,
                                    message=f"solver exception on {s.id}: {exc}")
        term = results.solver.termination_condition
        if term != TerminationCondition.optimal or results.solver.status not in (
                SolverStatus.ok, SolverStatus.warning):
            return PolicyEvaluation(policy=policy.name, status="infeasible",
                                    n_scenarios=len(scenarios.scenarios), solver=name,
                                    message=f"scenario {s.id} infeasible under policy {policy.name} "
                                            f"(term={term})")
        model.solutions.load_from(results)
        cost = pyo.value(model.scenario_cost["eval"], exception=False)
        if cost is None:
            return PolicyEvaluation(policy=policy.name, status="error",
                                    n_scenarios=len(scenarios.scenarios), solver=name,
                                    message=f"no objective for scenario {s.id}")
        unmet_mwh = _unmet_energy(model, problem, pyo)
        costs.append(float(cost))
        unmet_energies.append(unmet_mwh)
        probs.append(float(s.probability))
        # Independently validate this scenario's realized trajectories.
        traj = _trajectories(model, T, pyo)
        report = _validate_scenario(problem, scen_prob, traj["dispatch"], traj["flows"],
                                    traj["unmet"], traj["charge"], traj["discharge"], traj["soc"],
                                    reported=float(cost), allow_unmet=allow_unmet,
                                    battery_mode=battery_mode)
        if not report.passed:
            failures.append(s.id)
    runtime = time.perf_counter() - t0

    metrics = _risk_metrics(costs, probs, risk.cvar_alpha)
    return PolicyEvaluation(
        policy=policy.name, status="optimal", n_scenarios=len(scenarios.scenarios),
        solver=name, runtime_seconds=runtime,
        expected_cost_usd=metrics["expected"],
        expected_unmet_energy_mwh=sum(p * u for p, u in zip(probs, unmet_energies)),
        probability_of_unmet=sum(p for p, u in zip(probs, unmet_energies) if u > 1e-6),
        worst_case_cost_usd=metrics["worst"], var_usd=metrics["var"], cvar_usd=metrics["cvar"],
        p95_cost_usd=metrics["p95"],
        scenario_costs_usd=tuple(costs), scenario_unmet_mwh=tuple(unmet_energies),
        scenario_probabilities=tuple(probs),
        validation_all_passed=(len(failures) == 0),
        validation_failures=tuple(failures))


def _build_pinned_model(problem: ScheduleProblem, scenario_problem: ScheduleProblem,
                        policy: Policy, risk: RiskConfig, allow_unmet: bool,
                        battery_mode: str, pyo: Any) -> Any:
    """Single-scenario model with the policy's first-stage values FIXED."""
    from gridmind.stochastic import build_stochastic_model
    from gridmind.uncertainty import UncertaintyScenario
    single = ScenarioSet(
        scenarios=(UncertaintyScenario(
            id="eval", name="eval", probability=1.0,
            demand_mw=scenario_problem.horizon.demand_mw,
            availability_mw=scenario_problem.horizon.availability_mw),),
        demand_nodes=tuple(n.id for n in problem.network.nodes),
        availability_generators=tuple(g.id for g in problem.network.generators))
    eval_risk = replace(risk, lead_periods=policy.lead, risk_weight=0.0, cvar_alpha=0.5)
    model = build_stochastic_model(scenario_problem, single, risk=eval_risk,
                                   allow_unmet=allow_unmet, battery_mode=battery_mode)
    # Fix first-stage battery variables to the policy commitments. In the
    # single-scenario model the shared first-stage vars exist iff lead > 0 and
    # are tied to the scenario vars by equality, so fixing the shared ones is
    # equivalent to fixing the scenario decisions.
    lead_T = list(range(policy.lead))
    for b, series in policy.charge.items():
        for t in lead_T:
            model.CH[b, t].fix(pyo.value(series[t]))
    for b, series in policy.discharge.items():
        for t in lead_T:
            model.DIS[b, t].fix(pyo.value(series[t]))
    for b, series in policy.soc.items():
        for t in lead_T:
            model.E[b, t].fix(pyo.value(series[t]))
    return model


def _unmet_energy(model: Any, problem: ScheduleProblem, pyo: Any) -> float:
    dt = problem.step_hours
    total = 0.0
    for n in model.N:
        for t in model.T:
            val = pyo.value(model.u["eval", n, t], exception=False)
            if val:
                total += val
    return dt * total


def _trajectories(model: Any, T: list[int], pyo: Any) -> dict[str, Any]:
    dispatch = {g: [float(pyo.value(model.p["eval", g, t], exception=False) or 0.0) for t in T]
                for g in model.G}
    flows = {l: [float(pyo.value(model.f["eval", l, t], exception=False) or 0.0) for t in T]
             for l in model.L}
    unmet = {n: [float(pyo.value(model.u["eval", n, t], exception=False) or 0.0) for t in T]
             for n in model.N}
    charge = {b: [float(pyo.value(model.ch["eval", b, t], exception=False) or 0.0) for t in T]
              for b in model.B}
    discharge = {b: [float(pyo.value(model.dis["eval", b, t], exception=False) or 0.0) for t in T]
                 for b in model.B}
    soc = {b: [float(pyo.value(model.e["eval", b, t], exception=False) or 0.0) for t in T]
           for b in model.B}
    return {"dispatch": dispatch, "flows": flows, "unmet": unmet,
            "charge": charge, "discharge": discharge, "soc": soc}


def _risk_metrics(costs: Sequence[float], probs: Sequence[float], alpha: float) -> dict[str, float]:
    """Expected cost, worst case, VaR, CVaR and p95 from scenario costs/weights.

    ``VaR`` is the ``alpha`` probability-weighted quantile of the cost (the
    smallest cost level whose cumulative weight reaches ``alpha``); it is the
    upper boundary of the worst ``1 - alpha`` tail. ``CVaR`` is the
    probability-weighted mean of that worst tail, renormalised by the tail
    probability - the standard discrete CVaR at confidence ``alpha``. ``p95`` is
    the 95th-percentile cost level.
    """
    pairs = sorted(zip(costs, probs))
    expected = float(sum(c * p for c, p in zip(costs, probs)))
    worst = float(max(costs))

    def quantile(level: float) -> float:
        cum = 0.0
        for c, p in pairs:
            cum += p
            if cum >= level:
                return float(c)
        return worst

    var_cost = quantile(alpha)
    # CVaR: accumulate the worst (1-alpha) probability mass from the top.
    remaining = 1.0 - alpha
    tail_cost = 0.0
    tail_prob = 0.0
    for c, p in reversed(pairs):
        take = min(p, remaining)
        tail_cost += take * c
        tail_prob += take
        remaining -= take
        if remaining <= 1e-12:
            break
    cvar = tail_cost / tail_prob if tail_prob > 1e-12 else worst
    return {"expected": expected, "worst": worst, "var": var_cost,
            "cvar": float(cvar), "p95": quantile(0.95)}


# --------------------------------------------------------------- comparison ----
def compare_policies(problem: ScheduleProblem, in_sample: ScenarioSet, holdout: ScenarioSet, *,
                     risk_weight: float = 0.5, cvar_alpha: float = 0.95, lead_periods: int = 1,
                     battery_mode: str = "lp", allow_unmet: bool = True) -> dict[str, Any]:
    """Fit deterministic / risk-neutral / risk-aware policies in-sample, evaluate out-of-sample.

    Returns a record with one :class:`PolicyEvaluation` per policy (as dicts), the
    stochastic fit results, the risk configuration and the solver used. Policies
    that fail to fit are recorded with their status and are not fabricated.
    """
    from gridmind.stochastic import solve_stochastic
    risk_neutral = RiskConfig(risk_weight=0.0, cvar_alpha=cvar_alpha, lead_periods=lead_periods)
    risk_aware = RiskConfig(risk_weight=risk_weight, cvar_alpha=cvar_alpha, lead_periods=lead_periods)

    fit: dict[str, Any] = {}
    policies: dict[str, Policy] = {}

    det_policy, det_res = deterministic_policy(problem, lead_periods,
                                               battery_mode=battery_mode, allow_unmet=allow_unmet)
    policies["deterministic"] = det_policy
    fit["deterministic"] = {"status": getattr(det_res, "status", "error"),
                            "objective_usd": getattr(det_res, "objective", None),
                            "solver": getattr(det_res, "solver", "")}

    rn = solve_stochastic(problem, in_sample, risk=risk_neutral,
                          allow_unmet=allow_unmet, battery_mode=battery_mode)
    fit["risk_neutral"] = rn.to_dict()
    if rn.status == "optimal":
        policies["risk_neutral"] = policy_from_stochastic("risk_neutral", rn)

    ra = solve_stochastic(problem, in_sample, risk=risk_aware,
                          allow_unmet=allow_unmet, battery_mode=battery_mode)
    fit["risk_aware"] = ra.to_dict()
    if ra.status == "optimal":
        policies["risk_aware"] = policy_from_stochastic("risk_aware", ra)

    eval_risk = RiskConfig(risk_weight=risk_weight, cvar_alpha=cvar_alpha, lead_periods=lead_periods)
    evaluations: dict[str, Any] = {}
    for name, policy in policies.items():
        ev = evaluate_policy(problem, policy, holdout, risk=eval_risk,
                             battery_mode=battery_mode, allow_unmet=allow_unmet)
        evaluations[name] = ev

    solver = ""
    for source in (fit.get("risk_neutral", {}), fit.get("risk_aware", {})):
        if isinstance(source, dict) and source.get("solver"):
            solver = source["solver"]
            break
    return {
        "risk": eval_risk.to_dict(),
        "battery_mode": battery_mode,
        "lead_periods": lead_periods,
        "solver": solver,
        "in_sample_scenarios": len(in_sample.scenarios),
        "holdout_scenarios": len(holdout.scenarios),
        "fit": fit,
        "evaluations": {k: v.to_dict() for k, v in evaluations.items()},
    }
