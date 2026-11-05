"""Multi-period (intertemporal) optimisation: model construction, solving, extraction.

Formulation (see README for the full maths). Outages are data-driven 0/1
parameters, so they need no mixed-integer variables. Variables, all in MW except
``e`` (MWh):

    p[g,t]  >= 0   generator g output in period t
    f[l,t]  free    flow on line l (positive from -> to), MW
    u[n,t]  >= 0   unmet demand at node n, MW
    ch[b,t] >= 0   battery b charging power, MW (drawn from the grid)
    dis[b,t]>= 0   battery b discharging power, MW (delivered to the grid)
    e[b,t]  >= 0   battery b state of charge AFTER period t, MWh
    z[b,t]  binary  battery b discharge-mode indicator (exclusive mode only)

Two battery modes are supported (``battery_mode``):

* ``"lp"`` (default) - the continuous relaxation above. Minimises generation cost
  + unmet-demand penalty + battery throughput cost, plus a tiny lexicographic
  tie-break epsilon*(ch+dis) that *discourages* but does not forbid simultaneous
  charge/discharge. When round-trip efficiency is below one a simultaneous pair
  can be required for feasibility and can appear even with epsilon > 0 (see
  README, "simultaneity").
* ``"exclusive"`` - genuine MIP exclusivity: adds a binary ``z[b,t]`` with
  ``ch[b,t] <= max_charge[b]*(1 - z[b,t])`` and
  ``dis[b,t] <= max_discharge[b]*z[b,t]``, so a battery can never charge and
  discharge in the same period. Requires a verified MIP-capable solver
  (``resolve_solver(mip=True)``); never falls back to an LP-only solver.

Like the single-period path, a non-optimal outcome never fabricates numbers:
``status`` != ``optimal`` implies ``objective is None`` and empty trajectories.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from gridmind.multiperiod import ScheduleProblem
from gridmind.provenance import ExecutionMetadata, capture_execution_metadata, sanitize_for_serialization
from gridmind.solver import SolverOptions, SolverUnavailableError, map_termination_status, resolve_solver, solver_name
from gridmind.validation import ValidationReport, recompute_schedule_costs, validate_schedule

CLEAN_EPS = 1e-9

#: accepted ``battery_mode`` values. "lp" is the historical continuous relaxation;
#: "exclusive" adds binary charge/discharge exclusivity and needs a MIP solver.
BATTERY_MODES: tuple[str, ...] = ("lp", "exclusive")


def normalize_battery_mode(mode: str) -> str:
    """Validate and return a ``battery_mode`` value (raises ValueError otherwise)."""
    if mode not in BATTERY_MODES:
        raise ValueError(f"battery_mode must be one of {BATTERY_MODES}, got {mode!r}")
    return mode


@dataclass
class ScheduleResult:
    status: str                  # optimal | infeasible | unbounded | solver_unavailable | error
    solver: str
    termination_condition: str
    message: str
    runtime_seconds: float
    periods: int = 0
    step_hours: float = 0.0
    objective: float | None = None           # solver objective incl. tie-break (USD)
    operating_cost_usd: float | None = None  # generation + unmet penalty + throughput (USD)
    generation_cost_usd: float | None = None
    unmet_penalty_cost_usd: float | None = None
    battery_throughput_cost_usd: float | None = None
    tiebreak_cost_usd: float | None = None
    total_demand_mwh: float = 0.0
    unmet_energy_mwh: float | None = None
    served_demand_mwh: float | None = None
    demand_served_pct: float | None = None
    max_unmet_mw: float | None = None
    max_unmet_periods: tuple[int, ...] = ()
    dispatch: dict[str, list[float]] = field(default_factory=dict)   # generator -> MW per period
    flows: dict[str, list[float]] = field(default_factory=dict)      # line -> MW per period
    unmet: dict[str, list[float]] = field(default_factory=dict)      # node -> MW per period
    served: dict[str, list[float]] = field(default_factory=dict)     # node -> MW per period
    charge: dict[str, list[float]] = field(default_factory=dict)     # battery -> MW per period
    discharge: dict[str, list[float]] = field(default_factory=dict)  # battery -> MW per period
    soc: dict[str, list[float]] = field(default_factory=dict)        # battery -> MWh after each period
    cost_by_period_usd: list[float] = field(default_factory=list)    # operating cost per period
    validation: ValidationReport | None = None
    strict: bool = False
    battery_mode: str = "lp"     # "lp" (continuous) or "exclusive" (MIP)
    metadata: ExecutionMetadata | None = None

    @property
    def ok(self) -> bool:
        return self.status == "optimal" and (self.validation is None or self.validation.passed)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in vars(self).items() if k not in ("validation", "metadata")}
        d["max_unmet_periods"] = list(self.max_unmet_periods)
        d["validation"] = self.validation.to_dict() if self.validation else None
        d["metadata"] = self.metadata.to_dict() if self.metadata else None
        d["ok"] = self.ok
        return sanitize_for_serialization(d)


# ----------------------------------------------------------------- model ----
def build_schedule_model(problem: ScheduleProblem, allow_unmet: bool = True,
                         battery_mode: str = "lp",
                         fixed_battery_mw: Mapping[str, float] | None = None) -> Any:
    """Construct the multi-period model for ``problem`` (already validated).

    ``battery_mode="lp"`` builds the historical continuous LP; ``"exclusive"``
    additionally adds binary charge/discharge exclusivity (a MIP model).

    ``fixed_battery_mw`` optionally injects an *already committed* net battery
    power at each battery's node as a **constant** in the nodal balance (MW,
    positive = discharge into the node, negative = charging load). The battery's
    own decision variables are still present, so callers normally freeze them
    (``max_charge_mw = max_discharge_mw = 0``) when using this. It is used by the
    rolling-horizon controller to execute a first-stage storage commitment against
    realized conditions; because the injection is power (MW), any surplus can be
    exported over the network instead of being clamped away. Default ``None``
    leaves the model bit-for-bit unchanged."""
    import pyomo.environ as pyo

    battery_mode = normalize_battery_mode(battery_mode)
    problem.validate()
    net, hor = problem.network, problem.horizon
    T = list(range(problem.periods))
    dt = problem.step_hours
    nodes = [n.id for n in net.nodes]
    gens = [g.id for g in net.generators]
    lines = [l.id for l in net.lines]
    batts = [b.id for b in problem.batteries]
    gen_of = {g.id: g for g in net.generators}

    # ramp sets built in Python so the constraint blocks are never empty (some
    # Pyomo/solver combinations dislike empty indexed constraints)
    ramp_up = {g.id: g.ramp_up_mw for g in net.generators if g.ramp_up_mw is not None}
    ramp_down = {g.id: g.ramp_down_mw for g in net.generators if g.ramp_down_mw is not None}
    ramp_up_t = [(g, t) for g in ramp_up for t in T[1:]]
    ramp_down_t = [(g, t) for g in ramp_down for t in T[1:]]
    ramp_init_up = [(g, g0) for g in ramp_up for g0 in [problem.initial_dispatch_mw.get(g)] if g0 is not None]
    ramp_init_down = [(g, g0) for g in ramp_down for g0 in [problem.initial_dispatch_mw.get(g)] if g0 is not None]
    batt_of = {b.id: b for b in problem.batteries}
    with_final = [b.id for b in problem.batteries if b.final_soc_mwh is not None]

    m = pyo.ConcreteModel(name="GridMindMP")
    m.T = pyo.Set(initialize=T, ordered=True)
    m.N = pyo.Set(initialize=nodes, ordered=True)
    m.G = pyo.Set(initialize=gens, ordered=True)
    m.L = pyo.Set(initialize=lines, ordered=True)
    m.B = pyo.Set(initialize=batts, ordered=True)

    m.demand = pyo.Param(m.N, m.T, initialize=lambda mm, n, t: hor.demand(n, t))
    m.gen_limit = pyo.Param(m.G, m.T, initialize=lambda mm, g, t: hor.generation_limit(gen_of[g], t))
    m.line_cap = pyo.Param(m.L, m.T, initialize=lambda mm, l, t: hor.line_limit(net.line(l), t))

    m.p = pyo.Var(m.G, m.T, domain=pyo.NonNegativeReals)
    m.f = pyo.Var(m.L, m.T, domain=pyo.Reals)
    m.u = pyo.Var(m.N, m.T, domain=pyo.NonNegativeReals)
    m.ch = pyo.Var(m.B, m.T, domain=pyo.NonNegativeReals)
    m.dis = pyo.Var(m.B, m.T, domain=pyo.NonNegativeReals)
    m.e = pyo.Var(m.B, m.T, domain=pyo.NonNegativeReals)

    m.gen_ub = pyo.Constraint(m.G, m.T, rule=lambda mm, g, t: mm.p[g, t] <= mm.gen_limit[g, t])
    m.line_ub = pyo.Constraint(m.L, m.T, rule=lambda mm, l, t: mm.f[l, t] <= mm.line_cap[l, t])
    m.line_lb = pyo.Constraint(m.L, m.T, rule=lambda mm, l, t: mm.f[l, t] >= -mm.line_cap[l, t])

    # ramp limits: between consecutive periods, and (when an initial output is
    # declared) from that output into period 0
    m.ramp_up = pyo.Constraint(ramp_up_t,
                               rule=lambda mm, g, t: mm.p[g, t] - mm.p[g, t - 1] <= ramp_up[g])
    m.ramp_down = pyo.Constraint(ramp_down_t,
                                 rule=lambda mm, g, t: mm.p[g, t - 1] - mm.p[g, t] <= ramp_down[g])
    m.ramp_init_up = pyo.Constraint(ramp_init_up,
                                    rule=lambda mm, g, g0: mm.p[g, 0] - g0 <= ramp_up[g])
    m.ramp_init_down = pyo.Constraint(ramp_init_down,
                                      rule=lambda mm, g, g0: g0 - mm.p[g, 0] <= ramp_down[g])

    m.bat_cap = pyo.Param(m.B, initialize={b.id: b.capacity_mwh for b in problem.batteries})
    m.bat_min = pyo.Param(m.B, initialize={b.id: b.min_soc_mwh for b in problem.batteries})
    m.bat_pc = pyo.Param(m.B, initialize={b.id: b.max_charge_mw for b in problem.batteries})
    m.bat_pd = pyo.Param(m.B, initialize={b.id: b.max_discharge_mw for b in problem.batteries})
    m.bat_ec = pyo.Param(m.B, initialize={b.id: b.eta_charge for b in problem.batteries})
    m.bat_ed = pyo.Param(m.B, initialize={b.id: b.eta_discharge for b in problem.batteries})
    m.bat_soc0 = pyo.Param(m.B, initialize={b.id: b.initial_soc_mwh for b in problem.batteries})

    m.charge_ub = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.ch[b, t] <= mm.bat_pc[b])
    m.discharge_ub = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.dis[b, t] <= mm.bat_pd[b])
    m.soc_ub = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.e[b, t] <= mm.bat_cap[b])
    m.soc_lb = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.e[b, t] >= mm.bat_min[b])

    if battery_mode == "exclusive" and batts:
        # z[b,t] = 1 forces discharge-only, 0 forces charge-only. The big-M
        # coefficients are the battery's own power limits (tight bounds), so the
        # constraints are exact and no charge/discharge can coexist in a period.
        m.z = pyo.Var(m.B, m.T, domain=pyo.Binary)
        m.exclusive_charge = pyo.Constraint(
            m.B, m.T, rule=lambda mm, b, t: mm.ch[b, t] <= mm.bat_pc[b] * (1 - mm.z[b, t]))
        m.exclusive_discharge = pyo.Constraint(
            m.B, m.T, rule=lambda mm, b, t: mm.dis[b, t] <= mm.bat_pd[b] * mm.z[b, t])

    def soc_step(mm: Any, b: str, t: int) -> Any:
        prev = mm.bat_soc0[b] if t == 0 else mm.e[b, t - 1]
        return mm.e[b, t] == prev + mm.bat_ec[b] * mm.ch[b, t] * dt - mm.dis[b, t] * dt / mm.bat_ed[b]

    m.soc_dyn = pyo.Constraint(m.B, m.T, rule=soc_step)
    m.soc_final = pyo.Constraint(with_final,
                                 rule=lambda mm, b: mm.e[b, problem.periods - 1] == batt_of[b].final_soc_mwh)

    gens_at = {n: [g.id for g in net.generators if g.node == n] for n in nodes}
    into = {n: [l.id for l in net.lines if l.to_node == n] for n in nodes}
    out_of = {n: [l.id for l in net.lines if l.from_node == n] for n in nodes}
    batts_at = {n: [b.id for b in problem.batteries if b.node == n] for n in nodes}

    fixed_mw = {str(k): float(v) for k, v in (fixed_battery_mw or {}).items()}
    unknown_fixed = set(fixed_mw) - set(batts)
    if unknown_fixed:
        raise ValueError(f"fixed_battery_mw references unknown battery ids: {sorted(unknown_fixed)}")
    fixed_at = {n: sum(fixed_mw.get(b, 0.0) for b in batts_at[n]) for n in nodes}

    def balance(mm: Any, n: str, t: int) -> Any:
        return (sum(mm.p[g, t] for g in gens_at[n])
                + sum(mm.f[l, t] for l in into[n]) - sum(mm.f[l, t] for l in out_of[n])
                + sum(mm.dis[b, t] for b in batts_at[n]) - sum(mm.ch[b, t] for b in batts_at[n])
                + fixed_at[n]
                + mm.u[n, t]) == mm.demand[n, t]

    m.balance = pyo.Constraint(m.N, m.T, rule=balance)
    if allow_unmet:
        m.unmet_ub = pyo.Constraint(m.N, m.T, rule=lambda mm, n, t: mm.u[n, t] <= mm.demand[n, t])
    else:
        m.unmet_ub = pyo.Constraint(m.N, m.T, rule=lambda mm, n, t: mm.u[n, t] == 0.0)

    penalty = net.unmet_penalty_per_mwh
    thr_cost = {b.id: b.throughput_cost_per_mwh for b in problem.batteries}
    eps = problem.tiebreak_epsilon
    m.total_cost = pyo.Objective(
        expr=dt * (sum(net.generator(g).cost_per_mwh * m.p[g, t] for g in m.G for t in m.T)
                   + penalty * sum(m.u[n, t] for n in m.N for t in m.T)
                   + sum(thr_cost[b] * m.dis[b, t] for b in m.B for t in m.T))
             + eps * dt * sum(m.ch[b, t] + m.dis[b, t] for b in m.B for t in m.T),
        sense=pyo.minimize)
    if battery_mode != "exclusive":
        # LP duals are meaningful for the continuous relaxation and are imported
        # as an optional diagnostic. A MIP (exclusive mode) has no valid duals,
        # and the ``appsi`` LegacySolver wrapper would raise while trying to
        # import them, so the suffix is declared only for the LP model.
        m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    return m


# ----------------------------------------------------------------- solve ----
def _clean(x: float) -> float:
    return 0.0 if abs(x) < CLEAN_EPS else float(x)


def _failure(status: str, solver: str, term: str, msg: str, runtime: float,
             problem: ScheduleProblem, strict: bool, battery_mode: str = "lp",
             metadata: ExecutionMetadata | None = None) -> ScheduleResult:
    return ScheduleResult(status=status, solver=solver, termination_condition=term, message=msg,
                          runtime_seconds=runtime, periods=problem.periods,
                          step_hours=problem.step_hours, total_demand_mwh=problem.total_demand_mwh,
                          strict=strict, battery_mode=battery_mode, metadata=metadata)


def solve_schedule(problem: ScheduleProblem, *, allow_unmet: bool = True,
                   validate: bool = True, battery_mode: str = "lp",
                   fixed_battery_mw: Mapping[str, float] | None = None,
                   options: SolverOptions | None = None) -> ScheduleResult:
    """Build, solve and independently validate a multi-period problem.

    ``battery_mode`` is ``"lp"`` (default, continuous relaxation) or
    ``"exclusive"`` (adds binary charge/discharge exclusivity; a verified
    MIP-capable solver is resolved, with no silent fallback).

    ``fixed_battery_mw`` (optional) adds a committed net battery power (MW) per
    battery as a constant nodal injection; see :func:`build_schedule_model`. The
    independent validator only knows the battery decision variables, so callers
    that use a fixed injection must pass ``validate=False`` and validate the
    *executed* trajectory separately (the controller does exactly this).

    ``options`` (optional) applies structured execution options (time limit,
    mip gap, threads) to the resolved solver instance."""
    strict = not allow_unmet
    try:
        battery_mode = normalize_battery_mode(battery_mode)
    except ValueError as exc:
        return _failure("error", solver_name(), "invalid_battery_mode", str(exc), 0.0, problem, strict, "lp")

    mip = battery_mode == "exclusive"
    try:
        name, solver = resolve_solver(mip=mip, options=options) if options is not None else resolve_solver(mip=mip)
    except SolverUnavailableError as exc:
        return _failure("solver_unavailable", solver_name(), "not_run", str(exc), 0.0, problem, strict,
                        battery_mode)

    meta = capture_execution_metadata(solver, name, problem, options=options)

    import pyomo.environ as pyo
    from pyomo.opt import SolverStatus, TerminationCondition

    try:
        model = build_schedule_model(problem, allow_unmet=allow_unmet, battery_mode=battery_mode,
                                     fixed_battery_mw=fixed_battery_mw)
    except ValueError as exc:
        return _failure("error", name, "model_build_failed", f"invalid schedule problem: {exc}",
                        0.0, problem, strict, battery_mode, metadata=meta)

    t0 = time.perf_counter()
    try:
        results = solver.solve(model, load_solutions=False, tee=False)
    except Exception as exc:  # solver crash, bad executable, parse failure ...
        return _failure("error", name, "solver_exception", f"{type(exc).__name__}: {exc}",
                        time.perf_counter() - t0, problem, strict, battery_mode, metadata=meta)
    runtime = time.perf_counter() - t0

    term = results.solver.termination_condition
    term_s = str(term)
    if term != TerminationCondition.optimal or results.solver.status not in (SolverStatus.ok, SolverStatus.warning):
        status = map_termination_status(term, default="error")
        msg = {"infeasible": "model is infeasible: no schedule satisfies all constraints "
                             "(check ramps, outages and terminal SoC requirements)",
               "unbounded": "model is unbounded (check data)",
               "time_limit": f"solver reached time limit (termination={term_s})",
               "interrupted": f"solver execution interrupted (termination={term_s})",
               "solver_error": f"solver error encountered (termination={term_s})"}.get(
            status, f"solver did not reach optimality (status={results.solver.status}, termination={term_s})")
        return _failure(status, name, term_s, msg, runtime, problem, strict, battery_mode, metadata=meta)
    T = list(range(problem.periods))
    try:
        model.solutions.load_from(results)
        dispatch = {g: [_clean(pyo.value(model.p[g, t], exception=False)) for t in T] for g in model.G}
        flows = {l: [_clean(pyo.value(model.f[l, t], exception=False)) for t in T] for l in model.L}
        unmet = {n: [_clean(pyo.value(model.u[n, t], exception=False)) for t in T] for n in model.N}
        charge = {b: [_clean(pyo.value(model.ch[b, t], exception=False)) for t in T] for b in model.B}
        discharge = {b: [_clean(pyo.value(model.dis[b, t], exception=False)) for t in T] for b in model.B}
        soc = {b: [_clean(pyo.value(model.e[b, t], exception=False)) for t in T] for b in model.B}
        reported = pyo.value(model.total_cost, exception=False)
        flat = (list(dispatch.values()) + list(flows.values()) + list(unmet.values())
                + list(charge.values()) + list(discharge.values()) + list(soc.values()))
        if reported is None or any(v is None for series in flat for v in series):
            raise ValueError("solver reported optimal but returned no values")
    except Exception as exc:
        return _failure("error", name, term_s, f"could not read solution: {exc}", runtime, problem, strict,
                        battery_mode, metadata=meta)

    dispatch = {g: [_clean(v) for v in series] for g, series in dispatch.items()}
    flows = {l: [_clean(v) for v in series] for l, series in flows.items()}
    unmet = {n: [_clean(v) for v in series] for n, series in unmet.items()}
    charge = {b: [_clean(v) for v in series] for b, series in charge.items()}
    discharge = {b: [_clean(v) for v in series] for b, series in discharge.items()}
    soc = {b: [_clean(v) for v in series] for b, series in soc.items()}

    net = problem.network
    dt = problem.step_hours
    penalty = net.unmet_penalty_per_mwh
    costs = recompute_schedule_costs(problem, dispatch, unmet, charge, discharge)
    unmet_energy = dt * sum(sum(series) for series in unmet.values())
    total_demand = problem.total_demand_mwh
    max_unmet = max((max(row, default=0.0) for row in unmet.values()), default=0.0)
    max_unmet_periods = tuple(t for t in T if any(unmet[n][t] > CLEAN_EPS for n in unmet))
    per_period_cost = [
        dt * (sum(net.generator(g).cost_per_mwh * dispatch[g][t] for g in dispatch)
              + penalty * sum(unmet[n][t] for n in unmet)
              + sum(problem.battery(b).throughput_cost_per_mwh * discharge[b][t] for b in discharge))
        for t in T]

    report = validate_schedule(problem, dispatch, flows, unmet, charge, discharge, soc,
                               reported_objective=float(reported), allow_unmet=allow_unmet,
                               battery_mode=battery_mode) if validate else None

    served = {n: [_clean(problem.horizon.demand(n, t) - unmet[n][t]) for t in T] for n in unmet}
    return ScheduleResult(
        status="optimal", solver=name, termination_condition=term_s,
        message="optimal solution found" if (report is None or report.passed)
        else "optimal per solver but FAILED independent validation",
        runtime_seconds=runtime, periods=problem.periods, step_hours=dt,
        objective=float(reported),
        operating_cost_usd=costs["operating_cost_usd"],
        generation_cost_usd=costs["generation_cost_usd"],
        unmet_penalty_cost_usd=costs["unmet_penalty_cost_usd"],
        battery_throughput_cost_usd=costs["battery_throughput_cost_usd"],
        tiebreak_cost_usd=costs["tiebreak_cost_usd"],
        total_demand_mwh=total_demand, unmet_energy_mwh=unmet_energy,
        served_demand_mwh=total_demand - unmet_energy,
        demand_served_pct=100.0 * (total_demand - unmet_energy) / total_demand if total_demand > 0 else 100.0,
        max_unmet_mw=max_unmet, max_unmet_periods=max_unmet_periods,
        dispatch=dispatch, flows=flows, unmet=unmet, served=served,
        charge=charge, discharge=discharge, soc=soc,
        cost_by_period_usd=[_clean(c) for c in per_period_cost],
        validation=report, strict=strict, battery_mode=battery_mode,
        metadata=meta)
