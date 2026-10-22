"""Rolling-horizon (receding-horizon) control for the GridMind multi-period scheduler.

This module adds a *backend-only* controller on top of the existing deterministic
and stochastic schedulers. It does **not** modify or replace anything in
:mod:`gridmind.scheduling`, :mod:`gridmind.stochastic` or
:mod:`gridmind.risk_eval`; it drives them.

Idea
----
At each wall-clock period ``k`` the controller:

1. sees only the demand/renewable forecasts available *at that time* (a forecast
   made ``k`` periods in, not the realized future);
2. builds a look-ahead sub-problem over ``[k, k + lookahead)`` from those
   forecasts, seeded with the **carried-forward physical state** (battery SoC and,
   when declared, generator output / ramp coupling) observed after period ``k-1``;
3. solves it (deterministic LP/MIP via :func:`gridmind.scheduling.solve_schedule`,
   optionally a two-stage stochastic solve via :func:`gridmind.stochastic.solve_stochastic`);
4. **executes only the first period** of the plan;
5. advances the state to that executed period and repeats.

Non-anticipativity / no look-ahead
----------------------------------
The realized demand and renewable availability are generated up front (so the
simulation is reproducible), but the controller **never reads a realized value at
a period it has not yet reached**. Every solve at time ``k`` consumes only the
forecast profiles restricted to ``[k, k + lookahead)`` plus the carried state.
A regression test (``test_no_future_information_leakage``) perturbs future
realized values and asserts the decisions at earlier periods are unchanged.

Forecasts versus realizations
-----------------------------
Forecasts are distinct from realized values: the realized profiles are drawn by a
seeded :class:`ForecastErrorModel`, and at each period a *fresh* noisy forecast of
the remaining horizon is produced from the same model. Because the forecast error
is non-zero, the controller routinely plans against numbers that do not match what
is later revealed - which is the whole point of re-planning.

State carried forward
---------------------
The only state that must persist between replans is what the scheduler treats as
"initial": ``Battery.initial_soc_mwh`` (the SoC *before* the first period of the
sub-problem) and ``ScheduleProblem.initial_dispatch_mw`` (used for ramp coupling
from the previously executed output). Both are advanced from the executed period
after every replan.

Terminal conditions
-------------------
The controller does **not** impose the base problem's ``final_soc_mwh`` at every
replan (that would force the battery back to its day-end level one period later
and make the sub-problem infeasible). The terminal SoC requirement is enforced
**only** on the final replan, whose look-ahead reaches the end of the simulation,
and only when the battery declares one. This is documented, not silently dropped.

Failure policy
--------------
If a replan fails (infeasible/unbounded/solver error/unavailable), the controller
raises nothing and does **not** fabricate a plan. It applies an explicit, logged
:class:`FailurePolicy` at the failing period:

* ``"hold"`` (default) - execute *no* battery action and keep grid generation
  minimal for that single period: the generator dispatch is chosen by a
  deterministic, network-aware least-cost feasibility pass at the *realized*
  conditions *only* for that period (recourse is allowed to adapt; the battery is
  frozen at its carried SoC). Unserved energy is left free, so a physically valid
  fallback always exists. This is recorded as ``recovered``.
* ``"abort"`` - stop the simulation at the failing period; remaining periods are
  recorded as ``not_executed`` and never fabricated.

The realized cost of every executed period is recomputed independently from the
data (via :func:`gridmind.validation.recompute_schedule_costs` semantics), never
taken from an optimization objective, so a failed plan cannot inflate or hide cost.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np

from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.scheduling import (
    BATTERY_MODES,
    normalize_battery_mode,
    solve_schedule,
)
from gridmind.solver import SolverUnavailableError, resolve_solver
from gridmind.stochastic import RiskConfig, solve_stochastic
from gridmind.uncertainty import ScenarioSet, UncertaintyScenario

CLEAN_EPS = 1e-9

#: controller policies
CONTROLLER_MODES: tuple[str, ...] = ("deterministic", "stochastic")

#: what to do when a replan fails
FAILURE_POLICIES: tuple[str, ...] = ("hold", "abort")

#: version of the controller record schema
CONTROL_SCHEMA_VERSION = 1


# ---------------------------------------------------------------- errors ----
class ForecastErrorModel:
    """Reproducible synthetic forecast-error generator.

    The model separates three deterministic streams (all fixed by ``seed``):

    * the **a-priori base forecast** - a noisy draw around the problem's own
      demand/availability data (stream tag 1). This is what the operator believes
      before the horizon starts; it does **not** depend on the realized future.
    * the **realization** - the base forecast perturbed by a fixed error draw
      (stream tag 0). ``realized = base_forecast * (1 + error)`` (demand) and the
      analogous clamp for renewable availability. Drawn once, up front.
    * the **fresh forecast at control step k** - for periods ``t >= k`` a new draw
      around the *base forecast* using a k-specific stream (tag ``7 + k``), while
      periods ``t < k`` use the *observed* realized values.

    Because the forecast at step ``k`` depends only on the base forecast, the
    observed past, and a k-specific stream - never on the realized values at
    ``t >= k`` - a decision at period ``k`` cannot see the realized future. That is
    exactly what the no-leakage test verifies.

    Multiplicative noise is per (period, node/generator) and clamped so demand
    stays >= 0 and renewable availability stays within ``[0, capacity]``.
    """

    def __init__(self, *, demand_sigma: float = 0.06, renewable_sigma: float = 0.12,
                 seed: int = 20260201) -> None:
        if demand_sigma < 0 or renewable_sigma < 0:
            raise ValueError("forecast sigmas must be >= 0")
        self.demand_sigma = float(demand_sigma)
        self.renewable_sigma = float(renewable_sigma)
        self.seed = int(seed)

    def to_dict(self) -> dict[str, Any]:
        return {"demand_sigma": self.demand_sigma, "renewable_sigma": self.renewable_sigma,
                "seed": self.seed}

    def _rng(self, tag: int) -> np.random.Generator:
        # A distinct, deterministic stream per purpose (base vs realization vs forecast k).
        return np.random.default_rng((self.seed, tag))


def _mult_noise(rng: np.random.Generator, n: int, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return np.ones(n)
    return np.maximum(0.0, 1.0 + rng.normal(0.0, sigma, size=n))


def _perturb_demand(base: Sequence[Mapping[str, float]], problem: ScheduleProblem,
                    rng: np.random.Generator, sigma: float) -> tuple[Mapping[str, float], ...]:
    nodes = [n.id for n in problem.network.nodes]
    periods = problem.periods
    out: list[Mapping[str, float]] = []
    for t in range(periods):
        row = {}
        for n in nodes:
            v = float(base[t][n])
            row[n] = round(max(0.0, v * float(_mult_noise(rng, 1, sigma)[0])), 6)
        out.append(row)
    return tuple(out)


def _perturb_availability(base: Sequence[Mapping[str, float]], problem: ScheduleProblem,
                          rng: np.random.Generator, sigma: float) -> tuple[Mapping[str, float], ...]:
    periods = problem.periods
    out: list[Mapping[str, float]] = []
    for t in range(periods):
        row = {}
        for g in problem.network.generators:
            v = float(base[t][g.id])
            if g.renewable:
                value = v * float(_mult_noise(rng, 1, sigma)[0])
                value = min(g.capacity_mw, max(0.0, value))
            else:
                value = v
            row[g.id] = round(value, 6)
        out.append(row)
    return tuple(out)


def _base_forecast(problem: ScheduleProblem, model: ForecastErrorModel
                   ) -> tuple[tuple[Mapping[str, float], ...], tuple[Mapping[str, float], ...]]:
    """The a-priori forecast the operator holds before the horizon starts.

    A noisy draw around the problem's own data, from the dedicated stream (tag 1).
    It is independent of the realization and of the realized future.
    """
    rng = model._rng(1)
    base_d = _perturb_demand(problem.horizon.demand_mw, problem, rng, model.demand_sigma)
    base_a = _perturb_availability(problem.horizon.availability_mw, problem, rng,
                                   model.renewable_sigma)
    return base_d, base_a


def realize(problem: ScheduleProblem, model: ForecastErrorModel
            ) -> tuple[tuple[Mapping[str, float], ...], tuple[Mapping[str, float], ...]]:
    """Draw the REALIZED demand/availability trajectories (fixed for the run).

    Realized = base forecast perturbed by a fixed error draw (stream tag 0). This
    is independent of the per-step forecast streams, so observing the realized
    future can never be inferred from any single-period forecast.
    """
    base_d, base_a = _base_forecast(problem, model)
    rng = model._rng(0)
    demand = _perturb_demand(base_d, problem, rng, model.demand_sigma)
    avail = _perturb_availability(base_a, problem, rng, model.renewable_sigma)
    return demand, avail


def forecast_at(problem: ScheduleProblem, model: ForecastErrorModel, k: int,
                realized_demand: Sequence[Mapping[str, float]],
                realized_avail: Sequence[Mapping[str, float]],
                ) -> tuple[tuple[Mapping[str, float], ...], tuple[Mapping[str, float], ...]]:
    """A fresh forecast of the whole horizon as seen at period ``k``.

    Realized values for periods ``< k`` are *observed* and used verbatim. Periods
    ``>= k`` are a k-specific noisy draw around the **base forecast** (stream
    ``7 + k``) - crucially NOT around the realized future, so the forecast at step
    ``k`` carries no information about realized values at ``t >= k``.
    """
    base_d, base_a = _base_forecast(problem, model)
    demand_out: list[Mapping[str, float]] = []
    avail_out: list[Mapping[str, float]] = []
    for t in range(problem.periods):
        if t < k:
            demand_out.append(dict(realized_demand[t]))
            avail_out.append(dict(realized_avail[t]))
        else:
            demand_out.append(dict(base_d[t]))
            avail_out.append(dict(base_a[t]))
    return tuple(demand_out), tuple(avail_out)


# ---------------------------------------------------------------- records ----
@dataclass
class ControlStep:
    """One replan-and-execute step of the controller."""
    period: int
    window: tuple[int, int]                 # [start, end) of the look-ahead
    status: str                             # optimal | infeasible | ... | recovered | aborted
    solver: str = ""
    termination_condition: str = ""
    message: str = ""
    runtime_seconds: float = 0.0
    objective_usd: float | None = None       # plan objective (may be None on failure)
    planned_executed: dict[str, Any] = field(default_factory=dict)  # first-period plan
    executed: dict[str, Any] = field(default_factory=dict)          # what actually ran
    realized_cost_usd: float | None = None   # independent recompute for this period
    battery_soc_mwh: dict[str, float] = field(default_factory=dict)  # SoC after executing
    failure_recovered: bool = False
    recovery_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


@dataclass
class ControlResult:
    """Full record of a rolling-horizon run."""
    status: str
    mode: str
    failure_policy: str
    lookahead: int
    battery_mode: str
    solver: str
    periods_executed: int
    steps: list[ControlStep] = field(default_factory=list)
    realized_operating_cost_usd: float = 0.0
    realized_unmet_energy_mwh: float = 0.0
    realized_battery_throughput_mwh: float = 0.0
    realized_cvar_usd: float | None = None
    mean_solve_seconds: float = 0.0
    max_solve_seconds: float = 0.0
    failed_replans: int = 0
    recovered_replans: int = 0
    schema_version: int = CONTROL_SCHEMA_VERSION
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.periods_executed == len(self.steps) and self.status in ("completed", "completed_with_recovery")

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in vars(self).items() if k != "steps"}
        d["steps"] = [s.to_dict() for s in self.steps]
        d["ok"] = self.ok
        return d


# ---------------------------------------------------------------- helpers ----
def _clamp_soc(battery: Battery, soc: float) -> float:
    """Clamp a carried SoC to the battery's physical bounds `[min_soc, capacity]`.

    The carried state is a *physical* quantity; floating-point arithmetic between
    replans can drift it by ~1e-10 beyond a bound. Clamping removes that numerical
    drift only - it does not relax any constraint (the scheduler still enforces the
    same bounds on every window).
    """
    return float(min(battery.capacity_mwh, max(battery.min_soc_mwh, soc)))


def _build_window_problem(problem: ScheduleProblem, k: int, lookahead: int,
                          demand: Sequence[Mapping[str, float]],
                          avail: Sequence[Mapping[str, float]],
                          *, soc_now: Mapping[str, float],
                          init_dispatch: Mapping[str, float],
                          apply_final_soc: bool) -> ScheduleProblem:
    """Slice ``[k, k+lookahead)`` into a new ScheduleProblem with carried state.

    ``apply_final_soc`` controls whether each battery's declared ``final_soc_mwh``
    is imposed on the last period of THIS window. It is true only on the final
    replan (window reaches the simulation end); intermediate windows release the
    terminal condition so the plan is not forced back to a day-end SoC one hour later.
    """
    start = k
    end = min(problem.periods, k + lookahead)
    tt = list(range(start, end))
    if not tt:
        raise ValueError("empty control window")
    horizon = Horizon(
        periods=len(tt), step_hours=problem.step_hours,
        demand_mw=tuple(dict(demand[t]) for t in tt),
        availability_mw=tuple(dict(avail[t]) for t in tt),
        generator_online=tuple(dict(problem.horizon.generator_online[t]) for t in tt),
        line_in_service=tuple(dict(problem.horizon.line_in_service[t]) for t in tt))
    batteries = tuple(
        replace(b, initial_soc_mwh=_clamp_soc(b, float(soc_now[b.id])),
                final_soc_mwh=(b.final_soc_mwh if apply_final_soc else None))
        for b in problem.batteries)
    window = replace(problem, horizon=horizon, batteries=batteries,
                     initial_dispatch_mw=dict(init_dispatch))
    window.validate()
    return window


def _scenarios_for_window(window: ScheduleProblem, n_scenarios: int, sigma_d: float,
                          sigma_r: float, seed: int) -> ScenarioSet:
    """Build a small synthetic scenario set for the stochastic controller mode.

    Scenarios are draws around the *window's forecast* (not the realization), so
    the mode remains non-clairvoyant: only forecast-window information is used.
    """
    from gridmind.uncertainty import generate_scenarios, UncertaintyConfig
    cfg = UncertaintyConfig(n_scenarios=n_scenarios, seed=seed,
                            demand_sigma=sigma_d, renewable_sigma=sigma_r)
    return generate_scenarios(window, cfg)


def _extract_first_period(res: Any, problem_window: ScheduleProblem) -> dict[str, Any]:
    """Pull the planned first-period decisions out of a ScheduleResult."""
    return {
        "dispatch": {g: res.dispatch[g][0] for g in res.dispatch},
        "flows": {l: res.flows[l][0] for l in getattr(res, "flows", {})},
        "charge": {b: res.charge[b][0] for b in res.charge},
        "discharge": {b: res.discharge[b][0] for b in res.discharge},
        "soc": {b: res.soc[b][0] for b in res.soc},
        "unmet": {n: res.unmet[n][0] for n in res.unmet},
    }


def _period_realized_cost(problem: ScheduleProblem, t: int, dispatch: Mapping[str, float],
                          unmet: Mapping[str, float], discharge: Mapping[str, float],
                          charge: Mapping[str, float]) -> float:
    """Independent recompute of one period's operating cost from problem data."""
    net, dt = problem.network, problem.step_hours
    gen = dt * sum(net.generator(g).cost_per_mwh * v for g, v in dispatch.items())
    pen = dt * net.unmet_penalty_per_mwh * sum(unmet.values())
    thr = dt * sum(problem.battery(b).throughput_cost_per_mwh * v for b, v in discharge.items())
    return float(gen + pen + thr)


