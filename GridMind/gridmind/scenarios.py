"""Deterministic scenario definitions and network transformations."""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from gridmind.network import Network
from gridmind.multiperiod import ScheduleProblem


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    description: str
    demand_multiplier: float = 1.0
    renewable_multiplier: float = 1.0
    line_outages: tuple[str, ...] = ()
    generator_outages: tuple[str, ...] = ()


# ---- elementary transformations (all return NEW networks) -----------------
def scale_demand(network: Network, factor: float) -> Network:
    nodes = tuple(replace(n, demand_mw=n.demand_mw * factor) for n in network.nodes)
    return replace(network, nodes=nodes)


def scale_renewable_availability(network: Network, factor: float) -> Network:
    gens = tuple(
        replace(g, availability_mw=min(g.capacity_mw, g.available_mw * factor)) if g.renewable else g
        for g in network.generators
    )
    return replace(network, generators=gens)


def scale_generator_cost(network: Network, factor: float, generator_id: str | None = None) -> Network:
    if generator_id is not None:
        network.generator(generator_id)  # raises KeyError if unknown
    gens = tuple(
        replace(g, cost_per_mwh=g.cost_per_mwh * factor) if generator_id in (None, g.id) else g
        for g in network.generators
    )
    return replace(network, generators=gens)


def scale_line_capacity(network: Network, factor: float, line_id: str | None = None) -> Network:
    if line_id is not None:
        network.line(line_id)
    lines = tuple(
        replace(l, capacity_mw=l.capacity_mw * factor) if line_id in (None, l.id) else l
        for l in network.lines
    )
    return replace(network, lines=lines)


def with_line_outage(network: Network, line_id: str) -> Network:
    network.line(line_id)
    return replace(network, lines=tuple(replace(l, in_service=False) if l.id == line_id else l for l in network.lines))


def with_generator_outage(network: Network, generator_id: str) -> Network:
    network.generator(generator_id)
    gens = tuple(replace(g, online=False) if g.id == generator_id else g for g in network.generators)
    return replace(network, generators=gens)


def apply_scenario(network: Network, scenario: Scenario) -> Network:
    out = scale_demand(network, scenario.demand_multiplier)
    out = scale_renewable_availability(out, scenario.renewable_multiplier)
    for line_id in scenario.line_outages:
        out = with_line_outage(out, line_id)
    for gen_id in scenario.generator_outages:
        out = with_generator_outage(out, gen_id)
    return out


# ---- named scenarios -------------------------------------------------------
CORE_SCENARIOS: tuple[Scenario, ...] = (
    Scenario("normal", "Normal demand", "Reference demand and renewable forecast, intact network."),
    Scenario("peak_demand", "Peak demand", "Demand +28% everywhere; everything else unchanged.",
             demand_multiplier=1.28),
    Scenario("low_renewable", "Reduced renewables",
             "Hydro, solar and wind availability cut to 30% of the reference forecast.",
             renewable_multiplier=0.3),
    Scenario("line_outage", "Transmission-line outage",
             "Central-East 220 kV line out of service.",
             line_outages=("line-central-east",)),
)

EXTRA_SCENARIOS: tuple[Scenario, ...] = (
    Scenario("generator_outage", "Generator outage", "North Thermal (gas-north) forced out.",
             generator_outages=("gas-north",)),
    Scenario("feeder_outage", "Central feeder outage (unserved demand)",
             "The only feeder to Central Demand is out: that load is physically unreachable.",
             line_outages=("line-central-demand",)),
)

ALL_SCENARIOS: tuple[Scenario, ...] = CORE_SCENARIOS + EXTRA_SCENARIOS


def get_scenario(scenario_id: str) -> Scenario:
    for s in ALL_SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown scenario '{scenario_id}'; known: {[s.id for s in ALL_SCENARIOS]}")


def generate_random_scenarios(network: Network, n: int, seed: int = 42) -> list[Scenario]:
    """Reproducible random scenarios (same seed + network -> identical list)."""
    rng = np.random.default_rng(seed)
    line_ids = [l.id for l in network.lines]
    gen_ids = [g.id for g in network.generators]
    out: list[Scenario] = []
    for i in range(n):
        demand_mult = round(float(rng.uniform(0.7, 1.4)), 3)
        ren_mult = round(float(rng.uniform(0.2, 1.0)), 3)
        lines = (line_ids[int(rng.integers(len(line_ids)))],) if rng.random() < 0.35 else ()
        gens = (gen_ids[int(rng.integers(len(gen_ids)))],) if rng.random() < 0.15 else ()
        out.append(Scenario(
            id=f"rand-{seed}-{i:03d}", name=f"Random scenario {i}",
            description="Randomly generated (seeded) stress scenario.",
            demand_multiplier=demand_mult, renewable_multiplier=ren_mult,
            line_outages=lines, generator_outages=gens))
    return out


