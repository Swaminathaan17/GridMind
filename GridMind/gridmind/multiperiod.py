"""Multi-period scheduling problem: batteries, horizons and time-dependent profiles.

This module owns the *data model* for multi-period optimisation. The network
topology, generator and line definitions are reused from ``gridmind.network``;
everything time-varying lives in a ``Horizon`` (demand, availability and outage
profiles per period) and storage is described by ``Battery`` objects.

Units (used consistently everywhere):
  power  - MW       (generator output, line flow, charge/discharge power, demand)
  energy - MWh      (battery state of charge, energy totals)
  cost   - USD      (objective); marginal costs in USD/MWh
  time   - hours    (``Horizon.step_hours`` is the duration dt of one period)

SoC convention: ``Battery`` initial SoC is the energy stored *before* period 0
(state at t=-1); trajectories returned by the solver report the SoC *after* each
period, i.e. the end-of-period state.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from gridmind.network import DATA_DIR, Generator, Line, Network, Node, load_network

DEFAULT_SAMPLE_HORIZON_PATH = DATA_DIR / "sample_horizon.json"

#: default lexicographic tie-break (USD/MWh of battery charge+discharge). Small
#: enough not to distort operating cost. It discourages *gratuitous* simultaneous
#: charging/discharging but does NOT guarantee cycle-free optima: when round-trip
#: efficiency is below one, a charge/discharge pair is not SoC-neutral and may be
#: required for feasibility (see README, "simultaneity").
DEFAULT_TIEBREAK_EPS = 1e-3


# ----------------------------------------------------------------- battery ----
@dataclass(frozen=True)
class Battery:
    """Battery energy storage at a node.

    Charging power ``ch`` and discharging power ``dis`` are MW; the stored
    energy evolves as
        e[t] = e[t-1] + eta_charge * ch[t] * dt - dis[t] * dt / eta_discharge
    with losses making e strictly smaller than the energy drawn from the grid.
    """
    id: str
    node: str
    capacity_mwh: float
    max_charge_mw: float
    max_discharge_mw: float
    eta_charge: float = 0.95
    eta_discharge: float = 0.95
    initial_soc_mwh: float = 0.0
    min_soc_mwh: float = 0.0
    final_soc_mwh: float | None = None   # None = free terminal state
    throughput_cost_per_mwh: float = 0.0  # USD per MWh discharged (degradation proxy)

    def validate(self, node_ids: set[str]) -> None:
        if self.node not in node_ids:
            raise ValueError(f"battery {self.id} references unknown node {self.node}")
        if min(self.capacity_mwh, self.max_charge_mw, self.max_discharge_mw,
               self.min_soc_mwh, self.initial_soc_mwh, self.throughput_cost_per_mwh) < 0:
            raise ValueError(f"battery {self.id} has a negative capacity, power, SoC or cost")
        if not (0.0 < self.eta_charge <= 1.0) or not (0.0 < self.eta_discharge <= 1.0):
            raise ValueError(f"battery {self.id} efficiencies must lie in (0, 1]")
        if self.min_soc_mwh > self.capacity_mwh:
            raise ValueError(f"battery {self.id}: min_soc_mwh exceeds capacity_mwh")
        if not (self.min_soc_mwh <= self.initial_soc_mwh <= self.capacity_mwh):
            raise ValueError(f"battery {self.id}: initial SoC outside [min_soc, capacity]")
        if self.final_soc_mwh is not None and not (self.min_soc_mwh <= self.final_soc_mwh <= self.capacity_mwh):
            raise ValueError(f"battery {self.id}: final SoC outside [min_soc, capacity]")

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


# ------------------------------------------------------------------ horizon ----
@dataclass(frozen=True)
class Horizon:
    """Time-varying data for a schedule of ``periods`` steps of ``step_hours`` each.

    Every inner mapping covers ALL elements of that kind (missing elements are
    not allowed; builders and loaders fill defaults from the base network).
    """
    periods: int
    step_hours: float
    demand_mw: tuple[Mapping[str, float], ...]            # [t][node]
    availability_mw: tuple[Mapping[str, float], ...]      # [t][generator]
    generator_online: tuple[Mapping[str, bool], ...]      # [t][generator]
    line_in_service: tuple[Mapping[str, bool], ...]       # [t][line]

    def validate(self, network: Network) -> None:
        if self.periods < 1 or int(self.periods) != self.periods:
            raise ValueError(f"horizon periods must be a positive integer, got {self.periods}")
        if not (self.step_hours > 0) or not math.isfinite(self.step_hours):
            raise ValueError(f"horizon step_hours must be > 0, got {self.step_hours}")
        node_ids = {n.id for n in network.nodes}
        gen_ids = {g.id for g in network.generators}
        line_ids = {l.id for l in network.lines}
        expected = {"demand_mw": (self.demand_mw, node_ids),
                    "availability_mw": (self.availability_mw, gen_ids),
                    "generator_online": (self.generator_online, gen_ids),
                    "line_in_service": (self.line_in_service, line_ids)}
        for label, (series, keys) in expected.items():
            if len(series) != self.periods:
                raise ValueError(f"horizon {label} has {len(series)} periods, expected {self.periods}")
        for t, row in enumerate(self.demand_mw):
            _require_keys(row, node_ids, f"demand_mw[{t}]")
            for node_id, value in row.items():
                if value < 0 or not math.isfinite(float(value)):
                    raise ValueError(f"demand_mw[{t}][{node_id}] must be finite and >= 0")
        for t, row in enumerate(self.availability_mw):
            _require_keys(row, gen_ids, f"availability_mw[{t}]")
            for gen_id, value in row.items():
                if value < 0 or not math.isfinite(float(value)):
                    raise ValueError(f"availability_mw[{t}][{gen_id}] must be finite and >= 0")
        for t, row in enumerate(self.generator_online):
            _require_keys(row, gen_ids, f"generator_online[{t}]")
        for t, row in enumerate(self.line_in_service):
            _require_keys(row, line_ids, f"line_in_service[{t}]")

    # convenience -----------------------------------------------------------
    def demand(self, node_id: str, t: int) -> float:
        return float(self.demand_mw[t][node_id])

    def generation_limit(self, gen: Generator, t: int) -> float:
        """Upper bound on gen output at period t (capacity, availability, outage)."""
        if not self.generator_online[t][gen.id]:
            return 0.0
        return max(0.0, min(gen.capacity_mw, float(self.availability_mw[t][gen.id])))

    def line_limit(self, line: Line, t: int) -> float:
        return line.capacity_mw if self.line_in_service[t][line.id] else 0.0


def _require_keys(row: Mapping[str, Any], keys: set[str], label: str) -> None:
    missing = keys - set(row)
    extra = set(row) - keys
    if missing:
        raise ValueError(f"horizon {label} is missing entries for: {sorted(missing)}")
    if extra:
        raise ValueError(f"horizon {label} has unknown entries: {sorted(extra)}")


# ---------------------------------------------------------- schedule problem ----
@dataclass(frozen=True)
class ScheduleProblem:
    """A complete multi-period optimisation problem.

    ``network`` supplies topology, generator costs/capacities/ramps and the
    unmet-demand penalty; ``horizon`` supplies everything time-varying;
    ``batteries`` the storage fleet. ``tiebreak_epsilon`` (USD/MWh) is a tiny
    lexicographic objective term on battery throughput that discourages
    simultaneous charging/discharging without mixed-integer variables. It is not
    a guarantee: with round-trip efficiency below one the pair is not
    SoC-neutral and may be indispensable for feasibility (see README).
    """
    network: Network
    horizon: Horizon
    batteries: tuple[Battery, ...] = ()
    initial_dispatch_mw: Mapping[str, float] = field(default_factory=dict)
    notes: str = ""
    tiebreak_epsilon: float = DEFAULT_TIEBREAK_EPS

    # ---- validation --------------------------------------------------------
    def validate(self) -> None:
        self.network.validate()
        self.horizon.validate(self.network)
        battery_ids = [b.id for b in self.batteries]
        if len(battery_ids) != len(set(battery_ids)):
            raise ValueError("duplicate battery ids in schedule problem")
        node_ids = {n.id for n in self.network.nodes}
        for b in self.batteries:
            b.validate(node_ids)
        gen_ids = {g.id for g in self.network.generators}
        for gen_id, value in self.initial_dispatch_mw.items():
            if gen_id not in gen_ids:
                raise ValueError(f"initial_dispatch_mw references unknown generator {gen_id}")
            if value < 0 or not math.isfinite(float(value)):
                raise ValueError(f"initial_dispatch_mw[{gen_id}] must be finite and >= 0")
        if self.tiebreak_epsilon < 0:
            raise ValueError("tiebreak_epsilon must be >= 0")

    # ---- convenience -------------------------------------------------------
    @property
    def periods(self) -> int:
        return self.horizon.periods

    @property
    def step_hours(self) -> float:
        return self.horizon.step_hours

    @property
    def total_demand_mwh(self) -> float:
        dt = self.step_hours
        return dt * sum(float(v) for row in self.horizon.demand_mw for v in row.values())

    def battery(self, battery_id: str) -> Battery:
        for b in self.batteries:
            if b.id == battery_id:
                return b
        raise KeyError(f"unknown battery: {battery_id}")

    def without_batteries(self) -> "ScheduleProblem":
        return replace(self, batteries=())

    # ---- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "notes": self.notes,
            "periods": self.periods,
            "step_hours": self.step_hours,
            "tiebreak_epsilon": self.tiebreak_epsilon,
            "batteries": [b.to_dict() for b in self.batteries],
            "initial_dispatch_mw": dict(self.initial_dispatch_mw),
            "demand_mw": [dict(row) for row in self.horizon.demand_mw],
            "availability_mw": [dict(row) for row in self.horizon.availability_mw],
            "generator_online": [dict(row) for row in self.horizon.generator_online],
            "line_in_service": [dict(row) for row in self.horizon.line_in_service],
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any], *, base_dir: str | Path | None = None,
                  network: Network | None = None) -> "ScheduleProblem":
        """Build from the JSON schema (see data/sample_horizon.json).

        The network is either referenced by ``network_file`` (resolved relative
        to ``base_dir``) or embedded under ``network``; alternatively an already
        loaded ``network`` may be passed in.
        """
        if network is None:
            if "network" in data:
                network = Network.from_dict(dict(data["network"]))
            elif "network_file" in data:
                path = Path(data["network_file"])
                if base_dir is not None and not path.is_absolute():
                    path = Path(base_dir) / path
                network = load_network(path)
            else:
                raise ValueError("schedule JSON needs a 'network_file' reference or an embedded 'network'")
        periods = int(data["periods"])
        step_hours = float(data.get("step_hours", network.interval_hours))
        node_ids = [n.id for n in network.nodes]
        gen_ids = [g.id for g in network.generators]
        line_ids = [l.id for l in network.lines]

        horizon = Horizon(
            periods=periods,
            step_hours=step_hours,
            demand_mw=_fill_series(data.get("demand_mw"), node_ids, periods,
                                   lambda k: [network.node(k).demand_mw] * periods, float),
            availability_mw=_fill_series(data.get("availability_mw"), gen_ids, periods,
                                         lambda k: [network.generator(k).available_mw] * periods, float),
            generator_online=_fill_series(data.get("generator_online"), gen_ids, periods,
                                          lambda k: [network.generator(k).online] * periods, bool),
            line_in_service=_fill_series(data.get("line_in_service"), line_ids, periods,
                                         lambda k: [network.line(k).in_service] * periods, bool))
        try:
            batteries = tuple(Battery(**dict(b)) for b in data.get("batteries", []))
        except TypeError as exc:
            raise ValueError(f"battery entry has an unexpected or malformed field: {exc}") from exc
        problem = ScheduleProblem(
            network=network, horizon=horizon, batteries=batteries,
            initial_dispatch_mw={k: float(v) for k, v in data.get("initial_dispatch_mw", {}).items()},
            notes=str(data.get("notes", "")),
            tiebreak_epsilon=float(data.get("tiebreak_epsilon", DEFAULT_TIEBREAK_EPS)))
        problem.validate()
        return problem


def _as_columns(raw: Any) -> dict[str, Any] | None:
    """Accept profiles either as {id: [series]} or as a list of per-period {id: value}."""
    if raw is None:
        return None
    if isinstance(raw, list):
        if not raw or not isinstance(raw[0], dict):
            raise ValueError("profile list must contain per-period objects")
        keys = set(raw[0])
        columns: dict[str, list[Any]] = {k: [] for k in keys}
        for row in raw:
            if set(row) != keys:
                raise ValueError("per-period profile rows must all have the same ids")
            for k, v in row.items():
                columns[k].append(v)
        return columns
    if isinstance(raw, dict):
        return raw
    raise ValueError(f"profile must be an object of series or a list of rows, got {type(raw).__name__}")


def _fill_series(raw: Any, keys: list[str], periods: int,
                 default: Callable[[str], list[Any]], cast: Callable[[Any], Any]
                 ) -> tuple[Mapping[str, Any], ...]:
    """Per-period dict rows covering every element; defaults come from ``default``."""
    raw = _as_columns(raw) or {}
    unknown = set(raw) - set(keys)
    if unknown:
        raise ValueError(f"profile references unknown ids: {sorted(unknown)}")
    columns: dict[str, list[Any]] = {}
    for key in keys:
        values = list(raw[key]) if key in raw else list(default(key))
        if len(values) != periods:
            raise ValueError(f"profile for '{key}' has {len(values)} values, expected {periods}")
        columns[key] = [cast(v) for v in values]
    return tuple({key: columns[key][t] for key in keys} for t in range(periods))


def constant_horizon(network: Network, periods: int, *, step_hours: float | None = None,
                     batteries: tuple[Battery, ...] = (),
                     initial_dispatch_mw: Mapping[str, float] | None = None) -> ScheduleProblem:
    """Flat profiles repeated over ``periods`` (time-invariant reference case)."""
    problem = ScheduleProblem(
        network=network,
        horizon=Horizon(
            periods=periods,
            step_hours=float(step_hours if step_hours is not None else network.interval_hours),
            demand_mw=tuple({n.id: n.demand_mw for n in network.nodes} for _ in range(periods)),
            availability_mw=tuple({g.id: g.available_mw for g in network.generators} for _ in range(periods)),
            generator_online=tuple({g.id: g.online for g in network.generators} for _ in range(periods)),
            line_in_service=tuple({l.id: l.in_service for l in network.lines} for _ in range(periods))),
        batteries=batteries,
        initial_dispatch_mw=dict(initial_dispatch_mw or {}))
    problem.validate()
    return problem


def load_schedule_problem(path: str | Path) -> ScheduleProblem:
    path = Path(path)
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return ScheduleProblem.from_dict(data, base_dir=path.parent)


def save_schedule_problem(problem: ScheduleProblem, network_file: str, path: str | Path) -> Path:
    """Write a problem as JSON referencing the given (relative) network file."""
    payload = {"network_file": network_file}
    payload.update(problem.to_dict())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------ sample synthetic data ----
SAMPLE_PERIODS = 24
SAMPLE_DT_HOURS = 1.0


def _diurnal_demand_factor(t: int) -> float:
    """Documented synthetic load shape: base 0.80, morning bump at 08:00,
    evening peak at 19:00, clipped to [0.70, 1.35]."""
    morning = 0.10 * math.exp(-((t - 8) ** 2) / (2 * 1.5 ** 2))
    evening = 0.35 * math.exp(-((t - 19) ** 2) / (2 * 2.5 ** 2))
    return max(0.70, min(1.35, 0.80 + morning + evening))


def _solar_factor(t: int) -> float:
    """Clear-sky proxy: 06:00-18:00 sine lobe scaled to the network's 72 MW peak."""
    if 6 <= t < 18:
        return 72.0 * math.sin(math.pi * (t - 6) / 12.0)
    return 0.0