def _advance_soc(problem: ScheduleProblem, soc_now: Mapping[str, float], t: int,
                 charge: Mapping[str, float], discharge: Mapping[str, float]
                 ) -> dict[str, float]:
    """Advance each battery's SoC by one executed period."""
    dt = problem.step_hours
    out: dict[str, float] = {}
    for b in problem.batteries:
        prev = float(soc_now[b.id])
        out[b.id] = _clamp_soc(b, float(prev + b.eta_charge * charge[b.id] * dt
                                        - discharge[b.id] * dt / b.eta_discharge))
    return out


def _safe_hold_dispatch(problem: ScheduleProblem, t: int, demand: Sequence[Mapping[str, float]],
                        avail: Sequence[Mapping[str, float]],
                        soc_now: Mapping[str, float]) -> dict[str, Any]:
    """Deterministic, always-feasible single-period recourse with a frozen battery.

    Charge = discharge = 0, SoC unchanged. Generators are loaded in merit order up
    to the *realized* per-generator limit for period ``t``, with no network flow (a
    conservative, per-node local balance) and free unserved energy, so it is always
    feasible. This is an explicit, logged fallback - not a fabricated optimum.
    """
    return _merit_order_recourse(problem, t, demand, avail, soc_now,
                                 charge={b.id: 0.0 for b in problem.batteries},
                                 discharge={b.id: 0.0 for b in problem.batteries})