# ====================================== multi-period (horizon) scenarios ====
@dataclass(frozen=True)
class DemandSpike:
    """Multiply demand at ``node`` (all nodes when None) inside [start, end)."""
    start: int
    end: int
    multiplier: float
    node: str | None = None


@dataclass(frozen=True)
class RenewableDrop:
    """Scale renewable availability of ``generator`` (all renewables when None)
    inside [start, end)."""
    start: int
    end: int
    factor: float
    generator: str | None = None


@dataclass(frozen=True)
class OutageWindow:
    """Force an element out of service inside [start, end)."""
    element_id: str
    start: int
    end: int


@dataclass(frozen=True)
class HorizonScenario:
    """Time-located stress experiment for a multi-period ScheduleProblem."""
    id: str
    name: str
    description: str
    demand_multiplier: float = 1.0
    renewable_multiplier: float = 1.0
    demand_spikes: tuple[DemandSpike, ...] = ()
    renewable_drops: tuple[RenewableDrop, ...] = ()
    generator_outages: tuple[OutageWindow, ...] = ()
    line_outages: tuple[OutageWindow, ...] = ()


def _check_window(window: tuple[int, ...], periods: int, label: str) -> None:
    start, end = window[0], window[1]
    if not (0 <= start < end <= periods):
        raise ValueError(f"{label}: window [{start}, {end}) must satisfy 0 <= start < end <= {periods}")


def apply_horizon_scenario(problem: ScheduleProblem, scenario: HorizonScenario) -> ScheduleProblem:
    """Return a NEW ScheduleProblem with the scenario's time-located stresses applied."""
    hor = problem.horizon
    periods = hor.periods
    for spike in scenario.demand_spikes:
        _check_window((spike.start, spike.end), periods, f"scenario {scenario.id} demand spike")
        if spike.node is not None:
            problem.network.node(spike.node)
    for drop in scenario.renewable_drops:
        _check_window((drop.start, drop.end), periods, f"scenario {scenario.id} renewable drop")
        if drop.generator is not None:
            gen = problem.network.generator(drop.generator)
            if not gen.renewable:
                raise ValueError(f"scenario {scenario.id}: {drop.generator} is not renewable")
    for win in scenario.generator_outages:
        _check_window((win.start, win.end), periods, f"scenario {scenario.id} generator outage")
        problem.network.generator(win.element_id)
    for win in scenario.line_outages:
        _check_window((win.start, win.end), periods, f"scenario {scenario.id} line outage")
        problem.network.line(win.element_id)

    net = problem.network
    demand_mw, availability_mw, generator_online, line_in_service = [], [], [], []
    for t in range(periods):
        row, avail, g_on, l_on = {}, {}, {}, {}
        for n in net.nodes:
            value = hor.demand(n.id, t) * scenario.demand_multiplier
            for spike in scenario.demand_spikes:
                if spike.start <= t < spike.end and spike.node in (None, n.id):
                    value *= spike.multiplier
            row[n.id] = round(max(0.0, value), 6)
        for g in net.generators:
            value = min(g.capacity_mw, hor.availability_mw[t][g.id] * scenario.renewable_multiplier)
            for drop in scenario.renewable_drops:
                if drop.start <= t < drop.end and drop.generator in (None, g.id) and g.renewable:
                    value = min(g.capacity_mw, value * drop.factor)
            avail[g.id] = round(max(0.0, value), 6)
            g_on[g.id] = hor.generator_online[t][g.id] and not any(
                win.element_id == g.id and win.start <= t < win.end for win in scenario.generator_outages)
        for l in net.lines:
            l_on[l.id] = hor.line_in_service[t][l.id] and not any(
                win.element_id == l.id and win.start <= t < win.end for win in scenario.line_outages)
        demand_mw.append(row)
        availability_mw.append(avail)
        generator_online.append(g_on)
        line_in_service.append(l_on)

    out = replace(problem, horizon=replace(hor, demand_mw=tuple(demand_mw),
                                           availability_mw=tuple(availability_mw),
                                           generator_online=tuple(generator_online),
                                           line_in_service=tuple(line_in_service)))
    out.validate()
    return out


