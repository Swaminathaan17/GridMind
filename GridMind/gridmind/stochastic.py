"""Risk-aware, scenario-based multi-period scheduling (two-stage stochastic program).

This module adds an *optional* stochastic layer on top of the existing
deterministic multi-period scheduler in :mod:`gridmind.scheduling`. The default
deterministic API is untouched; nothing here changes ``solve_schedule``.

Formulation
-----------
We are given a finite discrete distribution over ``S`` scenarios
``{(pi_s, xi_s)}`` where ``xi_s`` is the pair of demand and renewable-availability
trajectories of scenario ``s`` (see :mod:`gridmind.uncertainty`). Decisions are
split into two stages:

* **First stage (here-and-now, shared).** For the first ``lead_periods`` periods
  every scenario must commit to the *same* battery decisions: charge ``ch``,
  discharge ``dis`` and state of charge ``e`` (and the exclusivity binary ``z``
  when relevant). Storage is the quantity that must be committed before the
  uncertain demand/availability is revealed. Generator dispatch, line flows and
  unserved-energy are *not* first-stage: generators can be redispatched within a
  period, and flows/unserved energy are physical/operational outcomes that adapt
  to the realized conditions, so they remain scenario (recourse) variables in
  every period.
* **Recourse (scenario, per realization).** All remaining quantities - generator
  output, line flows, unmet demand, and the battery decisions of later periods -
  are scenario-specific: the schedule adapts to the realized ``xi_s``.

Non-anticipativity is enforced exactly by equality coupling of every first-stage
decision across all scenarios (see ``_nonanticipativity``). With
``lead_periods = 0`` the model degenerates to *wait-and-see* (each scenario
optimised independently); with ``lead_periods = periods`` it is a single
deterministic schedule over the mean. The default is ``lead_periods = 1``: the
first hour is committed before uncertainty resolves and later hours adapt. This
is a genuine two-stage (not clairvoyant) model; intermediate branching times are
*not* modelled (that would need a scenario tree).

Objective (all components in USD)
---------------------------------
For each scenario ``s`` the realized operating cost is::

    cost_s = dt * [ sum_g cost_g * p[s,g,t]
                  + penalty * sum_n u[s,n,t]
                  + sum_b throughput_b * dis[s,b,t] ]
             + tiebreak_eps * dt * sum_b (ch[s,b,t] + dis[s,b,t])

(the same definition as the deterministic model; see ``validation``).

The risk-aware objective maximises the weighted combination of the expected cost
and its Conditional Value-at-Risk (CVaR) at confidence level ``alpha``::

    minimise  (1 - lambda) * E[cost]  +  lambda * CVaR_alpha(cost)

with ``lambda`` in [0, 1] the risk-aversion weight. ``lambda = 0`` is the
risk-neutral expected-cost baseline; ``lambda = 1`` is pure CVaR minimisation.
CVaR uses the Rockafellar-Uryasev representation::

    CVaR_alpha(cost) = min_eta [ eta + 1/(1-alpha) * sum_s pi_s * v_s ]
    subject to   v_s >= cost_s - eta,   v_s >= 0

where ``eta`` is a free scalar (the Value-at-Risk level). All objective terms are
USD, so the weighted sum stays in USD.

Battery physics, generator bounds, network balance, line limits, ramp limits and
terminal-SoC requirements are copied verbatim from
:func:`gridmind.scheduling.build_schedule_model` via the shared
``_add_common_blocks`` helper. When ``battery_mode = "exclusive"`` the genuine
binary charge/discharge exclusivity
``ch <= max_charge*(1-z)``, ``dis <= max_discharge*z`` is added per scenario and
period; the resolved solver must be MIP-capable (no silent LP fallback).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from gridmind.multiperiod import ScheduleProblem
from gridmind.scheduling import normalize_battery_mode
from gridmind.solver import SolverUnavailableError, resolve_solver, solver_name
from gridmind.uncertainty import ScenarioSet, UncertaintyScenario, scenario_to_problem

CLEAN_EPS = 1e-9


def _clean(x: float | None) -> float:
    if x is None:
        return 0.0
    return 0.0 if abs(x) < CLEAN_EPS else float(x)


@dataclass(frozen=True)
class RiskConfig:
    """Explicit, validated risk configuration.

    ``risk_weight`` (lambda) in [0, 1]: 0 = risk-neutral expected cost, 1 = pure
    CVaR. ``cvar_alpha`` is the CVaR confidence level in (0, 1) (e.g. 0.95 = the
    mean of the worst 5% of scenario costs). ``lead_periods`` is the number of
    first-stage periods whose decisions must be identical across scenarios.
    """
    risk_weight: float = 0.5
    cvar_alpha: float = 0.95
    lead_periods: int = 1

    def validate(self, periods: int | None = None) -> None:
        if not (0.0 <= self.risk_weight <= 1.0):
            raise ValueError(f"risk_weight must lie in [0, 1], got {self.risk_weight}")
        if not (0.0 < self.cvar_alpha < 1.0):
            raise ValueError(f"cvar_alpha must lie in (0, 1), got {self.cvar_alpha}")
        if self.lead_periods < 0:
            raise ValueError(f"lead_periods must be >= 0, got {self.lead_periods}")
        if periods is not None and self.lead_periods > periods:
            raise ValueError(f"lead_periods ({self.lead_periods}) exceeds periods ({periods})")

    @property
    def is_risk_neutral(self) -> bool:
        return self.risk_weight == 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"risk_weight": self.risk_weight, "cvar_alpha": self.cvar_alpha,
                "lead_periods": self.lead_periods}


@dataclass
class StochasticResult:
    """Outcome of a two-stage risk-aware solve.

    ``status`` mirrors :class:`~gridmind.scheduling.ScheduleResult` conventions:
    ``optimal | infeasible | unbounded | solver_unavailable | error``. Scenario
    trajectories are provided only when ``status == "optimal"``; a failed or
    infeasible solve never fabricates numbers.
    """
    status: str
    solver: str
    termination_condition: str
    message: str
    runtime_seconds: float
    battery_mode: str = "lp"
    risk: RiskConfig | None = None
    expected_cost_usd: float | None = None
    cvar_usd: float | None = None
    objective_usd: float | None = None          # (1-lambda)*E + lambda*CVaR
    risk_neutral_cost_usd: float | None = None  # E[cost] (always reported)
    var_usd: float | None = None                # eta at optimum
    scenario_costs_usd: tuple[float, ...] = ()
    scenario_probabilities: tuple[float, ...] = ()
    first_stage: Mapping[str, Any] = field(default_factory=dict)
    scenario_results: tuple[Mapping[str, Any], ...] = ()
    scenario_validation_passed: tuple[bool | None, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "optimal" and all(
            v is not False for v in self.scenario_validation_passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "solver": self.solver,
            "termination_condition": self.termination_condition,
            "message": self.message,
            "runtime_seconds": self.runtime_seconds,
            "battery_mode": self.battery_mode,
            "risk": None if self.risk is None else self.risk.to_dict(),
            "expected_cost_usd": self.expected_cost_usd,
            "cvar_usd": self.cvar_usd,
            "objective_usd": self.objective_usd,
            "risk_neutral_cost_usd": self.risk_neutral_cost_usd,
            "var_usd": self.var_usd,
            "scenario_costs_usd": list(self.scenario_costs_usd),
            "scenario_probabilities": list(self.scenario_probabilities),
            "first_stage": _jsonable(self.first_stage),
            "scenario_results": [_jsonable(r) for r in self.scenario_results],
            "scenario_validation_passed": list(self.scenario_validation_passed),
            "ok": self.ok,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return float(value)
    return value


def _failure(status: str, name: str, term: str, msg: str, runtime: float,
             battery_mode: str, risk: RiskConfig | None) -> StochasticResult:
    return StochasticResult(status=status, solver=name, termination_condition=term,
                            message=msg, runtime_seconds=runtime,
                            battery_mode=battery_mode, risk=risk)


# --------------------------------------------------------------- model ----
def _add_common_blocks(m: Any, problem: ScheduleProblem, allow_unmet: bool, pyo: Any) -> None:
    """Add the deterministic constraint blocks (physics) shared with the LP model.

    This mirrors :func:`gridmind.scheduling.build_schedule_model` but parameterises
    every variable by a leading scenario index ``s``. Keeping the physics in one
    place guarantees the stochastic model uses identical dynamics.
    """
    net, hor = problem.network, problem.horizon
    T = list(range(problem.periods))
    dt = problem.step_hours
    nodes = [n.id for n in net.nodes]
    gens = [g.id for g in net.generators]
    lines = [l.id for l in net.lines]
    batts = [b.id for b in problem.batteries]

    m.soc_dyn = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: (
        mm.e[s, b, t] == (mm.bat_soc0[b] if t == 0 else mm.e[s, b, t - 1])
        + mm.bat_ec[b] * mm.ch[s, b, t] * dt - mm.dis[s, b, t] * dt / mm.bat_ed[b]))
    with_final = [(s, b.id) for s in m.S for b in problem.batteries if b.final_soc_mwh is not None]
    m.soc_final = pyo.Constraint(with_final, rule=lambda mm, s, b: (
        mm.e[s, b, problem.periods - 1] == _final_soc(problem, b)))

    m.gen_ub = pyo.Constraint(m.S, m.G, m.T, rule=lambda mm, s, g, t: (
        mm.p[s, g, t] <= mm.gen_limit[s, g, t]))
    m.line_ub = pyo.Constraint(m.S, m.L, m.T, rule=lambda mm, s, l, t: mm.f[s, l, t] <= mm.line_cap[l, t])
    m.line_lb = pyo.Constraint(m.S, m.L, m.T, rule=lambda mm, s, l, t: mm.f[s, l, t] >= -mm.line_cap[l, t])

    ramp_up_t = [(s, g, t) for s in m.S for g in _ramp_gens(problem, "up") for t in T[1:]]
    ramp_down_t = [(s, g, t) for s in m.S for g in _ramp_gens(problem, "down") for t in T[1:]]
    if ramp_up_t:
        m.ramp_up = pyo.Constraint(ramp_up_t, rule=lambda mm, s, g, t: (
            mm.p[s, g, t] - mm.p[s, g, t - 1] <= _ramp(problem, g, "up")))
    if ramp_down_t:
        m.ramp_down = pyo.Constraint(ramp_down_t, rule=lambda mm, s, g, t: (
            mm.p[s, g, t - 1] - mm.p[s, g, t] <= _ramp(problem, g, "down")))
    init_up = [(s, g, g0) for s in m.S for g in _ramp_gens(problem, "up")
               for g0 in [problem.initial_dispatch_mw.get(g)] if g0 is not None]
    init_down = [(s, g, g0) for s in m.S for g in _ramp_gens(problem, "down")
                 for g0 in [problem.initial_dispatch_mw.get(g)] if g0 is not None]
    if init_up:
        m.ramp_init_up = pyo.Constraint(init_up, rule=lambda mm, s, g, g0: (
            mm.p[s, g, 0] - g0 <= _ramp(problem, g, "up")))
    if init_down:
        m.ramp_init_down = pyo.Constraint(init_down, rule=lambda mm, s, g, g0: (
            g0 - mm.p[s, g, 0] <= _ramp(problem, g, "down")))

    m.charge_ub = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: mm.ch[s, b, t] <= mm.bat_pc[b])
    m.discharge_ub = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: mm.dis[s, b, t] <= mm.bat_pd[b])
    m.soc_ub = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: mm.e[s, b, t] <= mm.bat_cap[b])
    m.soc_lb = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: mm.e[s, b, t] >= mm.bat_min[b])

    gens_at = {n: [g.id for g in net.generators if g.node == n] for n in nodes}
    into = {n: [l.id for l in net.lines if l.to_node == n] for n in nodes}
    out_of = {n: [l.id for l in net.lines if l.from_node == n] for n in nodes}
    batts_at = {n: [b.id for b in problem.batteries if b.node == n] for n in nodes}

    def balance(mm: Any, s: str, n: str, t: int) -> Any:
        return (sum(mm.p[s, g, t] for g in gens_at[n])
                + sum(mm.f[s, l, t] for l in into[n]) - sum(mm.f[s, l, t] for l in out_of[n])
                + sum(mm.dis[s, b, t] for b in batts_at[n]) - sum(mm.ch[s, b, t] for b in batts_at[n])
                + mm.u[s, n, t]) == mm.demand[s, n, t]

    m.balance = pyo.Constraint(m.S, m.N, m.T, rule=balance)
    if allow_unmet:
        m.unmet_ub = pyo.Constraint(m.S, m.N, m.T, rule=lambda mm, s, n, t: mm.u[s, n, t] <= mm.demand[s, n, t])
    else:
        m.unmet_ub = pyo.Constraint(m.S, m.N, m.T, rule=lambda mm, s, n, t: mm.u[s, n, t] == 0.0)


def _ramp_gens(problem: ScheduleProblem, direction: str) -> list[str]:
    attr = "ramp_up_mw" if direction == "up" else "ramp_down_mw"
    return [g.id for g in problem.network.generators if getattr(g, attr) is not None]


def _ramp(problem: ScheduleProblem, gen_id: str, direction: str) -> float:
    g = problem.network.generator(gen_id)
    return float(g.ramp_up_mw if direction == "up" else g.ramp_down_mw)


def _final_soc(problem: ScheduleProblem, battery_id: str) -> float:
    return float(problem.battery(battery_id).final_soc_mwh)


def _scenario_cost_expr(m: Any, problem: ScheduleProblem, s: str, pyo: Any) -> Any:
    net, dt = problem.network, problem.step_hours
    penalty = net.unmet_penalty_per_mwh
    thr = {b.id: b.throughput_cost_per_mwh for b in problem.batteries}
    eps = problem.tiebreak_epsilon
    return dt * (sum(net.generator(g).cost_per_mwh * m.p[s, g, t] for g in m.G for t in m.T)
                 + penalty * sum(m.u[s, n, t] for n in m.N for t in m.T)
                 + sum(thr[b] * m.dis[s, b, t] for b in m.B for t in m.T)) \
        + eps * dt * sum(m.ch[s, b, t] + m.dis[s, b, t] for b in m.B for t in m.T)


def build_stochastic_model(problem: ScheduleProblem, scenario_set: ScenarioSet, *,
                           risk: RiskConfig, allow_unmet: bool = True,
                           battery_mode: str = "lp") -> Any:
    """Construct the two-stage risk-aware Pyomo model (see module docstring)."""
    import pyomo.environ as pyo

    battery_mode = normalize_battery_mode(battery_mode)
    risk.validate(problem.periods)
    problem.validate()
    if scenario_set.scenarios[0].periods != problem.periods:
        raise ValueError("scenario set periods do not match the problem")

    net, hor = problem.network, problem.horizon
    periods = problem.periods
    T = list(range(periods))
    lead = risk.lead_periods
    nodes = [n.id for n in net.nodes]
    gens = [g.id for g in net.generators]
    lines = [l.id for l in net.lines]
    batts = [b.id for b in problem.batteries]
    scen_ids = [s.id for s in scenario_set.scenarios]
    prob_of = {s.id: float(s.probability) for s in scenario_set.scenarios}
    scen_of = {s.id: s for s in scenario_set.scenarios}
    gen_of = {g.id: g for g in net.generators}

    m = pyo.ConcreteModel(name="GridMindStochastic")
    m.S = pyo.Set(initialize=scen_ids, ordered=True)
    m.T = pyo.Set(initialize=T, ordered=True)
    m.N = pyo.Set(initialize=nodes, ordered=True)
    m.G = pyo.Set(initialize=gens, ordered=True)
    m.L = pyo.Set(initialize=lines, ordered=True)
    m.B = pyo.Set(initialize=batts, ordered=True)

    # Scenario-dependent demand and generator limits (MW). Availability/outage
    # are scenario DATA, not variables.
    m.demand = pyo.Param(m.S, m.N, m.T, initialize=lambda mm, s, n, t: float(scen_of[s].demand_mw[t][n]))
    m.gen_limit = pyo.Param(m.S, m.G, m.T, initialize=lambda mm, s, g, t: _gen_limit(
        problem, scen_of[s], gen_of[g], t))
    m.line_cap = pyo.Param(m.L, m.T, initialize=lambda mm, l, t: hor.line_limit(net.line(l), t))

    # Battery parameters (identical across scenarios) and scenario probabilities.
    m.bat_cap = pyo.Param(m.B, initialize={b.id: b.capacity_mwh for b in problem.batteries})
    m.bat_min = pyo.Param(m.B, initialize={b.id: b.min_soc_mwh for b in problem.batteries})
    m.bat_pc = pyo.Param(m.B, initialize={b.id: b.max_charge_mw for b in problem.batteries})
    m.bat_pd = pyo.Param(m.B, initialize={b.id: b.max_discharge_mw for b in problem.batteries})
    m.bat_ec = pyo.Param(m.B, initialize={b.id: b.eta_charge for b in problem.batteries})
    m.bat_ed = pyo.Param(m.B, initialize={b.id: b.eta_discharge for b in problem.batteries})
    m.bat_soc0 = pyo.Param(m.B, initialize={b.id: b.initial_soc_mwh for b in problem.batteries})
    m.prob = pyo.Param(m.S, initialize=prob_of)

    m.p = pyo.Var(m.S, m.G, m.T, domain=pyo.NonNegativeReals)
    m.f = pyo.Var(m.S, m.L, m.T, domain=pyo.Reals)
    m.u = pyo.Var(m.S, m.N, m.T, domain=pyo.NonNegativeReals)
    m.ch = pyo.Var(m.S, m.B, m.T, domain=pyo.NonNegativeReals)
    m.dis = pyo.Var(m.S, m.B, m.T, domain=pyo.NonNegativeReals)
    m.e = pyo.Var(m.S, m.B, m.T, domain=pyo.NonNegativeReals)

    _add_common_blocks(m, problem, allow_unmet, pyo)

    if battery_mode == "exclusive" and batts:
        m.z = pyo.Var(m.S, m.B, m.T, domain=pyo.Binary)
        m.exclusive_charge = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: (
            mm.ch[s, b, t] <= mm.bat_pc[b] * (1 - mm.z[s, b, t])))
        m.exclusive_discharge = pyo.Constraint(m.S, m.B, m.T, rule=lambda mm, s, b, t: (
            mm.dis[s, b, t] <= mm.bat_pd[b] * mm.z[s, b, t]))

    _add_nonanticipativity(m, problem, lead, pyo)

    # Risk: expected cost + CVaR (Rockafellar-Uryasev).
    m.scenario_cost = pyo.Expression(m.S, rule=lambda mm, s: _scenario_cost_expr(mm, problem, s, pyo))
    m.expected_cost = pyo.Expression(expr=sum(prob_of[s] * m.scenario_cost[s] for s in scen_ids))
    m.eta = pyo.Var(domain=pyo.Reals)
    m.cvar_excess = pyo.Var(m.S, domain=pyo.NonNegativeReals)
    m.cvar_def = pyo.Constraint(m.S, rule=lambda mm, s: mm.cvar_excess[s] >= mm.scenario_cost[s] - mm.eta)
    m.cvar = pyo.Expression(expr=m.eta + (1.0 / (1.0 - risk.cvar_alpha))
                            * sum(prob_of[s] * m.cvar_excess[s] for s in scen_ids))
    lam = risk.risk_weight
    m.total_risk = pyo.Objective(
        expr=(1.0 - lam) * m.expected_cost + lam * m.cvar, sense=pyo.minimize)
    return m


def _gen_limit(problem: ScheduleProblem, scenario: UncertaintyScenario, gen: Any, t: int) -> float:
    online = problem.horizon.generator_online[t][gen.id]
    if not online:
        return 0.0
    return max(0.0, min(gen.capacity_mw, float(scenario.availability_mw[t][gen.id])))


def _add_nonanticipativity(m: Any, problem: ScheduleProblem, lead: int, pyo: Any) -> None:
    """Enforce that first-stage decisions agree across every scenario.

    Only the *committed* quantities are shared - battery charge ``ch``,
    discharge ``dis``, state of charge ``e`` and (when present) the exclusivity
    binary ``z``. Storage is what must be committed before uncertain demand and
    availability resolve. Generator output, line flows and unmet demand stay
    scenario-specific (generators can be redispatched within a period and flows
    and unserved energy are operational outcomes, not here-and-now commitments).
    Shared first-stage variables ``CH/DIS/E/(Z)`` are created for ``t < lead``
    and every scenario's variable is coupled to them by equality.
    """
    if lead <= 0:
        return
    first_T = list(range(lead))
    m.T1 = pyo.Set(initialize=first_T, ordered=True)
    m.CH = pyo.Var(m.B, m.T1, domain=pyo.NonNegativeReals)
    m.DIS = pyo.Var(m.B, m.T1, domain=pyo.NonNegativeReals)
    m.E = pyo.Var(m.B, m.T1, domain=pyo.NonNegativeReals)

    def eq_ch(mm: Any, s: str, b: str, t: int) -> Any:
        return mm.ch[s, b, t] == mm.CH[b, t]

    def eq_dis(mm: Any, s: str, b: str, t: int) -> Any:
        return mm.dis[s, b, t] == mm.DIS[b, t]

    def eq_e(mm: Any, s: str, b: str, t: int) -> Any:
        return mm.e[s, b, t] == mm.E[b, t]

    m.na_ch = pyo.Constraint(m.S, m.B, m.T1, rule=eq_ch)
    m.na_dis = pyo.Constraint(m.S, m.B, m.T1, rule=eq_dis)
    m.na_e = pyo.Constraint(m.S, m.B, m.T1, rule=eq_e)
    if hasattr(m, "z"):
        m.Z = pyo.Var(m.B, m.T1, domain=pyo.Binary)

        def eq_z(mm: Any, s: str, b: str, t: int) -> Any:
            return mm.z[s, b, t] == mm.Z[b, t]

        m.na_z = pyo.Constraint(m.S, m.B, m.T1, rule=eq_z)


# --------------------------------------------------------------- solve ----
def solve_stochastic(problem: ScheduleProblem, scenario_set: ScenarioSet, *,
                     risk: RiskConfig | None = None, allow_unmet: bool = True,
                     battery_mode: str = "lp", validate: bool = True) -> StochasticResult:
    """Solve the two-stage risk-aware program and return a :class:`StochasticResult`.

    Solver resolution and error handling mirror :func:`gridmind.scheduling.solve_schedule`:
    exclusive mode requires a verified MIP solver and never falls back to LP;
    non-optimal outcomes return explicit statuses with no fabricated numbers.
    """
    risk = risk or RiskConfig()
    strict = not allow_unmet
    try:
        battery_mode = normalize_battery_mode(battery_mode)
        risk.validate(problem.periods)
    except ValueError as exc:
        return _failure("error", solver_name(), "invalid_configuration", str(exc), 0.0, "lp", risk)
    if scenario_set.scenarios[0].periods != problem.periods:
        return _failure("error", solver_name(), "invalid_scenario_dimensions",
                        f"scenario periods {scenario_set.scenarios[0].periods} != problem periods "
                        f"{problem.periods}", 0.0, battery_mode, risk)

    mip = battery_mode == "exclusive"
    try:
        name, solver = resolve_solver(mip=mip)
    except SolverUnavailableError as exc:
        return _failure("solver_unavailable", solver_name(), "not_run", str(exc), 0.0, battery_mode, risk)

    import pyomo.environ as pyo
    from pyomo.opt import SolverStatus, TerminationCondition

    try:
        model = build_stochastic_model(problem, scenario_set, risk=risk,
                                       allow_unmet=allow_unmet, battery_mode=battery_mode)
    except ValueError as exc:
        return _failure("error", name, "model_build_failed", f"invalid problem/config: {exc}",
                        0.0, battery_mode, risk)

    t0 = time.perf_counter()
    try:
        results = solver.solve(model, load_solutions=False, tee=False)
    except Exception as exc:  # noqa: BLE001
        return _failure("error", name, "solver_exception", f"{type(exc).__name__}: {exc}",
                        time.perf_counter() - t0, battery_mode, risk)
    runtime = time.perf_counter() - t0

    term = results.solver.termination_condition
    term_s = str(term)
    if term != TerminationCondition.optimal or results.solver.status not in (
            SolverStatus.ok, SolverStatus.warning):
        status = {TerminationCondition.infeasible: "infeasible",
                  TerminationCondition.unbounded: "unbounded",
                  TerminationCondition.infeasibleOrUnbounded: "infeasible_or_unbounded"}.get(term, "error")
        msg = {"infeasible": "stochastic model is infeasible: no schedule satisfies all scenarios",
               "unbounded": "stochastic model is unbounded (check data)"}.get(
            status, f"solver did not reach optimality (status={results.solver.status}, term={term_s})")
        return _failure(status, name, term_s, msg, runtime, battery_mode, risk)

    try:
        model.solutions.load_from(results)
        expected = pyo.value(model.expected_cost, exception=False)
        cvar = pyo.value(model.cvar, exception=False)
        objective = pyo.value(model.total_risk, exception=False)
        eta = pyo.value(model.eta, exception=False)
        scen_ids = list(model.S)
        scenario_costs = tuple(pyo.value(model.scenario_cost[s], exception=False) for s in scen_ids)
    except Exception as exc:  # noqa: BLE001
        return _failure("error", name, term_s, f"could not read solution: {exc}",
                        runtime, battery_mode, risk)

    if expected is None or cvar is None or objective is None or any(c is None for c in scenario_costs):
        return _failure("error", name, term_s, "solver reported optimal but returned no values",
                        runtime, battery_mode, risk)

    # Extract per-scenario trajectories and validate each independently.
    scen_results: list[Mapping[str, Any]] = []
    validation_flags: list[bool | None] = []
    T = list(range(problem.periods))
    for s in scen_ids:
        sc = scenario_to_problem(problem, scenario_set.scenario(s))
        dispatch = {g: [_clean(pyo.value(model.p[s, g, t], exception=False)) for t in T] for g in model.G}
        flows = {l: [_clean(pyo.value(model.f[s, l, t], exception=False)) for t in T] for l in model.L}
        unmet = {n: [_clean(pyo.value(model.u[s, n, t], exception=False)) for t in T] for n in model.N}
        charge = {b: [_clean(pyo.value(model.ch[s, b, t], exception=False)) for t in T] for b in model.B}
        discharge = {b: [_clean(pyo.value(model.dis[s, b, t], exception=False)) for t in T] for b in model.B}
        soc = {b: [_clean(pyo.value(model.e[s, b, t], exception=False)) for t in T] for b in model.B}
        rep = None
        if validate:
            rep = _validate_scenario(problem, sc, dispatch, flows, unmet, charge, discharge, soc,
                                     reported=float(scenario_costs[scen_ids.index(s)]),
                                     allow_unmet=allow_unmet, battery_mode=battery_mode)
        validation_flags.append(None if rep is None else rep.passed)
        scen_results.append({
            "scenario_id": s, "probability": float(scenario_set.scenario(s).probability),
            "cost_usd": float(scenario_costs[scen_ids.index(s)]),
            "dispatch": dispatch, "flows": flows, "unmet": unmet,
            "charge": charge, "discharge": discharge, "soc": soc,
            "validation_passed": None if rep is None else rep.passed,
            "validation_failed_checks": [] if rep is None else [c.name for c in rep.failed()],
        })

    first_stage = _extract_first_stage(model, risk, T, pyo)
    return StochasticResult(
        status="optimal", solver=name, termination_condition=term_s,
        message="optimal two-stage solution found",
        runtime_seconds=runtime, battery_mode=battery_mode, risk=risk,
        expected_cost_usd=float(expected), cvar_usd=float(cvar),
        objective_usd=float(objective), risk_neutral_cost_usd=float(expected),
        var_usd=float(eta), scenario_costs_usd=tuple(float(c) for c in scenario_costs),
        scenario_probabilities=tuple(float(scenario_set.scenario(s).probability) for s in scen_ids),
        first_stage=first_stage, scenario_results=tuple(scen_results),
        scenario_validation_passed=tuple(validation_flags))


def _extract_first_stage(model: Any, risk: RiskConfig, T: list[int], pyo: Any) -> dict[str, Any]:
    if risk.lead_periods <= 0:
        return {"lead_periods": 0}
    lead_T = list(range(risk.lead_periods))
    return {
        "lead_periods": risk.lead_periods,
        "charge": {b: [pyo.value(model.CH[b, t], exception=False) for t in lead_T] for b in model.B},
        "discharge": {b: [pyo.value(model.DIS[b, t], exception=False) for t in lead_T] for b in model.B},
        "soc": {b: [pyo.value(model.E[b, t], exception=False) for t in lead_T] for b in model.B},
    }


def _validate_scenario(problem: ScheduleProblem, scenario_problem: ScheduleProblem,
                       dispatch: Any, flows: Any, unmet: Any, charge: Any, discharge: Any, soc: Any,
                       reported: float, allow_unmet: bool, battery_mode: str) -> Any:
    """Validate one scenario's trajectories in a Riemann sum (no Pyomo)."""
    import math
    dt = problem.step_hours
    net, hor = scenario_problem.network, scenario_problem.horizon
    T = list(range(problem.periods))
    ok = True
    # generator limits
    for g in net.generators:
        for t in T:
            limit = hor.generation_limit(g, t)
            if dispatch[g.id][t] < -1e-5 or dispatch[g.id][t] > limit + 1e-5:
                ok = False
    # power balance
    for n in net.nodes:
        for t in T:
            gen = sum(dispatch[g.id][t] for g in net.generators if g.node == n.id)
            inflow = sum(flows[l.id][t] for l in net.lines if l.to_node == n.id)
            outflow = sum(flows[l.id][t] for l in net.lines if l.from_node == n.id)
            dis = sum(discharge[b.id][t] for b in problem.batteries if b.node == n.id)
            ch = sum(charge[b.id][t] for b in problem.batteries if b.node == n.id)
            if abs(gen + inflow - outflow + dis - ch + unmet[n.id][t] - hor.demand(n.id, t)) > 1e-4:
                ok = False
    # battery dynamics + exclusivity
    for b in problem.batteries:
        prev = b.initial_soc_mwh
        for t in T:
            expected = prev + b.eta_charge * charge[b.id][t] * dt - discharge[b.id][t] * dt / b.eta_discharge
            if abs(soc[b.id][t] - expected) > 1e-4:
                ok = False
            if battery_mode == "exclusive" and min(charge[b.id][t], discharge[b.id][t]) > 1e-5:
                ok = False
            prev = soc[b.id][t]
    # objective consistency (operating + tiebreak)
    penalty = net.unmet_penalty_per_mwh
    gen_cost = dt * sum(net.generator(g).cost_per_mwh * v for g, series in dispatch.items() for v in series)
    pen = dt * penalty * sum(v for series in unmet.values() for v in series)
    thr = dt * sum(problem.battery(b).throughput_cost_per_mwh * v
                   for b, series in discharge.items() for v in series)
    tie = dt * problem.tiebreak_epsilon * (sum(v for series in charge.values() for v in series)
                                           + sum(v for series in discharge.values() for v in series))
    recomputed = gen_cost + pen + thr + tie
    if abs(recomputed - reported) / max(1.0, abs(reported)) > 1e-6:
        ok = False
    return _SimpleReport(ok)


@dataclass
class _SimpleReport:
    passed: bool

    def failed(self) -> list[Any]:
        return [] if self.passed else [_SimpleCheck()]


@dataclass
class _SimpleCheck:
    name: str = "stochastic_scenario_validation"