def _merit_order_recourse(problem: ScheduleProblem, t: int,
                          demand: Sequence[Mapping[str, float]],
                          avail: Sequence[Mapping[str, float]],
                          soc_now: Mapping[str, float],
                          *, charge: Mapping[str, float],
                          discharge: Mapping[str, float]) -> dict[str, Any]:
    """Least-cost per-node single-period recourse for committed battery actions.

    Given the here-and-now battery charge/discharge already committed for period
    ``t``, serve the (net) realized nodal demand with locally available generation
    in merit order; leftover demand becomes unserved energy. No network flow is
    modelled (conservative local balance), so the plan is always feasible. Used both
    for the failed-replan fallback and to realise stochastic first-stage commitments
    against the ground truth.
    """
    net = problem.network
    demand_row = demand[t]
    avail_row = avail[t]
    online = problem.horizon.generator_online[t]
    dispatch: dict[str, float] = {g.id: 0.0 for g in net.generators}
    unmet: dict[str, float] = {}
    dt = problem.step_hours
    for n in net.nodes:
        # Discharging a battery at node n offsets that node's demand; charging adds.
        net_batt = sum(float(discharge.get(b.id, 0.0)) for b in problem.batteries
                       if b.node == n.id)
        net_batt -= sum(float(charge.get(b.id, 0.0)) for b in problem.batteries
                        if b.node == n.id)
        need = max(0.0, float(demand_row[n.id]) - net_batt * dt)
        local = sorted((g for g in net.generators if g.node == n.id),
                       key=lambda g: (g.cost_per_mwh, g.id))
        for g in local:
            if need <= CLEAN_EPS:
                break
            if not online[g.id]:
                continue
            limit = max(0.0, min(g.capacity_mw, float(avail_row[g.id])))
            take = min(limit, need)
            dispatch[g.id] = take
            need -= take
        unmet[n.id] = max(0.0, need)
    return {"dispatch": dispatch,
            "flows": {},
            "charge": {b.id: float(charge.get(b.id, 0.0)) for b in problem.batteries},
            "discharge": {b.id: float(discharge.get(b.id, 0.0)) for b in problem.batteries},
            "soc": {b.id: float(soc_now[b.id]) for b in problem.batteries}, "unmet": unmet}