CORE_HORIZON_SCENARIOS: tuple[HorizonScenario, ...] = (
    HorizonScenario("h-normal", "Normal day", "Reference profiles, intact network, storage available."),
    HorizonScenario("h-evening-spike", "Evening demand spike",
                    "All demand nodes x1.30 during 18:00-21:00; everything else unchanged.",
                    demand_spikes=(DemandSpike(18, 21, 1.30),)),
    HorizonScenario("h-renewable-drop", "Renewable forecast drop",
                    "All renewable availability cut to 30% during 16:00-21:00.",
                    renewable_drops=(RenewableDrop(16, 21, 0.30),)),
    HorizonScenario("h-line-storm", "Storm line outage",
                    "Central-East 220 kV line out during 10:00-16:00.",
                    line_outages=(OutageWindow("line-central-east", 10, 16),)),
    HorizonScenario("h-generator-outage", "Evening generator outage",
                    "North Thermal (gas-north) forced out during 17:00-22:00.",
                    generator_outages=(OutageWindow("gas-north", 17, 22),)),
    HorizonScenario("h-feeder-loss", "Permanent central feeder loss",
                    "The only feeder to Central Demand is out all day: that load is unreachable.",
                    line_outages=(OutageWindow("line-central-demand", 0, 24),)),
    HorizonScenario("h-compound", "Compound stress day",
                    "Evening spike + renewable drop + gas-north outage, all overlapping 17:00-21:00.",
                    demand_spikes=(DemandSpike(17, 22, 1.25),),
                    renewable_drops=(RenewableDrop(15, 22, 0.35),),
                    generator_outages=(OutageWindow("gas-north", 16, 22),)),
)

ALL_HORIZON_SCENARIOS: tuple[HorizonScenario, ...] = CORE_HORIZON_SCENARIOS


def get_horizon_scenario(scenario_id: str) -> HorizonScenario:
    for s in ALL_HORIZON_SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown horizon scenario '{scenario_id}'; known: {[s.id for s in ALL_HORIZON_SCENARIOS]}")


def generate_random_horizon_scenarios(problem: ScheduleProblem, n: int, seed: int = 42) -> list[HorizonScenario]:
    """Reproducible random horizon scenarios (same seed + problem -> identical list)."""
    rng = np.random.default_rng(seed)
    periods = problem.periods
    line_ids = [l.id for l in problem.network.lines]
    gen_ids = [g.id for g in problem.network.generators]
    out: list[HorizonScenario] = []
    for i in range(n):
        spikes, drops, gen_wins, line_wins = [], [], [], []
        if rng.random() < 0.4:
            start = int(rng.integers(0, max(1, periods - 3)))
            spikes.append(DemandSpike(start, min(periods, start + int(rng.integers(2, 5))),
                                      round(float(rng.uniform(1.15, 1.45)), 3)))
        if rng.random() < 0.4:
            start = int(rng.integers(0, max(1, periods - 3)))
            drops.append(RenewableDrop(start, min(periods, start + int(rng.integers(2, 6))),
                                       round(float(rng.uniform(0.2, 0.6)), 3)))
        if rng.random() < 0.3:
            start = int(rng.integers(0, max(1, periods - 2)))
            gen_wins.append(OutageWindow(gen_ids[int(rng.integers(len(gen_ids)))], start,
                                         min(periods, start + int(rng.integers(2, 5)))))
        if rng.random() < 0.35:
            start = int(rng.integers(0, max(1, periods - 2)))
            line_wins.append(OutageWindow(line_ids[int(rng.integers(len(line_ids)))], start,
                                          min(periods, start + int(rng.integers(2, 5)))))
        out.append(HorizonScenario(
            id=f"hrand-{seed}-{i:03d}", name=f"Random horizon scenario {i}",
            description="Randomly generated (seeded) time-located stress scenario.",
            demand_multiplier=round(float(rng.uniform(0.9, 1.15)), 3),
            renewable_multiplier=round(float(rng.uniform(0.7, 1.05)), 3),
            demand_spikes=tuple(spikes), renewable_drops=tuple(drops),
            generator_outages=tuple(gen_wins), line_outages=tuple(line_wins)))
    return out