def _wind_factor(t: int) -> float:
    """Slow diurnal wind variation in [0.10, 1.00] x 48 MW."""
    return 48.0 * (0.55 + 0.45 * math.sin(2 * math.pi * (t + 3) / 24.0))


def _hydro_factor(t: int) -> float:
    """Flat hydro with a mild overnight dip, in [35.75, 55.25] MW."""
    return 65.0 * (0.70 + 0.15 * math.sin(2 * math.pi * t / 24.0))


def make_sample_horizon_problem() -> ScheduleProblem:
    """Deterministic 24x1h synthetic day for the sample network.

    Demand, solar, wind and hydro follow the closed-form shapes above; thermal
    availability is constant. Two batteries and the thermal ramp limits declared
    in the network make inter-temporal coupling observable. Everything is
    synthetic.
    """
    network = load_network()
    periods = SAMPLE_PERIODS
    demand_mw = tuple({n.id: round(n.demand_mw * _diurnal_demand_factor(t), 4) for n in network.nodes}
                      for t in range(periods))
    availability_mw = tuple({
        g.id: round(_solar_factor(t), 4) if g.id == "solar-west"
        else round(_wind_factor(t), 4) if g.id == "wind-west"
        else round(_hydro_factor(t), 4) if g.id == "hydro-central"
        else float(g.available_mw)
        for g in network.generators} for t in range(periods))
    batteries = (
        Battery(id="battery-central", node="central-hub", capacity_mwh=100.0,
                max_charge_mw=40.0, max_discharge_mw=40.0,
                eta_charge=0.95, eta_discharge=0.95,
                initial_soc_mwh=50.0, min_soc_mwh=10.0, final_soc_mwh=50.0),
        Battery(id="battery-south", node="south-hub", capacity_mwh=40.0,
                max_charge_mw=20.0, max_discharge_mw=20.0,
                eta_charge=0.90, eta_discharge=0.90,
                initial_soc_mwh=10.0, min_soc_mwh=5.0, final_soc_mwh=10.0))
    problem = ScheduleProblem(
        network=network,
        horizon=Horizon(
            periods=periods, step_hours=SAMPLE_DT_HOURS,
            demand_mw=demand_mw, availability_mw=availability_mw,
            generator_online=tuple({g.id: True for g in network.generators} for _ in range(periods)),
            line_in_service=tuple({l.id: True for l in network.lines} for _ in range(periods))),
        batteries=batteries,
        initial_dispatch_mw={"gas-north": 100.0, "gas-east": 80.0, "gas-south": 40.0},
        notes=("Fully synthetic 24-hour day: gaussian demand bumps (08:00/19:00), "
               "sine solar/wind lobes, mild hydro dip, two batteries, thermal ramp "
               "limits as declared in the network file. Not real grid data."))
    problem.validate()
    return problem


def sample_schedule_problem() -> ScheduleProblem:
    """The committed 24-hour sample horizon (data/sample_horizon.json)."""
    return load_schedule_problem(DEFAULT_SAMPLE_HORIZON_PATH)
