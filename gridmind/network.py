"""Simulated network definitions (immutable data classes + JSON loading).

Units: power in MW, energy in MWh, cost in USD (a currency-neutral label),
generator cost in USD/MWh. Every optimisation covers one interval of
``interval_hours`` hours, so cost [USD] = sum(cost [USD/MWh] * MW * hours).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SAMPLE_NETWORK_PATH = DATA_DIR / "sample_network.json"
DEFAULT_UNMET_PENALTY = 1000.0  # USD per MWh of unserved energy


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    demand_mw: float = 0.0


@dataclass(frozen=True)
class Generator:
    id: str
    name: str
    node: str
    fuel: str
    capacity_mw: float          # nameplate limit
    cost_per_mwh: float
    renewable: bool = False
    availability_mw: float | None = None  # renewable forecast; None -> capacity
    online: bool = True         # False models a generator outage
    # Ramp limits (MW of output change between consecutive periods). Only used by
    # the multi-period model; a None value means the generator is not ramp-limited.
    ramp_up_mw: float | None = None
    ramp_down_mw: float | None = None

    @property
    def available_mw(self) -> float:
        """Availability cap (forecast for renewables, capacity otherwise)."""
        if self.availability_mw is None:
            return self.capacity_mw
        return self.availability_mw

    @property
    def effective_limit_mw(self) -> float:
        """Upper bound on output implied by capacity, availability and outage."""
        if not self.online:
            return 0.0
        return max(0.0, min(self.capacity_mw, self.available_mw))


@dataclass(frozen=True)
class Line:
    id: str
    name: str
    from_node: str
    to_node: str
    capacity_mw: float
    in_service: bool = True     # False models a line outage

    @property
    def effective_capacity_mw(self) -> float:
        return self.capacity_mw if self.in_service else 0.0


@dataclass(frozen=True)
class Network:
    name: str
    nodes: tuple[Node, ...]
    generators: tuple[Generator, ...]
    lines: tuple[Line, ...]
    unmet_penalty_per_mwh: float = DEFAULT_UNMET_PENALTY
    interval_hours: float = 1.0
    notes: str = field(default="", compare=False)

    # ---- lookups -------------------------------------------------------
    def node(self, node_id: str) -> Node:
        return _find(self.nodes, node_id, "node")

    def generator(self, gen_id: str) -> Generator:
        return _find(self.generators, gen_id, "generator")

    def line(self, line_id: str) -> Line:
        return _find(self.lines, line_id, "line")

    @property
    def total_demand_mw(self) -> float:
        return sum(n.demand_mw for n in self.nodes)

    # ---- integrity -----------------------------------------------------
    def validate(self) -> None:
        """Raise ValueError for structurally invalid input."""
        for label, items in (("node", self.nodes), ("generator", self.generators), ("line", self.lines)):
            ids = [i.id for i in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} ids in network '{self.name}'")
        node_ids = {n.id for n in self.nodes}
        for g in self.generators:
            if g.node not in node_ids:
                raise ValueError(f"generator {g.id} references unknown node {g.node}")
            if g.capacity_mw < 0 or g.cost_per_mwh < 0:
                raise ValueError(f"generator {g.id} has negative capacity or cost")
            if g.availability_mw is not None and g.availability_mw < 0:
                raise ValueError(f"generator {g.id} has negative availability")
            if g.ramp_up_mw is not None and g.ramp_up_mw < 0:
                raise ValueError(f"generator {g.id} has negative ramp_up_mw")
            if g.ramp_down_mw is not None and g.ramp_down_mw < 0:
                raise ValueError(f"generator {g.id} has negative ramp_down_mw")
        for ln in self.lines:
            if ln.from_node not in node_ids or ln.to_node not in node_ids:
                raise ValueError(f"line {ln.id} references unknown node")
            if ln.from_node == ln.to_node:
                raise ValueError(f"line {ln.id} is a self-loop")
            if ln.capacity_mw < 0:
                raise ValueError(f"line {ln.id} has negative capacity")
        if any(n.demand_mw < 0 for n in self.nodes):
            raise ValueError("node demand must be non-negative")
        if self.unmet_penalty_per_mwh < 0 or self.interval_hours <= 0:
            raise ValueError("penalty must be >= 0 and interval_hours > 0")
        # --- finite-value guards (NaN / Inf are not caught by < or <=) ---
        if not math.isfinite(self.unmet_penalty_per_mwh):
            raise ValueError("unmet_penalty_per_mwh must be finite")
        if not math.isfinite(self.interval_hours):
            raise ValueError("interval_hours must be finite")
        for n in self.nodes:
            if not math.isfinite(n.demand_mw):
                raise ValueError(f"node {n.id} demand_mw must be finite, got {n.demand_mw}")
        for g in self.generators:
            if not math.isfinite(g.capacity_mw):
                raise ValueError(f"generator {g.id} capacity_mw must be finite, got {g.capacity_mw}")
            if not math.isfinite(g.cost_per_mwh):
                raise ValueError(f"generator {g.id} cost_per_mwh must be finite, got {g.cost_per_mwh}")
            if g.availability_mw is not None and not math.isfinite(g.availability_mw):
                raise ValueError(f"generator {g.id} availability_mw must be finite, got {g.availability_mw}")
            if g.ramp_up_mw is not None and not math.isfinite(g.ramp_up_mw):
                raise ValueError(f"generator {g.id} ramp_up_mw must be finite, got {g.ramp_up_mw}")
            if g.ramp_down_mw is not None and not math.isfinite(g.ramp_down_mw):
                raise ValueError(f"generator {g.id} ramp_down_mw must be finite, got {g.ramp_down_mw}")
        for ln in self.lines:
            if not math.isfinite(ln.capacity_mw):
                raise ValueError(f"line {ln.id} capacity_mw must be finite, got {ln.capacity_mw}")


    # ---- (de)serialisation ----------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "notes": self.notes,
            "unmet_penalty_per_mwh": self.unmet_penalty_per_mwh,
            "interval_hours": self.interval_hours,
            "nodes": [vars(n).copy() for n in self.nodes],
            "generators": [vars(g).copy() for g in self.generators],
            "lines": [vars(l).copy() for l in self.lines],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Network":
        try:
            net = Network(
                name=str(data["name"]),
                notes=str(data.get("notes", "")),
                unmet_penalty_per_mwh=float(data.get("unmet_penalty_per_mwh", DEFAULT_UNMET_PENALTY)),
                interval_hours=float(data.get("interval_hours", 1.0)),
                nodes=tuple(Node(**n) for n in data["nodes"]),
                generators=tuple(Generator(**g) for g in data["generators"]),
                lines=tuple(Line(**l) for l in data["lines"]),
            )
        except KeyError as exc:
            raise ValueError(f"network JSON is missing required field: {exc}") from exc
        except TypeError as exc:
            raise ValueError(f"network JSON has an unexpected or malformed field: {exc}") from exc
        net.validate()
        return net


def _find(items: tuple[Any, ...], item_id: str, label: str) -> Any:
    for item in items:
        if item.id == item_id:
            return item
    raise KeyError(f"unknown {label}: {item_id}")


def load_network(path: str | Path | None = None) -> Network:
    """Load a network from JSON (defaults to data/sample_network.json)."""
    with open(path or SAMPLE_NETWORK_PATH, encoding="utf-8") as handle:
        return Network.from_dict(json.load(handle))


def sample_network() -> Network:
    return load_network(SAMPLE_NETWORK_PATH)