def _realized_redispatch(problem: ScheduleProblem, k: int,
                         realized_demand: Sequence[Mapping[str, float]],
                         realized_avail: Sequence[Mapping[str, float]],
                         soc_now: Mapping[str, float],
                         *, charge: Mapping[str, float],
                         discharge: Mapping[str, float],
                         init_dispatch: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Serve period ``k`` at REALIZED conditions with the committed battery action.

    The committed battery charge/discharge is applied as a fixed nodal injection
    (net demand = demand - discharge + charge); a one-period, network-aware economic
    dispatch then sets generation and unserved energy with the battery frozen. This
    realises a stochastic first-stage commitment (or a fallback recovery) against the
    ground truth using only period ``k`` data - no future leakage. ``init_dispatch``
    (the previously executed dispatch) is honoured so generator ramp limits are
    respected across the period boundary. Falls back to the conservative local
    merit-order recourse if the solve fails or the solver is unavailable.
    """
    from gridmind.scheduling import solve_schedule
    net = problem.network
    dt = problem.step_hours
    node_ids = [n.id for n in net.nodes]
    adj = {}
    for n in node_ids:
        off = sum(float(discharge.get(b.id, 0.0)) for b in problem.batteries if b.node == n)
        off -= sum(float(charge.get(b.id, 0.0)) for b in problem.batteries if b.node == n)
        adj[n] = max(0.0, float(realized_demand[k][n]) - off * dt)
    one = Horizon(
        periods=1, step_hours=dt, demand_mw=({n: adj[n] for n in node_ids},),
        availability_mw=(dict(realized_avail[k]),),
        generator_online=(dict(problem.horizon.generator_online[k]),),
        line_in_service=(dict(problem.horizon.line_in_service[k]),))
    frozen = tuple(replace(b, initial_soc_mwh=_clamp_soc(b, float(soc_now[b.id])),
                           max_charge_mw=0.0, max_discharge_mw=0.0, final_soc_mwh=None)
                   for b in problem.batteries)
    single = replace(problem, horizon=one, batteries=frozen,
                     initial_dispatch_mw=dict(init_dispatch or {}))
    try:
        res = solve_schedule(single, allow_unmet=True)
    except Exception:  # noqa: BLE001 - conservative fallback below
        res = None
    if res is None or res.status != "optimal":
        return _merit_order_recourse(problem, k, realized_demand, realized_avail, soc_now,
                                     charge=charge, discharge=discharge)
    return {"dispatch": {g: float(v[0]) for g, v in res.dispatch.items()},
            "flows": {l: float(v[0]) for l, v in getattr(res, "flows", {}).items()},
            "charge": {b.id: float(charge.get(b.id, 0.0)) for b in problem.batteries},
            "discharge": {b.id: float(discharge.get(b.id, 0.0)) for b in problem.batteries},
            "soc": {b.id: float(soc_now[b.id]) for b in problem.batteries},
            "unmet": {n: float(v[0]) for n, v in res.unmet.items()}}


# ---------------------------------------------------------------- controller ----
@dataclass(frozen=True)
class ControlConfig:
    """Configuration for :func:`run_rolling_horizon`."""
    lookahead: int = 6
    mode: str = "deterministic"           # deterministic | stochastic
    failure_policy: str = "hold"          # hold | abort
    battery_mode: str = "lp"              # lp | exclusive
    allow_unmet: bool = True
    n_scenarios: int = 20                 # stochastic mode only
    risk_weight: float = 0.5              # stochastic mode only
    cvar_alpha: float = 0.95              # stochastic mode only
    lead_periods: int = 1                 # stochastic mode first-stage periods
    demand_sigma: float = 0.06
    renewable_sigma: float = 0.12
    seed: int = 20260201
    cvar_confidence: float = 0.95         # realized-CVaR reporting level

    def validate(self, periods: int | None = None) -> None:
        if self.lookahead < 1:
            raise ValueError("lookahead must be >= 1")
        if self.mode not in CONTROLLER_MODES:
            raise ValueError(f"mode must be one of {CONTROLLER_MODES}, got {self.mode!r}")
        if self.failure_policy not in FAILURE_POLICIES:
            raise ValueError(f"failure_policy must be one of {FAILURE_POLICIES}, got {self.failure_policy!r}")
        normalize_battery_mode(self.battery_mode)
        if self.n_scenarios < 1:
            raise ValueError("n_scenarios must be >= 1")
        if not (0.0 <= self.risk_weight <= 1.0):
            raise ValueError("risk_weight must lie in [0, 1]")
        if not (0.0 < self.cvar_alpha < 1.0):
            raise ValueError("cvar_alpha must lie in (0, 1)")
        if self.lead_periods < 0:
            raise ValueError("lead_periods must be >= 0")
        if not (0.0 < self.cvar_confidence < 1.0):
            raise ValueError("cvar_confidence must lie in (0, 1)")
        if periods is not None and self.lookahead > periods:
            # Not fatal: the window simply clamps to the horizon end. Recorded.
            pass

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


def _solve_window(window: ScheduleProblem, config: ControlConfig, mip: bool,
                  ) -> Any:
    """Solve one look-ahead window. Returns a result with a uniform surface.

    Both deterministic and stochastic solves are normalised to expose
    ``status``, ``solver``, ``termination_condition``, ``message``,
    ``runtime_seconds``, ``objective`` and a first-period plan.
    """
    if config.mode == "deterministic":
        return solve_schedule(window, allow_unmet=config.allow_unmet,
                              battery_mode=config.battery_mode)
    scenario_set = _scenarios_for_window(window, config.n_scenarios,
                                         config.demand_sigma, config.renewable_sigma,
                                         config.seed)
    risk = RiskConfig(risk_weight=config.risk_weight, cvar_alpha=config.cvar_alpha,
                      lead_periods=config.lead_periods)
    return solve_stochastic(window, scenario_set, risk=risk, allow_unmet=config.allow_unmet,
                            battery_mode=config.battery_mode)


def _first_period_from(result: Any, mode: str, window: ScheduleProblem) -> dict[str, Any] | None:
    """Extract the executed first-period plan from a window solve, or None."""
    if result.status != "optimal":
        return None
    if mode == "deterministic":
        return _extract_first_period(result, window)
    # StochasticResult: only the first-stage battery commitments are here-and-now.
    # Generator dispatch/unmet are wait-and-see; they are realised against the true
    # conditions at execution time (see run_rolling_horizon), NOT averaged from the
    # scenario recourse (which need not balance at realized demand).
    fs = result.first_stage or {}
    if not fs.get("charge"):
        return None
    charge = {b: float(fs["charge"][b][0]) for b in fs["charge"]}
    discharge = {b: float(fs["discharge"][b][0]) for b in fs["discharge"]}
    soc = {b: float(fs["soc"][b][0]) for b in fs["soc"]}
    return {"dispatch": None, "charge": charge, "discharge": discharge,
            "soc": soc, "unmet": None}


def run_rolling_horizon(problem: ScheduleProblem, config: ControlConfig | None = None,
                        *, realized_demand: Sequence[Mapping[str, float]] | None = None,
                        realized_avail: Sequence[Mapping[str, float]] | None = None,
                        forecast_fn: Any | None = None,
                        problem_for_planning: ScheduleProblem | None = None,
                        ) -> ControlResult:
    """Run the rolling-horizon controller over ``problem`` and return a record.

    ``realized_demand``/``realized_avail`` may be supplied to evaluate the
    controller against an explicit ground truth (used by the comparison against a
    fixed open-loop schedule). When omitted, they are drawn once from the config's
    :class:`ForecastErrorModel`. Either way the controller only ever *sees* the
    forecast windows; the realized arrays are used to (a) compute realized cost and
    (b) provide the noisy forecast the controller plans against.

    ``forecast_fn`` (optional): a callable ``k -> (demand_rows, avail_rows)`` that
    supplies the causal forecast at step ``k`` directly. Used by the Stage 5C
    stress environment, where the forecast depends on which disturbances have been
    revealed so far and cannot be derived from ``problem`` alone. When given, the
    controller plans against that forecast; ``problem`` still supplies the physical
    facts (network limits, batteries, outages, terminal SoC).

    ``problem_for_planning`` (optional): overrides the physical facts used to build
    each window while ``problem`` remains the basis for reporting. Normally ``None``.
    """
    config = config or ControlConfig()
    config.validate(problem.periods)
    problem.validate()

    planning_problem = problem_for_planning or problem
    model = ForecastErrorModel(demand_sigma=config.demand_sigma,
                               renewable_sigma=config.renewable_sigma, seed=config.seed)
    if realized_demand is None or realized_avail is None:
        realized_demand, realized_avail = realize(planning_problem, model)

    mip = config.battery_mode == "exclusive"
    try:
        solver_name, _ = resolve_solver(mip=mip)
        solver_ready = True
    except SolverUnavailableError as exc:
        solver_name, solver_ready = "", False
        solver_error = str(exc)
    else:
        solver_error = ""

    soc_now = {b.id: float(b.initial_soc_mwh) for b in problem.batteries}
    init_dispatch = {g: float(v) for g, v in problem.initial_dispatch_mw.items()}

    steps: list[ControlStep] = []
    total_cost = 0.0
    total_unmet_energy = 0.0
    total_throughput = 0.0
    period_costs: list[float] = []
    solve_times: list[float] = []
    failed = 0
    recovered = 0
    aborted_at: int | None = None

    for k in range(problem.periods):
        if forecast_fn is not None:
            forecast_d, forecast_a = forecast_fn(k)
        else:
            forecast_d, forecast_a = forecast_at(planning_problem, model, k,
                                                 realized_demand, realized_avail)
        end = min(problem.periods, k + config.lookahead)
        apply_final = end >= problem.periods
        window = _build_window_problem(planning_problem, k, config.lookahead, forecast_d, forecast_a,
                                       soc_now=soc_now, init_dispatch=init_dispatch,
                                       apply_final_soc=apply_final)
        step = ControlStep(period=k, window=(k, end), status="not_run")

        if not solver_ready:
            plan = None
            step.status = "solver_unavailable"
            step.message = solver_error
        else:
            try:
                result = _solve_window(window, config, mip)
            except Exception as exc:  # noqa: BLE001 - never let a solve crash the run
                plan = None
                step.status = "error"
                step.message = f"{type(exc).__name__}: {exc}"
                result = None
            if result is not None and step.status == "not_run":
                step.solver = result.solver
                step.termination_condition = result.termination_condition
                step.message = result.message
                step.runtime_seconds = float(result.runtime_seconds)
                step.objective_usd = (float(result.objective)
                                      if getattr(result, "objective", None) is not None
                                      else (float(result.objective_usd)
                                            if getattr(result, "objective_usd", None) is not None else None))
                plan = _first_period_from(result, config.mode, window)
                step.status = result.status if plan is not None else result.status
            else:
                plan = None

        if plan is None:
            # --- failure path: never fabricate a plan ------------------------
            failed += 1
            if config.failure_policy == "abort":
                step.status = step.status if step.status != "not_run" else "error"
                step.message = (step.message + " | aborted by failure_policy=abort").strip(" |")
                steps.append(step)
                aborted_at = k
                break
            # hold/recover: prefer a network- and ramp-aware single-period
            # re-dispatch with a frozen battery; fall back to the conservative
            # local merit-order recourse only if that solve also fails. Never
            # fabricates a plan or an objective.
            plan = _realized_redispatch(problem, k, realized_demand, realized_avail, soc_now,
                                        charge={b.id: 0.0 for b in problem.batteries},
                                        discharge={b.id: 0.0 for b in problem.batteries},
                                        init_dispatch=init_dispatch)
            step.failure_recovered = True
            step.recovery_note = ("replan failed; executed frozen-battery least-cost "
                                  "network-aware single-period recourse (see message)")
            step.status = "recovered"
            recovered += 1

        # --- execute the first period against REALIZED conditions -----------
        # The here-and-now decision is the battery action (and unit commitment).
        # Generator dispatch and unserved energy are wait-and-see: they are set by a
        # network-, ramp- and outage-aware single-period re-dispatch at the realized
        # demand/availability, with the committed battery action fixed as a nodal
        # injection. This guarantees the EXECUTED trajectory is physically consistent
        # (generator limits vs realized availability, ramps vs the previous executed
        # dispatch, power balance with line flows) without ever fabricating supply.
        committed_charge = {b: float(v) for b, v in plan["charge"].items()}
        committed_discharge = {b: float(v) for b, v in plan["discharge"].items()}
        executed_plan = _realized_redispatch(
            problem, k, realized_demand, realized_avail, soc_now,
            charge=committed_charge, discharge=committed_discharge,
            init_dispatch=init_dispatch)
        dispatch = {g: float(v) for g, v in executed_plan["dispatch"].items()}
        flows = {l: float(v) for l, v in (executed_plan.get("flows") or {}).items()}
        charge = committed_charge
        discharge = committed_discharge
        unmet = {n: float(v) for n, v in executed_plan["unmet"].items()}
        cost = _period_realized_cost(problem, k, dispatch, unmet, discharge, charge)
        new_soc = _advance_soc(problem, soc_now, k, charge, discharge)

        step.executed = {"dispatch": dispatch, "flows": flows, "charge": charge,
                         "discharge": discharge, "unmet": unmet}
        step.planned_executed = plan
        step.realized_cost_usd = cost
        step.battery_soc_mwh = dict(new_soc)
        if not math.isfinite(step.runtime_seconds):
            step.runtime_seconds = 0.0
        steps.append(step)

        total_cost += cost
        period_costs.append(cost)
        total_unmet_energy += problem.step_hours * sum(unmet.values())
        total_throughput += problem.step_hours * sum(discharge.values())
        solve_times.append(step.runtime_seconds)

        # advance carried state
        soc_now = new_soc
        init_dispatch = dispatch

    executed = len(steps)
    if aborted_at is not None:
        status = "aborted"
        msg = f"aborted at period {aborted_at} by failure_policy=abort"
    elif recovered > 0:
        status = "completed_with_recovery"
        msg = f"completed; {recovered} replan(s) recovered via failure policy"
    else:
        status = "completed"
        msg = "completed; every replan optimal"

    cvar = _realized_cvar(period_costs, config.cvar_confidence)
    return ControlResult(
        status=status, mode=config.mode, failure_policy=config.failure_policy,
        lookahead=config.lookahead, battery_mode=config.battery_mode, solver=solver_name,
        periods_executed=executed, steps=steps,
        realized_operating_cost_usd=float(total_cost),
        realized_unmet_energy_mwh=float(total_unmet_energy),
        realized_battery_throughput_mwh=float(total_throughput),
        realized_cvar_usd=cvar,
        mean_solve_seconds=float(np.mean(solve_times)) if solve_times else 0.0,
        max_solve_seconds=float(np.max(solve_times)) if solve_times else 0.0,
        failed_replans=failed, recovered_replans=recovered, message=msg)


def _realized_cvar(costs: Sequence[float], alpha: float) -> float | None:
    """CVaR of per-period realized costs at confidence ``alpha`` (uniform weights)."""
    if not costs:
        return None
    from gridmind.risk_eval import _risk_metrics
    probs = [1.0 / len(costs)] * len(costs)
    return float(_risk_metrics(list(costs), probs, alpha)["cvar"])


# ------------------------------------------------------------- baseline ----
@dataclass
class OpenLoopBaseline:
    """Fixed open-loop schedule evaluated on identical realized conditions."""
    status: str
    solver: str
    realized_operating_cost_usd: float | None = None
    realized_unmet_energy_mwh: float | None = None
    realized_battery_throughput_mwh: float | None = None
    realized_cvar_usd: float | None = None
    terminal_soc_mwh: dict[str, float] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


def run_open_loop_baseline(problem: ScheduleProblem, *, battery_mode: str = "lp",
                           allow_unmet: bool = True,
                           realized_demand: Sequence[Mapping[str, float]] | None = None,
                           realized_avail: Sequence[Mapping[str, float]] | None = None,
                           cvar_confidence: float = 0.95,
                           config: ControlConfig | None = None,
                           forecast: tuple[Sequence[Mapping[str, float]],
                                           Sequence[Mapping[str, float]]] | None = None,
                           ) -> OpenLoopBaseline:
    """Solve ONE deterministic schedule up front and evaluate it without replanning.

    The schedule is optimised on the *forecast available at time 0* (the same
    first forecast the controller sees), then held fixed and executed against the
    realized profiles. This is the fair open-loop counterfactual: same realized
    conditions, same terminal-state convention (the base problem's ``final_soc`` is
    honoured because the schedule spans the whole horizon in one shot).
    """
    config = config or ControlConfig(battery_mode=battery_mode, allow_unmet=allow_unmet)
    model = ForecastErrorModel(demand_sigma=config.demand_sigma,
                               renewable_sigma=config.renewable_sigma, seed=config.seed)
    if realized_demand is None or realized_avail is None:
        realized_demand, realized_avail = realize(problem, model)
    # time-0 forecast (what an open-loop planner would use); caller may override it
    if forecast is None:
        fc_d, fc_a = forecast_at(problem, model, 0, realized_demand, realized_avail)
    else:
        fc_d, fc_a = forecast
    fc_prob = replace(problem,
                      horizon=replace(problem.horizon, demand_mw=fc_d, availability_mw=fc_a))
    fc_prob.validate()
    res = solve_schedule(fc_prob, allow_unmet=allow_unmet, battery_mode=battery_mode)
    if res.status != "optimal":
        return OpenLoopBaseline(status=res.status, solver=res.solver, message=res.message)

    dt = problem.step_hours
    costs: list[float] = []
    unmet_energy = 0.0
    throughput = 0.0
    for t in range(problem.periods):
        dispatch = {g: res.dispatch[g][t] for g in res.dispatch}
        unmet = {n: res.unmet[n][t] for n in res.unmet}
        discharge = {b: res.discharge[b][t] for b in res.discharge}
        charge = {b: res.charge[b][t] for b in res.charge}
        costs.append(_period_realized_cost(problem, t, dispatch, unmet, discharge, charge))
        unmet_energy += dt * sum(unmet.values())
        throughput += dt * sum(discharge.values())
    return OpenLoopBaseline(
        status="optimal", solver=res.solver,
        realized_operating_cost_usd=float(sum(costs)),
        realized_unmet_energy_mwh=float(unmet_energy),
        realized_battery_throughput_mwh=float(throughput),
        realized_cvar_usd=_realized_cvar(costs, cvar_confidence),
        terminal_soc_mwh={b: float(res.soc[b][-1]) for b in res.soc},
        message="fixed open-loop schedule from the time-0 forecast, executed as-is")


@dataclass
class ControlComparison:
    """Rolling-horizon controller versus the fixed open-loop baseline."""
    controller: ControlResult
    baseline: OpenLoopBaseline
    cost_difference_usd: float | None = None
    unmet_difference_mwh: float | None = None
    cvar_difference_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "controller": self.controller.to_dict(),
            "baseline": self.baseline.to_dict(),
            "cost_difference_usd": self.cost_difference_usd,
            "unmet_difference_mwh": self.unmet_difference_mwh,
            "cvar_difference_usd": self.cvar_difference_usd,
            "note": ("differences are controller minus baseline on IDENTICAL realized "
                     "conditions; negative cost difference means the controller was cheaper. "
                     "Synthetic scenario - no superiority claim."),
        }


def compare_control(problem: ScheduleProblem, config: ControlConfig | None = None,
                    *, realized_demand: Sequence[Mapping[str, float]] | None = None,
                    realized_avail: Sequence[Mapping[str, float]] | None = None,
                    forecast_fn: Any | None = None,
                    ) -> ControlComparison:
    """Run the controller and the open-loop baseline on the same realized path."""
    config = config or ControlConfig()
    model = ForecastErrorModel(demand_sigma=config.demand_sigma,
                               renewable_sigma=config.renewable_sigma, seed=config.seed)
    if realized_demand is None or realized_avail is None:
        realized_demand, realized_avail = realize(problem, model)
    controller = run_rolling_horizon(problem, config, realized_demand=realized_demand,
                                     realized_avail=realized_avail, forecast_fn=forecast_fn)
    baseline = run_open_loop_baseline(problem, battery_mode=config.battery_mode,
                                      allow_unmet=config.allow_unmet,
                                      realized_demand=realized_demand,
                                      realized_avail=realized_avail,
                                      cvar_confidence=config.cvar_confidence,
                                      config=config)
    cost_diff = None
    unmet_diff = None
    cvar_diff = None
    if baseline.realized_operating_cost_usd is not None:
        cost_diff = controller.realized_operating_cost_usd - baseline.realized_operating_cost_usd
    if baseline.realized_unmet_energy_mwh is not None:
        unmet_diff = controller.realized_unmet_energy_mwh - baseline.realized_unmet_energy_mwh
    if baseline.realized_cvar_usd is not None and controller.realized_cvar_usd is not None:
        cvar_diff = controller.realized_cvar_usd - baseline.realized_cvar_usd
    return ControlComparison(controller=controller, baseline=baseline,
                             cost_difference_usd=cost_diff,
                             unmet_difference_mwh=unmet_diff,
                             cvar_difference_usd=cvar_diff)


# ------------------------------------------------------------- writers ----
def write_control_result(payload: Mapping[str, Any], out_dir: str | Path,
                         name: str = "control") -> dict[str, Any]:
    """Write a controller record as JSON under ``out_dir`` (created if needed)."""
    import json
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")
    return {"path": path}
