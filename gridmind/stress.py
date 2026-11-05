"""Stage 5C: reproducible stress testing, invariant verification and fair benchmarks.

This module provides three things, all built on the existing GridMind data model
without changing it:

1. **A reproducible stress-scenario framework** (:class:`StressSchedule`,
   :class:`Disturbance`, :func:`apply_stress`) that perturbs a valid
   :class:`~gridmind.multiperiod.ScheduleProblem` with configurable, serializable
   disturbances (demand spikes, renewable shortfalls, generator outages/derating,
   line-capacity reductions, low initial SoC, and combinations), each with explicit
   onset, duration and severity.

2. **Causal forecast/realization separation** (:class:`StressEnvironment`) so that
   at control step ``k`` the rolling-horizon controller only sees disturbances
   whose onset is at or before ``k``. A disturbance that has not yet been revealed
   is present in the *realized* conditions but absent from the *forecast* - a
   controller can therefore never use future realized information.

3. **Invariant verification** (:func:`verify_trajectory`) that re-checks an executed
   controller trajectory against the physical model using the project's independent,
   Pyomo-free validators, reporting machine-readable tolerances. The benchmark
   harness in :mod:`gridmind.stress_bench` compares the deterministic open-loop,
   stochastic risk-neutral, stochastic risk-aware and rolling-horizon methods on
   **identical realized stress conditions** using each method's stored executed
   trace rather than a second validation solve.

Everything here is deterministic given a seed. All data is synthetic.

Design constraints honoured throughout:

* Disturbances never mutate shared objects: every ``apply_*`` returns new
  ``ScheduleProblem``/``Horizon`` instances via ``dataclasses.replace``.
* Unsupported physics is never invented. The network model here has no voltage,
  frequency or inertia physics; outages/derating/line reductions are the only
  supported dispatch-affecting disturbances. Anything else is reported, not faked.
* A failed or infeasible run is recorded explicitly and never treated as a
  zero-cost success.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np

from gridmind.control import (ControlConfig, ControlResult, ControlStep,
                              ForecastErrorModel, _base_forecast,
                              _conditions_sha256, _perturb_availability,
                              _perturb_demand, _problem_sha256,
                              run_rolling_horizon)
from gridmind.scheduling import normalize_battery_mode
from gridmind.multiperiod import Horizon, ScheduleProblem
from gridmind.solver import SolverUnavailableError, resolve_solver
from gridmind.stochastic import RiskConfig, solve_stochastic
from gridmind.uncertainty import (ScenarioSet, UncertaintyScenario, UncertaintyConfig,
                                  generate_scenarios)
from gridmind.validation import DEFAULT_TOL, verify_executed_trajectory

#: version of the Stage 5C stress record schema
STRESS_SCHEMA_VERSION = 1

#: supported disturbance kinds (dispatch-affecting, representable in this model)
DISTURBANCE_KINDS: tuple[str, ...] = (
    "demand_spike",            # multiplicative demand bump at one or more nodes
    "renewable_shortfall",     # multiplicative availability cut for renewables
    "generator_outage",        # generator forced offline
    "generator_derate",        # generator capacity/availability reduced to a fraction
    "line_reduction",          # line capacity reduced to a fraction
    "low_initial_soc",         # battery starts at a low state of charge
)

#: kinds that are *not* representable and must be reported, never simulated
UNSUPPORTED_KINDS: tuple[str, ...] = (
    "frequency_event", "voltage_collapse", "inertia_loss", "islanding_split",
)

#: benchmark method names (fixed labels for reports)
METHOD_NAMES: tuple[str, ...] = (
    "deterministic_open_loop", "stochastic_risk_neutral",
    "stochastic_risk_aware", "stochastic_rolling_risk_neutral",
    "stochastic_rolling_risk_aware", "rolling_horizon",
)


def _round(x: float) -> float:
    return round(float(x), 6)


# =============================================================== disturbances ====
@dataclass(frozen=True)
class Disturbance:
    """One scheduled stress event.

    Parameters
    ----------
    kind:
        One of :data:`DISTURBANCE_KINDS`.
    onset:
        First period (0-based, inclusive) at which the disturbance takes effect.
    duration:
        Number of periods it lasts (>= 1); clamped to the horizon end.
    severity:
        Interpreted per ``kind``: a demand/renewable multiplier, a derating
        fraction in (0, 1], a line-capacity fraction in [0, 1], etc. See
        :func:`apply_stress`.
    target:
        Optional id of the affected element (node for ``demand_spike``, generator
        for renewable/outage/derate, line for ``line_reduction``, battery for
        ``low_initial_soc``). ``None`` means "all applicable elements".
    """

    kind: str
    onset: int = 0
    duration: int = 1
    severity: float = 1.0
    target: str | None = None

    def __post_init__(self) -> None:
        if self.kind in UNSUPPORTED_KINDS:
            raise ValueError(
                f"disturbance kind {self.kind!r} is not representable by this model "
                f"(no frequency/voltage/inertia physics); it is reported, not simulated")
        if self.kind not in DISTURBANCE_KINDS:
            raise ValueError(f"unknown disturbance kind {self.kind!r}; "
                             f"supported: {DISTURBANCE_KINDS}")
        if self.onset < 0:
            raise ValueError("disturbance onset must be >= 0")
        if self.duration < 1:
            raise ValueError("disturbance duration must be >= 1")
        if not math.isfinite(self.severity) or self.severity < 0:
            raise ValueError("disturbance severity must be finite and >= 0")

    def active_periods(self, periods: int) -> range:
        """Periods [onset, min(periods, onset+duration)) this disturbance affects."""
        return range(self.onset, min(periods, self.onset + self.duration))

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Disturbance":
        try:
            return Disturbance(**{k: data[k] for k in
                                  ("kind", "onset", "duration", "severity", "target")
                                  if k in data})
        except TypeError as exc:  # pragma: no cover - defensive
            raise ValueError(f"bad disturbance record: {exc}") from exc


@dataclass(frozen=True)
class StressSchedule:
    """An ordered, serializable set of disturbances with a reproducibility seed."""

    disturbances: tuple[Disturbance, ...] = ()
    seed: int = 20260301
    name: str = "custom"

    def __post_init__(self) -> None:
        ids = [d.kind for d in self.disturbances if d.kind in UNSUPPORTED_KINDS]
        if ids:
            raise ValueError(f"unsupported disturbance kinds: {ids}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "seed": self.seed,
                "disturbances": [d.to_dict() for d in self.disturbances]}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "StressSchedule":
        return StressSchedule(
            disturbances=tuple(Disturbance.from_dict(d) for d in data.get("disturbances", [])),
            seed=int(data.get("seed", 20260301)), name=str(data.get("name", "custom")))

    def max_period(self) -> int:
        return max((d.onset + d.duration for d in self.disturbances), default=0)


# ------------------------------------------------------------- preconditioning --
def low_soc_problem(problem: ScheduleProblem, fraction: float,
                    target: str | None = None) -> ScheduleProblem:
    """Return a copy with each selected battery's initial SoC set *low*.

    ``fraction`` is interpreted as the position within each battery's **usable**
    range ``[min_soc_mwh, capacity_mwh]``: the new initial SoC is
    ``min_soc + fraction * (capacity - min_soc)``. ``fraction = 0`` therefore means
    "as empty as physically allowed", which is always feasible, and ``fraction = 1``
    means full. This avoids ever requesting a state below ``min_soc``.
    """
    if not (0.0 <= fraction <= 1.0):
        raise ValueError("low_initial_soc fraction must lie in [0, 1]")
    batteries = []
    for b in problem.batteries:
        if target is not None and b.id != target:
            batteries.append(b)
            continue
        soc = b.min_soc_mwh + fraction * (b.capacity_mwh - b.min_soc_mwh)
        batteries.append(replace(b, initial_soc_mwh=_round(soc)))
    return replace(problem, batteries=tuple(batteries))


def apply_demand_spike(problem: ScheduleProblem, dist: Disturbance) -> ScheduleProblem:
    nodes = [n.id for n in problem.network.nodes]
    targets = [dist.target] if dist.target else nodes
    for n in targets:
        if n not in nodes:
            raise ValueError(f"demand_spike target node {n!r} not in network")
    active = set(dist.active_periods(problem.periods))
    mult = dist.severity
    rows = []
    for t in range(problem.periods):
        row = dict(problem.horizon.demand_mw[t])
        if t in active:
            for n in targets:
                row[n] = _round(row[n] * mult)
        rows.append(row)
    return replace(problem, horizon=replace(problem.horizon, demand_mw=tuple(rows)))


def apply_renewable_shortfall(problem: ScheduleProblem, dist: Disturbance) -> ScheduleProblem:
    gens = {g.id: g for g in problem.network.generators}
    targets = [dist.target] if dist.target else [g.id for g in problem.network.generators
                                                 if g.renewable]
    for g in targets:
        if g not in gens:
            raise ValueError(f"renewable_shortfall target {g!r} not in network")
        if not gens[g].renewable:
            raise ValueError(f"renewable_shortfall target {g!r} is not a renewable generator")
    active = set(dist.active_periods(problem.periods))
    factor = dist.severity
    rows = []
    for t in range(problem.periods):
        row = dict(problem.horizon.availability_mw[t])
        if t in active:
            for g in targets:
                row[g] = _round(row[g] * factor)
        rows.append(row)
    return replace(problem, horizon=replace(problem.horizon, availability_mw=tuple(rows)))


def apply_generator_outage(problem: ScheduleProblem, dist: Disturbance) -> ScheduleProblem:
    gens = {g.id: g for g in problem.network.generators}
    if dist.target is None:
        raise ValueError("generator_outage requires an explicit target generator id")
    if dist.target not in gens:
        raise ValueError(f"generator_outage target {dist.target!r} not in network")
    active = set(dist.active_periods(problem.periods))
    rows = []
    for t in range(problem.periods):
        row = dict(problem.horizon.generator_online[t])
        if t in active:
            row[dist.target] = False
        rows.append(row)
    return replace(problem, horizon=replace(problem.horizon, generator_online=tuple(rows)))


def apply_generator_derate(problem: ScheduleProblem, dist: Disturbance) -> ScheduleProblem:
    """Reduce a generator's available output to ``severity`` of its nominal value.

    Implemented on the *horizon* availability so the nameplate capacity is never
    mutated; the effective limit becomes ``min(capacity, availability)``.
    """
    gens = {g.id: g for g in problem.network.generators}
    if not (0.0 < dist.severity <= 1.0):
        raise ValueError("generator_derate severity must lie in (0, 1]")
    targets = [dist.target] if dist.target else [g.id for g in problem.network.generators]
    for g in targets:
        if g not in gens:
            raise ValueError(f"generator_derate target {g!r} not in network")
    active = set(dist.active_periods(problem.periods))
    rows = []
    for t in range(problem.periods):
        row = dict(problem.horizon.availability_mw[t])
        if t in active:
            for g in targets:
                nominal = gens[g].capacity_mw if gens[g].availability_mw is None \
                    else max(gens[g].availability_mw, float(problem.horizon.availability_mw[t][g]))
                row[g] = _round(min(row[g], nominal) * dist.severity)
        rows.append(row)
    return replace(problem, horizon=replace(problem.horizon, availability_mw=tuple(rows)))


def apply_line_reduction(problem: ScheduleProblem, dist: Disturbance) -> ScheduleProblem:
    """Reduce line capacity to ``severity`` of nominal (0 = effective outage).

    The Horizon stores only in/out flags, not capacities, so a *partial* reduction
    is applied by replacing the affected line's ``capacity_mw`` on the network - a
    single-line field, never a shared object.
    """
    lines = {l.id: l for l in problem.network.lines}
    if not (0.0 <= dist.severity <= 1.0):
        raise ValueError("line_reduction severity must lie in [0, 1]")
    targets = [dist.target] if dist.target else [l.id for l in problem.network.lines]
    for l in targets:
        if l not in lines:
            raise ValueError(f"line_reduction target {l!r} not in network")
    new_lines = []
    for l in problem.network.lines:
        if l.id in targets:
            new_lines.append(replace(l, capacity_mw=_round(l.capacity_mw * dist.severity)))
        else:
            new_lines.append(l)
    # For a *full* removal (severity 0) also flip the in-service flag per period so
    # flows are exactly zero (capacity 0 already implies this; kept for clarity).
    if dist.severity == 0.0:
        active = set(dist.active_periods(problem.periods))
        rows = []
        for t in range(problem.periods):
            row = dict(problem.horizon.line_in_service[t])
            if t in active:
                for l in targets:
                    row[l] = False
            rows.append(row)
        horizon = replace(problem.horizon, line_in_service=tuple(rows))
    else:
        horizon = problem.horizon
    new_net = replace(problem.network, lines=tuple(new_lines))
    out = replace(problem, network=new_net, horizon=horizon)
    return out


_APPLIERS = {
    "demand_spike": apply_demand_spike,
    "renewable_shortfall": apply_renewable_shortfall,
    "generator_outage": apply_generator_outage,
    "generator_derate": apply_generator_derate,
    "line_reduction": apply_line_reduction,
}


def apply_stress(problem: ScheduleProblem, schedule: StressSchedule) -> ScheduleProblem:
    """Apply every disturbance in ``schedule`` to ``problem`` (immutably).

    Raises ``ValueError`` (never a silent no-op) if a disturbance cannot be
    represented. The returned problem is validated, so an infeasible-by-
    construction stress schedule is caught before any solve.
    """
    perturbed = problem
    for dist in schedule.disturbances:
        if dist.kind == "low_initial_soc":
            try:
                perturbed = low_soc_problem(perturbed, dist.severity, dist.target)
            except ValueError as exc:
                raise ValueError(f"low_initial_soc: {exc}") from exc
        else:
            reducer = _APPLIERS[dist.kind]
            perturbed = reducer(perturbed, dist)
    perturbed.validate()
    return perturbed


# ------------------------------------------------------- stress environment -----
@dataclass
class StressEnvironment:
    """Causal forecast/realization split for a stressed problem.

    The environment holds a **ground-truth nominal problem** (all disturbances
    applied) and reveals, at each control step ``k``, only the disturbances whose
    onset is at or before ``k``. The forecast the controller sees therefore never
    contains an as-yet-unrevealed disturbance; the realized conditions always
    contain every disturbance (they are physical reality).

    Both forecast and realization are then passed through a
    :class:`~gridmind.control.ForecastErrorModel` so measurement noise is also
    present, exactly as in Stage 5B.
    """

    base_problem: ScheduleProblem
    stress: StressSchedule
    error_model: ForecastErrorModel

    def __post_init__(self) -> None:
        n = self.base_problem.periods
        for d in self.stress.disturbances:
            if d.onset >= n:
                raise ValueError(
                    f"disturbance onset {d.onset} is at/after the horizon end {n}")

    @property
    def truth_problem(self) -> ScheduleProblem:
        """The fully disturbed physical problem (all disturbances realized)."""
        return apply_stress(self.base_problem, self.stress)

    def revealed_problem(self, k: int) -> ScheduleProblem:
        """The problem with only the disturbances revealed by step ``k`` applied."""
        revealed = StressSchedule(
            disturbances=tuple(d for d in self.stress.disturbances if d.onset <= k),
            seed=self.stress.seed, name=self.stress.name + f"_revealed_{k}")
        return apply_stress(self.base_problem, revealed)

    # -- forecast / realization for a single control step k ------------------
    def forecast_at(self, k: int) -> tuple[tuple[Mapping[str, float], ...],
                                           tuple[Mapping[str, float], ...]]:
        """Causal forecast as seen at step ``k``.

        Built from the *revealed* problem's base forecast, with observed realized
        past substituted in. Disturbances after ``k`` are absent, so no future
        realized information leaks in.
        """
        revealed = self.revealed_problem(k)
        base_d, base_a = _base_forecast(revealed, self.error_model)
        real_d, real_a = self.realized()
        periods = self.base_problem.periods
        d_out, a_out = [], []
        for t in range(periods):
            if t < k:
                d_out.append(dict(real_d[t]))
                a_out.append(dict(real_a[t]))
            else:
                d_out.append(dict(base_d[t]))
                a_out.append(dict(base_a[t]))
        return tuple(d_out), tuple(a_out)

    def realized(self) -> tuple[tuple[Mapping[str, float], ...],
                                tuple[Mapping[str, float], ...]]:
        """The single realized truth: full-stress problem + a fixed error draw."""
        truth = self.truth_problem
        base_d, base_a = _base_forecast(truth, self.error_model)
        rng = self.error_model._rng(0)
        demand = _perturb_demand(base_d, truth, rng, self.error_model.demand_sigma)
        avail = _perturb_availability(base_a, truth, rng, self.error_model.renewable_sigma)
        return demand, avail

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": STRESS_SCHEMA_VERSION,
                "stress": self.stress.to_dict(),
                "error_model": self.error_model.to_dict()}


# =========================================================== verification =====
def verify_trajectory(problem: ScheduleProblem, control_result: Any,
                      realized_demand: Sequence[Mapping[str, float]],
                      realized_avail: Sequence[Mapping[str, float]],
                      *, allow_unmet: bool = True, tol: float = DEFAULT_TOL,
                      battery_mode: str = "lp") -> dict[str, Any]:
    """Verify a rolling-horizon :class:`ControlResult` against realized conditions.

    Reconstructs the executed per-period arrays from the recorded steps and
    re-checks them with the independent validator. Non-executed periods (only
    possible after an ``abort``) are never treated as success: the report records
    ``periods_executed`` and ``periods_expected`` explicitly. Returns a
    machine-readable dict: ``passed``, the tolerance used, per-check results and a
    ``coverage`` flag (true only when every period was executed).
    """
    periods = problem.periods
    steps = control_result.steps
    gen_ids = [g.id for g in problem.network.generators]
    node_ids = [n.id for n in problem.network.nodes]
    line_ids = [l.id for l in problem.network.lines]
    batt_ids = [b.id for b in problem.batteries]
    dispatch = {g: [0.0] * periods for g in gen_ids}
    flows = {l: [0.0] * periods for l in line_ids}
    unmet = {n: [0.0] * periods for n in node_ids}
    charge = {b: [0.0] * periods for b in batt_ids}
    discharge = {b: [0.0] * periods for b in batt_ids}
    soc = {b: [problem.battery(b).initial_soc_mwh] * periods for b in batt_ids}
    prev_soc = {b: problem.battery(b).initial_soc_mwh for b in batt_ids}
    for step in steps:
        t = step.period
        if t >= periods:
            continue
        ex = step.executed or {}
        for g, v in (ex.get("dispatch") or {}).items():
            dispatch[g][t] = float(v)
        for l, v in (ex.get("flows") or {}).items():
            flows[l][t] = float(v)
        for n, v in (ex.get("unmet") or {}).items():
            unmet[n][t] = float(v)
        for b, v in (ex.get("charge") or {}).items():
            charge[b][t] = float(v)
        for b, v in (ex.get("discharge") or {}).items():
            discharge[b][t] = float(v)
        for b, v in (step.battery_soc_mwh or {}).items():
            soc[b][t] = float(v)
            prev_soc[b] = float(v)
    executed = len(steps)
    report = verify_executed_trajectory(
        problem, realized_demand, realized_avail,
        dispatch=dispatch, flows=flows, unmet=unmet, charge=charge, discharge=discharge,
        soc=soc, allow_unmet=allow_unmet, tol=tol, battery_mode=battery_mode)
    out = report.to_dict()
    out["tolerance"] = float(tol)
    out["tolerance_units"] = "MW (absolute), objective relative 1e-6"
    out["periods_executed"] = executed
    out["periods_expected"] = periods
    out["coverage_complete"] = executed == periods
    out["passed"] = bool(report.passed and executed == periods)
    return out


# =========================================================== method runners ==
def _realized_cost_components(problem: ScheduleProblem, t: int, dispatch, unmet, charge,
                               discharge) -> dict[str, float]:
    net, dt = problem.network, problem.step_hours
    gen = dt * sum(net.generator(g).cost_per_mwh * v for g, v in (dispatch or {}).items())
    pen = dt * net.unmet_penalty_per_mwh * sum((unmet or {}).values())
    thr = dt * sum(problem.battery(b).throughput_cost_per_mwh * v for b, v in (discharge or {}).items())
    return {"generation_cost_usd": gen, "unmet_penalty_cost_usd": pen,
            "battery_throughput_cost_usd": thr, "operating_cost_usd": gen + pen + thr}


@dataclass
class MethodResult:
    """Outcome of one method on one realized stress scenario.

    ``solver_status`` records what the optimiser returned. ``policy_status`` records
    what the executed policy actually achieved. ``status`` is the compatibility
    aggregate used by reports and is always the policy status, so a solver-optimal
    but physically infeasible execution is never presented as an optimal result.
    """
    method: str
    status: str
    solver_status: str = ""
    policy_status: str = ""
    validation_status: str = "skipped"
    feasible_for_comparison: bool = False
    exclusion_reason: str | None = None
    policy_label: str = ""
    is_oracle: bool = False
    solver: str = ""
    realized_operating_cost_usd: float | None = None
    generation_cost_usd: float | None = None
    unmet_penalty_cost_usd: float | None = None
    battery_throughput_cost_usd: float | None = None
    unmet_energy_mwh: float | None = None
    unmet_fraction: float | None = None       # unmet_energy / total_realized_demand
    battery_throughput_mwh: float | None = None
    terminal_soc_deviation_mwh: float | None = None  # max |final - declared final_soc|, or None
    solve_seconds: float | None = None
    validation_report: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    trace_metrics_match: bool | None = None
    metric_discrepancies: list[str] = field(default_factory=list)
    periods_expected: int = 0
    periods_executed: int = 0
    realized_conditions_sha256: str = ""
    realized_demand_mw: list[dict[str, float]] = field(default_factory=list)
    realized_availability_mw: list[dict[str, float]] = field(default_factory=list)
    problem_sha256: str = ""
    forecast_sha256: str = ""
    config_sha256: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    initial_soc_mwh: dict[str, float] = field(default_factory=dict)
    initial_dispatch_mw: dict[str, float] = field(default_factory=dict)
    first_stage: dict[str, Any] = field(default_factory=dict)
    failed_replans: int = 0
    message: str = ""

    @property
    def validation(self) -> dict[str, Any] | None:
        """Backward-compatible alias for :attr:`validation_report`."""
        return self.validation_report

    @property
    def ok(self) -> bool:
        return self.feasible_for_comparison

    @property
    def included_for_comparison(self) -> bool:
        return self.feasible_for_comparison

    @property
    def executed_trace(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for :attr:`trace`."""
        return self.trace

    def to_dict(self) -> dict[str, Any]:
        out = vars(self).copy()
        out["validation"] = self.validation_report
        out["included_for_comparison"] = self.feasible_for_comparison
        out["executed_trace"] = self.trace
        return out


