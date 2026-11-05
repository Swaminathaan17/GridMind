"""Reproducible synthetic uncertainty scenarios for multi-period scheduling.

This module generates *scenario trajectories* of demand and renewable
availability on top of an existing :class:`~gridmind.multiperiod.ScheduleProblem`.
It is deliberately separate from optimisation so that a fixed set of scenarios can
be generated, serialised and tested without invoking a solver.

IMPORTANT - these are SYNTHETIC assumptions, not forecasts.
    Nothing here is fitted to, or validated against, real grid data. The
    distributions are simple, documented parametric shapes chosen for
    reproducibility and for exercising the optimiser; they carry no claim of
    statistical realism.

Units follow the rest of GridMind exactly:
    power  - MW    (demand and generator availability per period)
    energy - MWh   (``step_hours`` scales a period's power into energy)
    cost   - USD   (marginal costs in USD/MWh)

A :class:`ScenarioSet` holds a finite, discrete distribution over
:class:`UncertaintyScenario` objects. Each scenario carries a non-negative
probability; the probabilities must sum to one within a tolerance. Scenario
generation is deterministic given ``seed`` plus the base problem, so the same
inputs always yield byte-identical scenario sets (see :func:`generate_scenarios`).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np

from gridmind.multiperiod import Horizon, ScheduleProblem

#: relative tolerance for the probability-sum check
PROB_TOL = 1e-9

#: the four documented synthetic stress families
STRESS_CASES: tuple[str, ...] = ("ordinary", "high_demand", "low_renewable", "compound")


# --------------------------------------------------------------- scenarios ----
@dataclass(frozen=True)
class UncertaintyScenario:
    """One discrete realization of demand and renewable-availability profiles.

    ``demand_mw[t][node]`` and ``availability_mw[t][generator]`` are per-period
    MW profiles over the *same* network/horizon as the base problem the set was
    generated from. ``probability`` is the discrete weight of this realization.
    """
    id: str
    name: str
    probability: float
    demand_mw: tuple[Mapping[str, float], ...]
    availability_mw: tuple[Mapping[str, float], ...]
    description: str = ""
    case: str = "ordinary"

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("scenario id must be non-empty")
        if not (self.probability >= 0.0):
            raise ValueError(f"scenario {self.id}: probability must be >= 0, got {self.probability}")
        if len(self.demand_mw) != len(self.availability_mw):
            raise ValueError(f"scenario {self.id}: demand and availability profiles differ in length")

    @property
    def periods(self) -> int:
        return len(self.demand_mw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "probability": self.probability,
            "case": self.case,
            "description": self.description,
            "demand_mw": [dict(row) for row in self.demand_mw],
            "availability_mw": [dict(row) for row in self.availability_mw],
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "UncertaintyScenario":
        try:
            return UncertaintyScenario(
                id=str(data["id"]),
                name=str(data.get("name", data["id"])),
                probability=float(data["probability"]),
                demand_mw=tuple(dict(row) for row in data["demand_mw"]),
                availability_mw=tuple(dict(row) for row in data["availability_mw"]),
                description=str(data.get("description", "")),
                case=str(data.get("case", "ordinary")))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed uncertainty scenario: {exc}") from exc


@dataclass(frozen=True)
class ScenarioSet:
    """A discrete distribution over scenarios with explicit probabilities.

    ``demand_nodes``/``availability_generators`` record the id universe the
    profiles cover, so dimension mismatches are detected early and clearly.
    """
    scenarios: tuple[UncertaintyScenario, ...]
    demand_nodes: tuple[str, ...]
    availability_generators: tuple[str, ...]
    seed: int | None = None
    assumptions: str = ""
    _extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.scenarios:
            raise ValueError("scenario set must contain at least one scenario")
        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        for scenario in self.scenarios:
            if scenario.id in seen_ids:
                duplicate_ids.add(scenario.id)
            else:
                seen_ids.add(scenario.id)
        if duplicate_ids:
            raise ValueError(f"scenario IDs must be unique; duplicates: {sorted(duplicate_ids)}")
        validate_probabilities([s.probability for s in self.scenarios])
        periods = self.scenarios[0].periods
        for s in self.scenarios:
            if s.periods != periods:
                raise ValueError(f"scenario {s.id} has {s.periods} periods, expected {periods}")
            _check_dimensions(s, set(self.demand_nodes), set(self.availability_generators))

    @property
    def probabilities(self) -> tuple[float, ...]:
        return tuple(s.probability for s in self.scenarios)

    def scenario(self, scenario_id: str) -> UncertaintyScenario:
        for s in self.scenarios:
            if s.id == scenario_id:
                return s
        raise KeyError(f"unknown uncertainty scenario: {scenario_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "assumptions": self.assumptions,
            "probabilities": list(self.probabilities),
            "demand_nodes": list(self.demand_nodes),
            "availability_generators": list(self.availability_generators),
            "scenarios": [s.to_dict() for s in self.scenarios],
            **dict(self._extra),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ScenarioSet":
        scenarios = tuple(UncertaintyScenario.from_dict(s) for s in data["scenarios"])
        extra = {k: data[k] for k in ("weights", "config") if k in data}
        return ScenarioSet(
            scenarios=scenarios,
            demand_nodes=tuple(data["demand_nodes"]),
            availability_generators=tuple(data["availability_generators"]),
            seed=data.get("seed"),
            assumptions=str(data.get("assumptions", "")),
            _extra=extra)


def validate_probabilities(probabilities: Sequence[float], tol: float = PROB_TOL) -> None:
    """Validate that probabilities are finite, non-negative and sum to one.

    Raises ``ValueError`` with a precise message otherwise. ``tol`` is an
    absolute tolerance on ``|sum - 1|``.
    """
    if len(probabilities) == 0:
        raise ValueError("probability list is empty")
    for i, p in enumerate(probabilities):
        if not np.isfinite(p):
            raise ValueError(f"probability[{i}] must be finite, got {p}")
        if p < 0.0:
            raise ValueError(f"probability[{i}] must be >= 0, got {p}")
    total = float(sum(probabilities))
    if abs(total - 1.0) > tol:
        raise ValueError(f"probabilities must sum to 1 within {tol}, got {total}")


def _check_dimensions(scenario: UncertaintyScenario, nodes: set[str], generators: set[str]) -> None:
    for t, row in enumerate(scenario.demand_mw):
        missing = nodes - set(row)
        extra = set(row) - nodes
        if missing:
            raise ValueError(f"scenario {scenario.id}: demand_mw[{t}] missing nodes {sorted(missing)}")
        if extra:
            raise ValueError(f"scenario {scenario.id}: demand_mw[{t}] unknown nodes {sorted(extra)}")
        for node_id, value in row.items():
            if value < 0 or not np.isfinite(value):
                raise ValueError(f"scenario {scenario.id}: demand_mw[{t}][{node_id}] must be finite and >= 0")
    for t, row in enumerate(scenario.availability_mw):
        missing = generators - set(row)
        extra = set(row) - generators
        if missing:
            raise ValueError(f"scenario {scenario.id}: availability_mw[{t}] missing generators {sorted(missing)}")
        if extra:
            raise ValueError(f"scenario {scenario.id}: availability_mw[{t}] unknown generators {sorted(extra)}")
        for gen_id, value in row.items():
            if value < 0 or not np.isfinite(value):
                raise ValueError(
                    f"scenario {scenario.id}: availability_mw[{t}][{gen_id}] must be finite and >= 0")


# --------------------------------------------------------------- config ----
@dataclass(frozen=True)
class UncertaintyConfig:
    """Parametric assumptions for synthetic scenario generation.

    All multipliers are dimensionless. Standard deviations are relative to the
    base profile value at each period. These are SYNTHETIC assumptions.
    """
    n_scenarios: int = 40
    seed: int = 20260101
    demand_sigma: float = 0.08          # relative demand noise (ordinary case)
    renewable_sigma: float = 0.18       # relative renewable-availability noise
    high_demand_factor: float = 1.25    # multiplier for the high-demand family
    low_renewable_factor: float = 0.35  # multiplier for the low-renewable family
    demand_ar1: float = 0.6             # lag-1 autocorrelation of the demand noise
    weights: Mapping[str, float] = field(
        default_factory=lambda: {"ordinary": 0.55, "high_demand": 0.15,
                                 "low_renewable": 0.20, "compound": 0.10})

    def validate(self) -> None:
        if self.n_scenarios < 1:
            raise ValueError("n_scenarios must be >= 1")
        if self.demand_sigma < 0 or self.renewable_sigma < 0:
            raise ValueError("sigma values must be >= 0")
        if self.high_demand_factor < 0 or self.low_renewable_factor < 0:
            raise ValueError("stress factors must be >= 0")
        if not (-1.0 <= self.demand_ar1 <= 1.0):
            raise ValueError("demand_ar1 must lie in [-1, 1]")
        unknown = set(self.weights) - set(STRESS_CASES)
        if unknown:
            raise ValueError(f"unknown stress cases in weights: {sorted(unknown)}")
        validate_probabilities([self.weights.get(c, 0.0) for c in STRESS_CASES])

    def to_dict(self) -> dict[str, Any]:
        return _config_dict(self)


# --------------------------------------------------------------- generation ----
def _renewable_ids(problem: ScheduleProblem) -> list[str]:
    return [g.id for g in problem.network.generators if g.renewable]


def _base_availability(problem: ScheduleProblem, gen_id: str, t: int) -> float:
    return float(problem.horizon.availability_mw[t][gen_id])


def generate_scenarios(problem: ScheduleProblem, config: UncertaintyConfig | None = None
                       ) -> ScenarioSet:
    """Generate a reproducible :class:`ScenarioSet` for ``problem``.

    Determinism: identical ``(problem, config)`` always produce an identical set,
    because a single ``numpy`` generator seeded with ``config.seed`` drives every
    draw in a fixed order.

    The four documented families are interleaved deterministically and given the
    probabilities in ``config.weights``:

    * ``ordinary``      - demand and renewables fluctuate by their sigmas.
    * ``high_demand``   - demand scaled by ``high_demand_factor``.
    * ``low_renewable`` - renewable availability scaled by ``low_renewable_factor``.
    * ``compound``      - both stresses applied simultaneously.
    """
    config = config or UncertaintyConfig()
    config.validate()
    problem.validate()
    rng = np.random.default_rng(config.seed)

    periods = problem.periods
    node_ids = [n.id for n in problem.network.nodes]
    ren_ids = _renewable_ids(problem)

    # Assign each of the n scenarios a stress case by deterministic weights.
    cases = _assign_cases(config)

    scenarios: list[UncertaintyScenario] = []
    for i in range(config.n_scenarios):
        case = cases[i]
        demand_rows: list[Mapping[str, float]] = []
        avail_rows: list[Mapping[str, float]] = []
        # AR(1) demand noise per node
        noise_prev = {n: 0.0 for n in node_ids}
        for t in range(periods):
            d_row: dict[str, float] = {}
            a_row: dict[str, float] = {}
            d_factor = config.high_demand_factor if case in ("high_demand", "compound") else 1.0
            r_factor = config.low_renewable_factor if case in ("low_renewable", "compound") else 1.0
            for n in node_ids:
                innov = float(rng.normal(0.0, config.demand_sigma))
                noise = config.demand_ar1 * noise_prev[n] + innov
                noise_prev[n] = noise
                base = float(problem.horizon.demand_mw[t][n])
                d_row[n] = round(max(0.0, base * d_factor * (1.0 + noise)), 6)
            for g in problem.network.generators:
                base = _base_availability(problem, g.id, t)
                if g.id in ren_ids:
                    noise = float(rng.normal(0.0, config.renewable_sigma))
                    value = base * r_factor * (1.0 + noise)
                    value = min(g.capacity_mw, max(0.0, value))
                else:
                    value = base
                a_row[g.id] = round(value, 6)
            demand_rows.append(d_row)
            avail_rows.append(a_row)
        scenarios.append(UncertaintyScenario(
            id=f"sc-{config.seed}-{i:03d}", name=f"Scenario {i} ({case})",
            probability=float(config.weights[case]),
            demand_mw=tuple(demand_rows), availability_mw=tuple(avail_rows),
            case=case, description=f"Synthetic '{case}' realization (seed {config.seed})."))

    _normalise_weights(scenarios)
    return ScenarioSet(
        scenarios=tuple(scenarios), demand_nodes=tuple(node_ids),
        availability_generators=tuple(g.id for g in problem.network.generators),
        seed=config.seed,
        assumptions=("Synthetic parametric scenarios (Gaussian noise, AR(1) demand, "
                     "documented stress families). NOT a real-world forecast."),
        _extra={"weights": dict(config.weights), "config": _config_dict(config)})


def _assign_cases(config: UncertaintyConfig) -> list[str]:
    """Deterministically spread ``n_scenarios`` across the weighted families.

    Uses the largest-remainder method so counts match the requested proportions
    as closely as possible without randomness.
    """
    n = config.n_scenarios
    raw = {case: config.weights.get(case, 0.0) * n for case in STRESS_CASES}
    counts = {case: int(np.floor(raw[case])) for case in STRESS_CASES}
    remainder = n - sum(counts.values())
    order = sorted(STRESS_CASES, key=lambda c: (raw[c] - counts[c], c), reverse=True)
    for case in order[:remainder]:
        counts[case] += 1
    cases: list[str] = []
    for case in STRESS_CASES:
        cases.extend([case] * counts[case])
    return cases[:n]


def _normalise_weights(scenarios: list[UncertaintyScenario]) -> None:
    """Re-weight the realized scenarios so their probabilities sum to one.

    The case weights already sum to one, and the counts follow them, but rounding
    could leave a tiny residual; we rescale in place (mutating via replace).
    """
    total = float(sum(s.probability for s in scenarios))
    if total <= 0:
        raise ValueError("scenario probabilities sum to zero; check config weights")
    if abs(total - 1.0) <= PROB_TOL:
        return
    for i, s in enumerate(scenarios):
        scenarios[i] = replace(s, probability=s.probability / total)


def _config_dict(config: UncertaintyConfig) -> dict[str, Any]:
    return {
        "n_scenarios": config.n_scenarios,
        "seed": config.seed,
        "demand_sigma": config.demand_sigma,
        "renewable_sigma": config.renewable_sigma,
        "high_demand_factor": config.high_demand_factor,
        "low_renewable_factor": config.low_renewable_factor,
        "demand_ar1": config.demand_ar1,
        "weights": dict(config.weights),
    }


# --------------------------------------------------------------- helpers ----
def scenario_to_problem(problem: ScheduleProblem, scenario: UncertaintyScenario) -> ScheduleProblem:
    """Return a NEW :class:`ScheduleProblem` whose Horizon matches ``scenario``.

    Battery fleet, network, ramp limits and tie-break are unchanged; only the
    per-period demand and availability profiles are replaced. Used by the
    out-of-sample evaluation to solve a fixed policy's problem under a scenario.
    """
    if scenario.periods != problem.periods:
        raise ValueError(f"scenario {scenario.id} has {scenario.periods} periods, "
                         f"problem has {problem.periods}")
    hor = replace(problem.horizon, demand_mw=scenario.demand_mw,
                  availability_mw=scenario.availability_mw)
    out = replace(problem, horizon=hor)
    out.validate()
    return out


def split_scenarios(scenario_set: ScenarioSet, *, holdout_fraction: float = 0.5,
                    ) -> tuple[ScenarioSet, ScenarioSet]:
    """Deterministically split a set into (in-sample, out-of-sample) subsets.

    The split is by index (stride) so it is reproducible and independent of the
    probabilities. Probabilities inside each subset are renormalised to sum to
    one so each subset remains a valid distribution.
    """
    if not (0.0 < holdout_fraction < 1.0):
        raise ValueError("holdout_fraction must lie strictly between 0 and 1")
    n = len(scenario_set.scenarios)
    if n < 2:
        raise ValueError("need at least two scenarios to split")
    step = max(2, int(round(1.0 / holdout_fraction)))
    in_sample = [s for i, s in enumerate(scenario_set.scenarios) if i % step != 0]
    holdout = [s for i, s in enumerate(scenario_set.scenarios) if i % step == 0]
    if not in_sample or not holdout:
        raise ValueError("split produced an empty subset; adjust holdout_fraction or n_scenarios")

    def _rescale(items: list[UncertaintyScenario]) -> ScenarioSet:
        total = float(sum(s.probability for s in items))
        rescaled = tuple(replace(s, probability=s.probability / total) for s in items)
        return ScenarioSet(scenarios=rescaled, demand_nodes=scenario_set.demand_nodes,
                           availability_generators=scenario_set.availability_generators,
                           seed=scenario_set.seed, assumptions=scenario_set.assumptions)

    return _rescale(in_sample), _rescale(holdout)
