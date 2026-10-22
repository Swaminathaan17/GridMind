"""Pyomo LP model + GLPK integration.

Separation of concerns:
  build_model()   -> pure model construction (no solving)
  make_solver()   -> solver discovery/configuration (env vars, no hardcoded path)
  solve_dispatch()-> solver execution, termination checks, extraction, validation

Formulation (see README for the maths). Variables: p[g] >= 0 generation (MW),
f[l] free flow (MW, positive from_node->to_node), u[n] >= 0 unmet demand (MW).
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from gridmind.network import Network
from gridmind.solver import (ENV_SOLVER, ENV_SOLVER_PATH, SolverUnavailableError, make_solver,
                             resolve_solver, solver_available, solver_name)
from gridmind.validation import ValidationReport, recompute_objective, validate_solution

CLEAN_EPS = 1e-9


@dataclass
class SolveResult:
    status: str                  # optimal | infeasible | unbounded | infeasible_or_unbounded | solver_unavailable | error
    solver: str
    termination_condition: str
    message: str
    runtime_seconds: float       # solver call only (excludes model build)
    objective: float | None = None
    generation_cost: float | None = None
    unmet_penalty_cost: float | None = None
    total_demand_mw: float = 0.0
    unmet_demand_mw: float | None = None
    served_demand_mw: float | None = None
    dispatch: dict[str, float] = field(default_factory=dict)
    flows: dict[str, float] = field(default_factory=dict)
    unmet: dict[str, float] = field(default_factory=dict)      # per node
    served: dict[str, float] = field(default_factory=dict)     # per node
    duals: dict[str, float] = field(default_factory=dict)      # raw solver duals, if provided
    validation: ValidationReport | None = None
    strict: bool = False         # True: unmet demand was forbidden

    @property
    def ok(self) -> bool:
        return self.status == "optimal" and (self.validation is None or self.validation.passed)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in vars(self).items() if k != "validation"}
        d["validation"] = self.validation.to_dict() if self.validation else None
        d["ok"] = self.ok
        return d


# ---------------------------------------------------------------- solver ----
# ----------------------------------------------------------------- model ----
def build_model(network: Network, allow_unmet: bool = True) -> Any:
    """Construct the LP. Outages are constraints (not deleted elements):
        gen_limit:  p[g] <= capacity[g] * online[g]
        avail_cap:  p[g] <= availability[g] * online[g]   (all generators)
        line_ub/lb: -cap[l]*in_service[l] <= f[l] <= cap[l]*in_service[l]
    allow_unmet=False forces u[n] = 0 (strict mode), which can make the LP infeasible."""
    import pyomo.environ as pyo

    network.validate()
    m = pyo.ConcreteModel(name="GridMind")
    nodes = {n.id: n for n in network.nodes}
    gens = {g.id: g for g in network.generators}
    lines = {l.id: l for l in network.lines}

    m.N = pyo.Set(initialize=list(nodes), ordered=True)
    m.G = pyo.Set(initialize=list(gens), ordered=True)
    m.L = pyo.Set(initialize=list(lines), ordered=True)

    m.cost = pyo.Param(m.G, initialize={g: o.cost_per_mwh for g, o in gens.items()})
    m.gen_cap = pyo.Param(m.G, initialize={g: o.capacity_mw for g, o in gens.items()})
    m.gen_online = pyo.Param(m.G, initialize={g: 1.0 if o.online else 0.0 for g, o in gens.items()})
    m.avail = pyo.Param(m.G, initialize={g: o.available_mw for g, o in gens.items()})
    m.line_cap = pyo.Param(m.L, initialize={l: o.capacity_mw for l, o in lines.items()})
    m.line_on = pyo.Param(m.L, initialize={l: 1.0 if o.in_service else 0.0 for l, o in lines.items()})
    m.demand = pyo.Param(m.N, initialize={n: o.demand_mw for n, o in nodes.items()})

    m.p = pyo.Var(m.G, domain=pyo.NonNegativeReals)
    m.f = pyo.Var(m.L, domain=pyo.Reals)
    m.u = pyo.Var(m.N, domain=pyo.NonNegativeReals)

    # gen_limit enforces capacity and outage; avail_cap enforces the (possibly
    # time-invariant) availability forecast. Taking the min keeps a single bound
    # so p[g] <= min(capacity, availability), matching Generator.effective_limit_mw.
    m.gen_limit = pyo.Constraint(m.G, rule=lambda mm, g: mm.p[g] <= mm.gen_cap[g] * mm.gen_online[g])
    m.avail_cap = pyo.Constraint(m.G, rule=lambda mm, g: mm.p[g] <= mm.avail[g] * mm.gen_online[g])
    m.line_ub = pyo.Constraint(m.L, rule=lambda mm, l: mm.f[l] <= mm.line_cap[l] * mm.line_on[l])
    m.line_lb = pyo.Constraint(m.L, rule=lambda mm, l: mm.f[l] >= -mm.line_cap[l] * mm.line_on[l])
    if allow_unmet:
        m.unmet_ub = pyo.Constraint(m.N, rule=lambda mm, n: mm.u[n] <= mm.demand[n])
    else:
        m.unmet_ub = pyo.Constraint(m.N, rule=lambda mm, n: mm.u[n] == 0.0)

    gens_at = {n: [g for g, o in gens.items() if o.node == n] for n in nodes}
    into = {n: [l for l, o in lines.items() if o.to_node == n] for n in nodes}
    out_of = {n: [l for l, o in lines.items() if o.from_node == n] for n in nodes}

    def balance(mm: Any, n: str) -> Any:
        return (sum(mm.p[g] for g in gens_at[n]) + sum(mm.f[l] for l in into[n])
                - sum(mm.f[l] for l in out_of[n]) + mm.u[n]) == mm.demand[n]

    m.balance = pyo.Constraint(m.N, rule=balance)

    hours, penalty = network.interval_hours, network.unmet_penalty_per_mwh
    m.total_cost = pyo.Objective(
        expr=hours * (sum(m.cost[g] * m.p[g] for g in m.G) + penalty * sum(m.u[n] for n in m.N)),
        sense=pyo.minimize)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    return m


# ----------------------------------------------------------------- solve ----
def _failure(status: str, solver: str, term: str, msg: str, runtime: float, network: Network, strict: bool) -> SolveResult:
    return SolveResult(status=status, solver=solver, termination_condition=term, message=msg,
                       runtime_seconds=runtime, total_demand_mw=network.total_demand_mw, strict=strict)


def _clean(x: float) -> float:
    return 0.0 if abs(x) < CLEAN_EPS else float(x)


def solve_dispatch(network: Network, *, allow_unmet: bool = True, validate: bool = True) -> SolveResult:
    """Build, solve and validate. Never fabricates values: on any non-optimal
    outcome the result has status != 'optimal', objective None and empty dicts."""
    strict = not allow_unmet
    try:
        name, solver = resolve_solver()
    except SolverUnavailableError as exc:
        return _failure("solver_unavailable", solver_name(), "not_run", str(exc), 0.0, network, strict)

    import pyomo.environ as pyo
    from pyomo.opt import SolverStatus, TerminationCondition

    try:
        model = build_model(network, allow_unmet=allow_unmet)
    except ValueError as exc:
        return _failure("error", name, "model_build_failed", f"invalid network: {exc}", 0.0, network, strict)

    t0 = time.perf_counter()
    try:
        results = solver.solve(model, load_solutions=False, tee=False)
    except Exception as exc:  # solver crash, bad executable, parse failure ...
        return _failure("error", name, "solver_exception", f"{type(exc).__name__}: {exc}",
                        time.perf_counter() - t0, network, strict)
    runtime = time.perf_counter() - t0

    term = results.solver.termination_condition
    term_s = str(term)
    if term != TerminationCondition.optimal or results.solver.status not in (SolverStatus.ok, SolverStatus.warning):
        status = {TerminationCondition.infeasible: "infeasible",
                  TerminationCondition.unbounded: "unbounded",
                  TerminationCondition.infeasibleOrUnbounded: "infeasible_or_unbounded"}.get(term, "error")
        msg = {"infeasible": "model is infeasible: no schedule satisfies all constraints",
               "unbounded": "model is unbounded (check data)"}.get(status, f"solver did not reach optimality (status={results.solver.status}, termination={term_s})")
        return _failure(status, name, term_s, msg, runtime, network, strict)

    try:
        model.solutions.load_from(results)
        dispatch = {g: pyo.value(model.p[g], exception=False) for g in model.G}
        flows = {l: pyo.value(model.f[l], exception=False) for l in model.L}
        unmet = {n: pyo.value(model.u[n], exception=False) for n in model.N}
        reported = pyo.value(model.total_cost, exception=False)
        if reported is None or any(v is None for d in (dispatch, flows, unmet) for v in d.values()):
            raise ValueError("solver reported optimal but returned no values")
    except Exception as exc:
        return _failure("error", name, term_s, f"could not read solution: {exc}", runtime, network, strict)

    dispatch = {k: _clean(v) for k, v in dispatch.items()}
    flows = {k: _clean(v) for k, v in flows.items()}
    unmet = {k: _clean(v) for k, v in unmet.items()}

    duals: dict[str, float] = {}
    try:
        for con in model.component_data_objects(pyo.Constraint, active=True):
            if con in model.dual:
                duals[con.name] = float(model.dual[con])
    except Exception:
        duals = {}  # duals are optional extras; never fail the solve on them

    gen_cost, penalty = recompute_objective(network, dispatch, unmet)
    total_unmet = sum(unmet.values())
    report = validate_solution(network, dispatch, flows, unmet, reported_objective=float(reported),
                               allow_unmet=allow_unmet) if validate else None
    return SolveResult(
        status="optimal", solver=name, termination_condition=term_s,
        message="optimal solution found" if (report is None or report.passed) else "optimal per solver but FAILED independent validation",
        runtime_seconds=runtime, objective=float(reported), generation_cost=gen_cost, unmet_penalty_cost=penalty,
        total_demand_mw=network.total_demand_mw, unmet_demand_mw=total_unmet,
        served_demand_mw=network.total_demand_mw - total_unmet,
        dispatch=dispatch, flows=flows, unmet=unmet,
        served={n.id: n.demand_mw - unmet[n.id] for n in network.nodes},
        duals=duals, validation=report, strict=strict)