def _config_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=float)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _terminal_soc_deviation(problem: ScheduleProblem, terminal_soc: Mapping[str, float] | None
                            ) -> float | None:
    if terminal_soc is None:
        return None
    worst = 0.0
    any_declared = False
    for b in problem.batteries:
        if b.final_soc_mwh is None:
            continue
        any_declared = True
        worst = max(worst, abs(float(terminal_soc.get(b.id, b.final_soc_mwh)) - b.final_soc_mwh))
    return worst if any_declared else None


def _trace_row(problem: ScheduleProblem, t: int,
               realized_d: Sequence[Mapping[str, float]],
               realized_a: Sequence[Mapping[str, float]],
               soc_before: Mapping[str, float], executed: Mapping[str, Any],
               soc_after: Mapping[str, float], *,
               committed: Mapping[str, Any] | None = None,
               status: str = "optimal",
               window: tuple[int, int] | None = None,
               operating_cost_usd: float | None = None) -> dict[str, Any]:
    """Build one serializable row of the *actually executed* trajectory."""
    ex = executed or {}
    row: dict[str, Any] = {
        "period": int(t),
        "window": list(window or (t, min(problem.periods, t + 1))),
        "status": status,
        "realized_demand_mw": {k: float(v) for k, v in dict(realized_d[t]).items()},
        "realized_availability_mw": {k: float(v) for k, v in dict(realized_a[t]).items()},
        "initial_soc_mwh": {b.id: float(soc_before[b.id]) for b in problem.batteries},
        "executed": {
            "dispatch": {k: float(v) for k, v in (ex.get("dispatch") or {}).items()},
            "flows": {k: float(v) for k, v in (ex.get("flows") or {}).items()},
            "charge": {k: float(v) for k, v in (ex.get("charge") or {}).items()},
            "discharge": {k: float(v) for k, v in (ex.get("discharge") or {}).items()},
            "unmet": {k: float(v) for k, v in (ex.get("unmet") or {}).items()},
        },
        "battery_soc_mwh": {b.id: float(soc_after[b.id]) for b in problem.batteries},
    }
    if committed is not None:
        row["committed"] = {
            "charge": {k: float(v) for k, v in (committed.get("charge") or {}).items()},
            "discharge": {k: float(v) for k, v in (committed.get("discharge") or {}).items()},
        }
    if operating_cost_usd is not None:
        row["operating_cost_usd"] = float(operating_cost_usd)
    return row


def _trace_to_control_result(problem: ScheduleProblem, result: MethodResult,
                             battery_mode: str) -> ControlResult:
    steps = [
        ControlStep(
            period=int(row["period"]),
            window=tuple(row.get("window") or (row["period"], row["period"] + 1)),
            status=str(row.get("status", "completed")),
            executed={
                "dispatch": dict((row.get("executed") or {}).get("dispatch") or {}),
                "flows": dict((row.get("executed") or {}).get("flows") or {}),
                "charge": dict((row.get("executed") or {}).get("charge") or {}),
                "discharge": dict((row.get("executed") or {}).get("discharge") or {}),
                "unmet": dict((row.get("executed") or {}).get("unmet") or {}),
            },
            battery_soc_mwh=dict(row.get("battery_soc_mwh") or {}),
        )
        for row in result.trace
    ]
    return ControlResult(
        status="completed", mode="stored_trace", failure_policy="hold",
        lookahead=problem.periods, battery_mode=battery_mode,
        solver=result.solver, periods_executed=len(steps), steps=steps)


def _trace_totals(problem: ScheduleProblem,
                 trace: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    dt = problem.step_hours
    gen = pen = thr = unmet_energy = throughput = 0.0
    for row in trace:
        ex = row.get("executed") or {}
        dispatch = ex.get("dispatch") or {}
        unmet = ex.get("unmet") or {}
        charge = ex.get("charge") or {}
        discharge = ex.get("discharge") or {}
        comp = _realized_cost_components(problem, int(row["period"]), dispatch, unmet,
                                         charge, discharge)
        gen += comp["generation_cost_usd"]
        pen += comp["unmet_penalty_cost_usd"]
        thr += comp["battery_throughput_cost_usd"]
        unmet_energy += dt * sum(float(v) for v in unmet.values())
        throughput += dt * sum(float(v) for v in charge.values())
        throughput += dt * sum(float(v) for v in discharge.values())
    return {"operating_cost_usd": gen + pen + thr,
            "generation_cost_usd": gen,
            "unmet_penalty_cost_usd": pen,
            "battery_throughput_cost_usd": thr,
            "unmet_energy_mwh": unmet_energy,
            "battery_throughput_mwh": throughput}


def _metric_close(a: float | None, b: float | None, tol: float) -> bool:
    if a is None or b is None:
        return a is b
    scale = max(1.0, abs(float(a)), abs(float(b)))
    return abs(float(a) - float(b)) <= max(float(tol), 1e-6 * scale)


def _finalize_method_result(result: MethodResult, problem: ScheduleProblem,
                            realized_d: Sequence[Mapping[str, float]],
                            realized_a: Sequence[Mapping[str, float]], *,
                            allow_unmet: bool, tol: float,
                            battery_mode: str) -> MethodResult:
    """Attach provenance and validate the stored trace without re-solving.

    This is the integrity boundary: benchmark inclusion is decided from the trace
    the method actually produced, never from a fresh solve of the same problem.
    """
    battery_mode = normalize_battery_mode(battery_mode)
    result.periods_expected = problem.periods
    result.periods_executed = len(result.trace)
    result.realized_demand_mw = [dict(row) for row in realized_d]
    result.realized_availability_mw = [dict(row) for row in realized_a]
    result.realized_conditions_sha256 = _conditions_sha256(realized_d, realized_a)
    result.problem_sha256 = _problem_sha256(problem)
    result.initial_soc_mwh = {b.id: float(b.initial_soc_mwh) for b in problem.batteries}
    result.initial_dispatch_mw = {g: float(v) for g, v in problem.initial_dispatch_mw.items()}
    result.config_sha256 = _config_sha256(result.config)
    if not result.solver_status:
        result.solver_status = result.status
    if not result.policy_status:
        result.policy_status = result.status

    if result.solver_status != "optimal":
        result.validation_status = "skipped"
        result.feasible_for_comparison = False
        result.exclusion_reason = result.exclusion_reason or (
            result.solver_status if result.solver_status.startswith("solver_")
            else f"solver_{result.solver_status}")
        result.status = result.policy_status
        return result

    if not result.trace:
        result.validation_status = "missing_trace"
        result.feasible_for_comparison = False
        result.policy_status = "incomplete"
        result.exclusion_reason = "missing_executed_trace"
        result.status = result.policy_status
        return result

    discrepancies: list[str] = []
    rows_d: list[dict[str, float]] = []
    rows_a: list[dict[str, float]] = []
    malformed: list[int] = []
    for i, row in enumerate(result.trace):
        if int(row.get("period", -1)) != i:
            malformed.append(i)
        if "realized_demand_mw" not in row or "realized_availability_mw" not in row:
            malformed.append(i)
            rows_d.append({})
            rows_a.append({})
            continue
        rows_d.append(dict(row["realized_demand_mw"]))
        rows_a.append(dict(row["realized_availability_mw"]))

    if malformed:
        result.validation_status = "failed"
        result.feasible_for_comparison = False
        result.policy_status = "policy_infeasible"
        result.exclusion_reason = f"malformed_executed_trace: periods {malformed}"
        result.metric_discrepancies = [result.exclusion_reason]
        result.status = result.policy_status
        return result

    trace_hash = _conditions_sha256(rows_d, rows_a)
    if trace_hash != result.realized_conditions_sha256:
        discrepancies.append("trace realized inputs do not match recorded realized inputs")

    complete = len(result.trace) == problem.periods
    if complete:
        control_result = _trace_to_control_result(problem, result, battery_mode)
        verification = verify_trajectory(
            problem, control_result, tuple(rows_d), tuple(rows_a),
            allow_unmet=allow_unmet, tol=tol, battery_mode=battery_mode)
        mode_feasible = bool(
            verification.get("physical_feasible") is not False
            and verification.get("model_valid") is not False
            and (battery_mode != "exclusive"
                 or verification.get("battery_exclusive") is not False)
            and verification.get("coverage_complete"))
    else:
        verification = {
            "passed": False,
            "physical_feasible": None,
            "model_valid": None,
            "battery_exclusive": None,
            "checks": [],
            "periods_executed": len(result.trace),
            "periods_expected": problem.periods,
            "coverage_complete": False,
        }
        mode_feasible = False
    verification["passed_for_battery_mode"] = bool(mode_feasible)
    verification["trace_sha256"] = trace_hash

    totals = _trace_totals(problem, result.trace)
    checks = (
        ("realized_operating_cost_usd", result.realized_operating_cost_usd,
         totals["operating_cost_usd"]),
        ("generation_cost_usd", result.generation_cost_usd, totals["generation_cost_usd"]),
        ("unmet_penalty_cost_usd", result.unmet_penalty_cost_usd,
         totals["unmet_penalty_cost_usd"]),
        ("battery_throughput_cost_usd", result.battery_throughput_cost_usd,
         totals["battery_throughput_cost_usd"]),
        ("unmet_energy_mwh", result.unmet_energy_mwh, totals["unmet_energy_mwh"]),
        ("battery_throughput_mwh", result.battery_throughput_mwh,
         totals["battery_throughput_mwh"]),
    )
    for name, reported, recomputed in checks:
        if not _metric_close(reported, recomputed, tol):
            discrepancies.append(f"{name}: reported {reported!r}, trace {recomputed!r}")

    terminal = dict(result.trace[-1].get("battery_soc_mwh") or {})
    trace_terminal = _terminal_soc_deviation(problem, terminal)
    if not _metric_close(result.terminal_soc_deviation_mwh, trace_terminal, tol):
        discrepancies.append(
            "terminal_soc_deviation_mwh: reported "
            f"{result.terminal_soc_deviation_mwh!r}, trace {trace_terminal!r}")

    result.trace_metrics_match = not discrepancies
    result.metric_discrepancies = discrepancies
    verification["trace_metrics_match"] = not discrepancies
    verification["metric_discrepancies"] = list(discrepancies)
    result.validation_report = verification

    passed = bool(complete and mode_feasible and not discrepancies)
    result.validation_status = "passed" if passed else (
        "incomplete" if not complete else "failed")
    if not passed:
        failed_checks = [c.get("name", "") for c in verification.get("checks", [])
                         if not c.get("passed", False)]
        if not complete:
            result.exclusion_reason = ("incomplete_executed_trace: "
                                       f"{len(result.trace)}/{problem.periods} periods")
        elif result.terminal_soc_deviation_mwh is not None \
                and result.terminal_soc_deviation_mwh > tol:
            result.exclusion_reason = "terminal_soc_violation"
        elif discrepancies:
            result.exclusion_reason = "trace_metrics_mismatch: " + "; ".join(discrepancies)
        else:
            relevant = [c for c in failed_checks
                        if c != "simultaneous_charge_discharge" or battery_mode == "exclusive"]
            result.exclusion_reason = "physical_validation_failed: " + ", ".join(
                relevant or failed_checks)
        result.policy_status = "incomplete" if not complete else "policy_infeasible"
    result.feasible_for_comparison = passed
    result.status = result.policy_status
    return result


def _run_method_controller(env: StressEnvironment, config: ControlConfig, *,
                           method: str, policy_label: str,
                           tol: float) -> MethodResult:
    """Run a causal controller and validate its stored executed trajectory."""
    truth = env.truth_problem
    realized_d, realized_a = env.realized()
    res = run_rolling_horizon(truth, config, realized_demand=realized_d,
                              realized_avail=realized_a, forecast_fn=env.forecast_at)
    recovered = [step for step in res.steps if getattr(step, "failure_recovered", False)]
    solved = [step for step in res.steps if not getattr(step, "failure_recovered", False)]
    if recovered:
        solver_status = "mixed" if solved else "failed"
    elif res.status in ("completed", "completed_with_recovery"):
        solver_status = ("optimal" if all(step.status == "optimal" for step in res.steps)
                         else "partial")
    else:
        solver_status = res.status
    trace: list[dict[str, Any]] = []
    soc_before = {b.id: float(b.initial_soc_mwh) for b in truth.batteries}
    for step in res.steps:
        soc_after = {b.id: float((step.battery_soc_mwh or {}).get(
            b.id, soc_before[b.id])) for b in truth.batteries}
        trace.append(_trace_row(
            truth, step.period, realized_d, realized_a, soc_before,
            step.executed or {}, soc_after,
            committed=step.planned_executed or {},
            status=step.status, window=tuple(step.window),
            operating_cost_usd=step.realized_cost_usd))
        soc_before = soc_after
    components = _components_from_steps(truth, res)
    terminal = dict(soc_before) if res.steps else None
    total_demand = truth.total_demand_mwh
    unmet_energy = res.realized_unmet_energy_mwh
    forecast_d, forecast_a = env.forecast_at(0)
    result = MethodResult(
        method=method, status=res.status, solver_status=solver_status,
        policy_status=res.status, solver=res.solver, policy_label=policy_label,
        realized_operating_cost_usd=res.realized_operating_cost_usd,
        generation_cost_usd=components["generation_cost_usd"],
        unmet_penalty_cost_usd=components["unmet_penalty_cost_usd"],
        battery_throughput_cost_usd=components["battery_throughput_cost_usd"],
        unmet_energy_mwh=unmet_energy,
        unmet_fraction=(unmet_energy / total_demand) if total_demand > 0 else 0.0,
        battery_throughput_mwh=res.realized_battery_throughput_mwh,
        terminal_soc_deviation_mwh=_terminal_soc_deviation(truth, terminal),
        solve_seconds=res.mean_solve_seconds,
        trace=trace, periods_executed=len(trace),
        forecast_sha256=_conditions_sha256(forecast_d, forecast_a),
        realized_conditions_sha256=res.realized_conditions_sha256,
        realized_demand_mw=res.realized_demand_mw,
        realized_availability_mw=res.realized_availability_mw,
        problem_sha256=res.problem_sha256,
        config=vars(config).copy(), initial_soc_mwh=res.initial_soc_mwh,
        initial_dispatch_mw=res.initial_dispatch_mw,
        failed_replans=res.failed_replans, message=res.message)
    return _finalize_method_result(
        result, truth, realized_d, realized_a, allow_unmet=config.allow_unmet,
        tol=tol, battery_mode=config.battery_mode)


def run_method_rolling_horizon(env: StressEnvironment, config: ControlConfig,
                               *, tol: float = DEFAULT_TOL) -> MethodResult:
    """Deterministic causal rolling-horizon controller under a stress forecast."""
    return _run_method_controller(
        env, config, method="rolling_horizon",
        policy_label="causal_rolling_horizon_realized_redispatch", tol=tol)


def run_method_stochastic_rolling_horizon(
        env: StressEnvironment, *, risk_weight: float, cvar_alpha: float,
        lead_periods: int, n_scenarios: int, lookahead: int,
        battery_mode: str = "lp", allow_unmet: bool = True,
        demand_sigma: float = 0.06, renewable_sigma: float = 0.12,
        seed: int = 20260201, tol: float = DEFAULT_TOL) -> MethodResult:
    """Causal receding-horizon stochastic-commitment hybrid.

    At each period the controller solves a two-stage stochastic window around the
    causal forecast, commits only the current-period battery action, re-dispatches
    generation at the realized current conditions, executes exactly that plan, and
    advances the state before replanning. It is not a scenario-tree multistage
    policy and makes no full-stochastic-optimality claim.
    """
    method = ("stochastic_rolling_risk_neutral" if risk_weight == 0.0
              else "stochastic_rolling_risk_aware")
    config = ControlConfig(
        lookahead=lookahead, mode="stochastic", battery_mode=battery_mode,
        allow_unmet=allow_unmet, n_scenarios=n_scenarios, risk_weight=risk_weight,
        cvar_alpha=cvar_alpha, lead_periods=lead_periods,
        demand_sigma=demand_sigma, renewable_sigma=renewable_sigma, seed=seed)
    return _run_method_controller(
        env, config, method=method,
        policy_label="causal_receding_horizon_stochastic_commitment_no_scenario_tree",
        tol=tol)


def _components_from_steps(problem: ScheduleProblem, res: Any) -> dict[str, float]:
    gen = pen = thr = 0.0
    dt = problem.step_hours
    for step in res.steps:
        ex = step.executed or {}
        for g, v in (ex.get("dispatch") or {}).items():
            gen += dt * problem.network.generator(g).cost_per_mwh * float(v)
        for n, v in (ex.get("unmet") or {}).items():
            pen += dt * problem.network.unmet_penalty_per_mwh * float(v)
        for b, v in (ex.get("discharge") or {}).items():
            thr += dt * problem.battery(b).throughput_cost_per_mwh * float(v)
    return {"generation_cost_usd": gen, "unmet_penalty_cost_usd": pen,
            "battery_throughput_cost_usd": thr, "operating_cost_usd": gen + pen + thr}


def run_method_open_loop(env: StressEnvironment, *, battery_mode: str = "lp",
                         allow_unmet: bool = True, tol: float = DEFAULT_TOL,
                         perfect_foresight: bool = False) -> MethodResult:
    """Deterministic open-loop schedule, or a perfect-foresight ORACLE if requested.

    The operationally-fair open-loop schedule is optimised on the **time-0 causal
    forecast** and then executed unchanged against the realized conditions. With
    ``perfect_foresight=True`` the schedule is instead optimised on the *realized*
    conditions - this is an **oracle** (clairvoyant) and is labelled as such; it is
    never a fair competitor.
    """
    truth = env.truth_problem
    realized_d, realized_a = env.realized()
    from gridmind.control import _realized_redispatch
    from gridmind.scheduling import solve_schedule
    fc_d, fc_a = env.forecast_at(0)
    if perfect_foresight:
        plan_problem = replace(truth, horizon=replace(
            truth.horizon, demand_mw=tuple(dict(realized_d[t]) for t in range(truth.periods)),
            availability_mw=tuple(dict(realized_a[t]) for t in range(truth.periods))))
        method = "oracle_perfect_foresight"
    else:
        plan_problem = replace(truth, horizon=replace(
            truth.horizon, demand_mw=fc_d, availability_mw=fc_a))
        method = "deterministic_open_loop"
    plan_problem.validate()
    res = solve_schedule(plan_problem, allow_unmet=allow_unmet, battery_mode=battery_mode)
    if res.status != "optimal":
        result = MethodResult(
            method=method, status=res.status, solver_status=res.status,
            policy_status=res.status, is_oracle=perfect_foresight, solver=res.solver,
            policy_label=("perfect_foresight_oracle" if perfect_foresight
                          else "open_loop_time0_forecast_then_realized_redispatch"),
            solve_seconds=res.runtime_seconds, message=res.message,
            config={"battery_mode": battery_mode, "allow_unmet": allow_unmet,
                    "perfect_foresight": perfect_foresight})
        return _finalize_method_result(
            result, truth, realized_d, realized_a, allow_unmet=allow_unmet,
            tol=tol, battery_mode=battery_mode)
    dt = truth.step_hours
    gen = pen = thr = 0.0
    unmet_energy = 0.0
    throughput_total = 0.0
    soc_now = {b.id: float(b.initial_soc_mwh) for b in truth.batteries}
    init_dispatch: dict[str, float] = {
        g: float(v) for g, v in truth.initial_dispatch_mw.items()}
    committed_charge = {b: [float(res.charge[b][t]) for t in range(truth.periods)]
                        for b in res.charge}
    committed_discharge = {b: [float(res.discharge[b][t]) for t in range(truth.periods)]
                           for b in res.discharge}
    batt_ids = [b.id for b in truth.batteries]
    trace: list[dict[str, Any]] = []
    for t in range(truth.periods):
        c = {b: committed_charge[b][t] for b in batt_ids}
        d = {b: committed_discharge[b][t] for b in batt_ids}
        soc_before = dict(soc_now)
        plan = _realized_redispatch(truth, t, realized_d, realized_a, soc_now,
                                    charge=c, discharge=d, init_dispatch=init_dispatch,
                                    battery_mode=battery_mode)
        exec_c = {b: float((plan.get("charge") or {}).get(b, c[b])) for b in batt_ids}
        exec_d = {b: float((plan.get("discharge") or {}).get(b, d[b])) for b in batt_ids}
        comp = _realized_cost_components(truth, t, plan.get("dispatch") or {}, plan.get("unmet") or {}, exec_c, exec_d)
        gen += comp["generation_cost_usd"]
        pen += comp["unmet_penalty_cost_usd"]
        thr += comp["battery_throughput_cost_usd"]
        unmet_energy += dt * sum((plan.get("unmet") or {}).values())
        throughput_total += dt * sum(exec_c[b] + exec_d[b] for b in batt_ids)
        soc_now = _advance(truth, soc_now, exec_c, exec_d, t, battery_mode=battery_mode)
        trace.append(_trace_row(
            truth, t, realized_d, realized_a, soc_before,
            {"dispatch": plan.get("dispatch") or {}, "flows": plan.get("flows") or {},
             "charge": exec_c, "discharge": exec_d, "unmet": plan.get("unmet") or {}},
            soc_now, committed={"charge": c, "discharge": d},
            window=(t, min(truth.periods, t + 1)),
            operating_cost_usd=comp["operating_cost_usd"]))
        init_dispatch = {g: float(v) for g, v in (plan.get("dispatch") or {}).items()}
    terminal = dict(soc_now)
    total_demand = truth.total_demand_mwh
    result = MethodResult(
        method=method, status="optimal", solver_status="optimal",
        policy_status="optimal", is_oracle=perfect_foresight, solver=res.solver,
        policy_label=("perfect_foresight_oracle" if perfect_foresight
                      else "open_loop_time0_forecast_then_realized_redispatch"),
        realized_operating_cost_usd=gen + pen + thr,
        generation_cost_usd=gen, unmet_penalty_cost_usd=pen, battery_throughput_cost_usd=thr,
        unmet_energy_mwh=unmet_energy,
        unmet_fraction=(unmet_energy / total_demand) if total_demand > 0 else 0.0,
        battery_throughput_mwh=throughput_total,
        terminal_soc_deviation_mwh=_terminal_soc_deviation(truth, terminal),
        solve_seconds=res.runtime_seconds, trace=trace,
        periods_executed=len(trace),
        forecast_sha256=_conditions_sha256(fc_d, fc_a),
        config={"battery_mode": battery_mode, "allow_unmet": allow_unmet,
                "perfect_foresight": perfect_foresight},
        initial_soc_mwh={b.id: float(b.initial_soc_mwh) for b in truth.batteries},
        initial_dispatch_mw={g: float(v) for g, v in truth.initial_dispatch_mw.items()})
    return _finalize_method_result(
        result, truth, realized_d, realized_a, allow_unmet=allow_unmet,
        tol=tol, battery_mode=battery_mode)


def run_method_stochastic(env: StressEnvironment, *, risk_weight: float, cvar_alpha: float,
                          lead_periods: int, n_scenarios: int, battery_mode: str = "lp",
                          allow_unmet: bool = True, tol: float = DEFAULT_TOL) -> MethodResult:
    """Committed full-horizon stochastic battery policy on the causal time-0 forecast.

    Scenarios are generated around the time-0 forecast (causal). The stochastic
    model commits the **battery schedule over the entire horizon** (the shared
    first-stage battery decisions span all periods), while generation, flows and
    unserved energy remain scenario recourse. The resulting full-horizon battery
    schedule is then executed unchanged against the realized stress conditions,
    with generation/network decisions re-dispatched each period. This is a
    *committed open-loop battery policy*, NOT an adaptive recourse or scenario-tree
    policy.

    ``lead_periods`` is recorded as the requested commitment window for provenance.
    The open-loop contract intentionally overrides it with the full horizon: the
    optimizer's terminal-SoC constraint applies to the whole trajectory, so
    executing only a short first-stage prefix (or zeroing later actions) would not
    match the optimized battery trajectory. Rolling-horizon stochastic methods keep
    their causal, short-window ``lead_periods`` semantics.
    """
    if lead_periods is None or lead_periods < 0:
        raise ValueError("lead_periods must be >= 0")
    truth = env.truth_problem
    if lead_periods > truth.periods:
        raise ValueError(
            f"lead_periods ({lead_periods}) exceeds periods ({truth.periods})")
    realized_d, realized_a = env.realized()
    fc_d, fc_a = env.forecast_at(0)
    forecast_problem = replace(truth, horizon=replace(
        truth.horizon, demand_mw=fc_d, availability_mw=fc_a))
    forecast_problem.validate()
    requested_lead_periods = int(lead_periods)
    committed_battery_periods = truth.periods
    risk = RiskConfig(risk_weight=risk_weight, cvar_alpha=cvar_alpha,
                      lead_periods=committed_battery_periods)
    name = "stochastic_risk_neutral" if risk_weight == 0.0 else "stochastic_risk_aware"
    try:
        ucfg = UncertaintyConfig(n_scenarios=n_scenarios, seed=env.stress.seed + 1,
                                 demand_sigma=env.error_model.demand_sigma,
                                 renewable_sigma=env.error_model.renewable_sigma)
        scenario_set = generate_scenarios(forecast_problem, ucfg)
        res = solve_stochastic(forecast_problem, scenario_set, risk=risk,
                               allow_unmet=allow_unmet, battery_mode=battery_mode)
    except SolverUnavailableError as exc:
        result = MethodResult(
            method=name, status="solver_unavailable", solver_status="solver_unavailable",
            policy_status="solver_unavailable",
            policy_label="committed_full_horizon_open_loop_battery",
            message=str(exc))
        return _finalize_method_result(
            result, truth, realized_d, realized_a, allow_unmet=allow_unmet,
            tol=tol, battery_mode=battery_mode)
    if res.status != "optimal" or not res.first_stage:
        result = MethodResult(
            method=name, status=res.status, solver_status=res.status,
            policy_status=res.status,
            policy_label="committed_full_horizon_open_loop_battery",
            solver=res.solver, solve_seconds=res.runtime_seconds, message=res.message)
        return _finalize_method_result(
            result, truth, realized_d, realized_a, allow_unmet=allow_unmet,
            tol=tol, battery_mode=battery_mode)
    # Execute the committed FULL-HORIZON battery schedule against realized
    # conditions. Generation/flows/unmet are re-dispatched each period, but the
    # battery actions are the optimizer's shared first-stage decisions for every
    # period, so the executed SoC trajectory matches the optimized terminal SoC.
    from gridmind.control import _realized_redispatch
    fs = res.first_stage
    if "charge" not in fs or "discharge" not in fs:
        result = MethodResult(
            method=name, status="error", solver_status="optimal", policy_status="error",
            policy_label="committed_full_horizon_open_loop_battery",
            solver=res.solver, solve_seconds=res.runtime_seconds,
            message="stochastic result missing committed battery charge/discharge arrays")
        return _finalize_method_result(
            result, truth, realized_d, realized_a, allow_unmet=allow_unmet,
            tol=tol, battery_mode=battery_mode)
    charge_fs = fs["charge"]
    discharge_fs = fs["discharge"]
    batt_ids = [b.id for b in truth.batteries]
    for bid in batt_ids:
        if bid not in charge_fs or bid not in discharge_fs:
            raise ValueError(f"missing committed battery actions for battery {bid}")
    charge = {b: [float(v) for v in charge_fs[b]] for b in batt_ids}
    discharge = {b: [float(v) for v in discharge_fs[b]] for b in batt_ids}
    for b in batt_ids:
        if len(charge[b]) != truth.periods or len(discharge[b]) != truth.periods:
            raise ValueError(
                f"stochastic policy did not return a full-horizon committed battery "
                f"schedule for battery {b}: got {len(charge[b])}/{len(discharge[b])} "
                f"periods, need {truth.periods}")
    soc_now = {b.id: float(b.initial_soc_mwh) for b in truth.batteries}
    gen = pen = thr = 0.0
    unmet_energy = 0.0
    dt = truth.step_hours
    init_dispatch: dict[str, float] = {
        g: float(v) for g, v in truth.initial_dispatch_mw.items()}
    total_charge_exec = {b: 0.0 for b in batt_ids}
    total_discharge_exec = {b: 0.0 for b in batt_ids}
    trace: list[dict[str, Any]] = []
    for t in range(truth.periods):
        c = {b: float(charge[b][t]) for b in batt_ids}
        d = {b: float(discharge[b][t]) for b in batt_ids}
        soc_before = dict(soc_now)
        plan = _realized_redispatch(truth, t, realized_d, realized_a, soc_now,
                                    charge=c, discharge=d, init_dispatch=init_dispatch,
                                    battery_mode=battery_mode)
        exec_c = {b: float((plan.get("charge") or {}).get(b, c[b])) for b in batt_ids}
        exec_d = {b: float((plan.get("discharge") or {}).get(b, d[b])) for b in batt_ids}
        comp = _realized_cost_components(truth, t, plan.get("dispatch") or {}, plan.get("unmet") or {}, exec_c, exec_d)
        gen += comp["generation_cost_usd"]
        pen += comp["unmet_penalty_cost_usd"]
        thr += comp["battery_throughput_cost_usd"]
        unmet_energy += dt * sum((plan.get("unmet") or {}).values())
        for b in batt_ids:
            total_charge_exec[b] += exec_c[b]
            total_discharge_exec[b] += exec_d[b]
        soc_now = _advance(truth, soc_now, exec_c, exec_d, t, battery_mode=battery_mode)
        trace.append(_trace_row(
            truth, t, realized_d, realized_a, soc_before,
            {"dispatch": plan.get("dispatch") or {}, "flows": plan.get("flows") or {},
             "charge": exec_c, "discharge": exec_d, "unmet": plan.get("unmet") or {}},
            soc_now, committed={"charge": c, "discharge": d},
            window=(t, min(truth.periods, t + 1)),
            operating_cost_usd=comp["operating_cost_usd"]))
        init_dispatch = {g: float(v) for g, v in (plan.get("dispatch") or {}).items()}
    terminal = dict(soc_now)
    total_demand = truth.total_demand_mwh
    throughput_total_mwh = dt * sum(total_charge_exec[b] + total_discharge_exec[b] for b in batt_ids)
    result = MethodResult(
        method=name, status="optimal", solver_status="optimal",
        policy_status="completed", solver=res.solver,
        policy_label="committed_full_horizon_open_loop_battery_then_realized_redispatch",
        realized_operating_cost_usd=gen + pen + thr,
        generation_cost_usd=gen, unmet_penalty_cost_usd=pen, battery_throughput_cost_usd=thr,
        unmet_energy_mwh=unmet_energy,
        unmet_fraction=(unmet_energy / total_demand) if total_demand > 0 else 0.0,
        battery_throughput_mwh=throughput_total_mwh,
        terminal_soc_deviation_mwh=_terminal_soc_deviation(truth, terminal),
        solve_seconds=res.runtime_seconds, trace=trace,
        periods_executed=len(trace),
        forecast_sha256=_conditions_sha256(fc_d, fc_a),
        config={"risk_weight": risk_weight, "cvar_alpha": cvar_alpha,
                "lead_periods": requested_lead_periods,
                "requested_lead_periods": requested_lead_periods,
                "committed_battery_periods": committed_battery_periods,
                "commitment_horizon": "full",
                "n_scenarios": n_scenarios,
                "battery_mode": battery_mode, "allow_unmet": allow_unmet},
        initial_soc_mwh={b.id: float(b.initial_soc_mwh) for b in truth.batteries},
        initial_dispatch_mw={g: float(v) for g, v in truth.initial_dispatch_mw.items()},
        first_stage={"charge": charge, "discharge": discharge,
                     "lead_periods": committed_battery_periods,
                     "requested_lead_periods": requested_lead_periods,
                     "commitment_horizon": "full"},
        )
    return _finalize_method_result(
        result, truth, realized_d, realized_a, allow_unmet=allow_unmet,
        tol=tol, battery_mode=battery_mode)


def _advance(problem: ScheduleProblem, soc_now: Mapping[str, float], charge: Mapping[str, float],
             discharge: Mapping[str, float], t: int, *, battery_mode: str = "lp"
             ) -> dict[str, float]:
    """Apply one battery transition, rejecting genuinely invalid actions.

    The LP relaxation permits simultaneous charge/discharge; ``exclusive`` mode
    rejects it. Invalid power or SoC bounds are always explicit errors and are never
    silently clamped. Terminal-SoC feasibility is checked separately by the
    independent trajectory validator so a completed trace can be reported as
    policy-infeasible rather than disappearing into an execution exception.
    """
    mode = normalize_battery_mode(battery_mode)
    dt = problem.step_hours
    out = dict(soc_now)
    for b in problem.batteries:
        c = float(charge.get(b.id, 0.0))
        d = float(discharge.get(b.id, 0.0))
        if c < -DEFAULT_TOL or d < -DEFAULT_TOL:
            raise ValueError(f"negative charge/discharge for battery {b.id} at period {t}")
        if c > b.max_charge_mw + DEFAULT_TOL or d > b.max_discharge_mw + DEFAULT_TOL:
            raise ValueError(f"charge/discharge exceeds power bounds for battery {b.id} at period {t}")
        if mode == "exclusive" and c > DEFAULT_TOL and d > DEFAULT_TOL:
            raise ValueError(f"simultaneous charge and discharge for battery {b.id} at period {t} in exclusive mode")
        nxt = (float(soc_now[b.id]) + b.eta_charge * c * dt - d * dt / b.eta_discharge)
        if nxt < b.min_soc_mwh - DEFAULT_TOL or nxt > b.capacity_mwh + DEFAULT_TOL:
            raise ValueError(f"SoC out of bounds for battery {b.id} at period {t} after transition")
        out[b.id] = float(nxt)
    return out

