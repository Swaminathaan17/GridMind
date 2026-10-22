"""Shared test helpers (plain functions, no fixtures)."""
from __future__ import annotations

from gridmind.network import Generator, Line, Network, Node
from gridmind.optimizer import solver_available

SOLVER_OK, SOLVER_MSG = solver_available()


def tiny_network(line_cap: float = 30.0, demand: float = 50.0, penalty: float = 1000.0) -> Network:
    """A -- B. Cheap gen (10 USD/MWh, 100 MW) at A, demand at B, one line A->B."""
    return Network(
        name="tiny",
        nodes=(Node("A", "A"), Node("B", "B", demand)),
        generators=(Generator("cheap", "Cheap", "A", "gas", 100.0, 10.0),),
        lines=(Line("AB", "A-B", "A", "B", line_cap),),
        unmet_penalty_per_mwh=penalty)


def two_gen_network() -> Network:
    """Cheap remote gen limited by the line + expensive local gen at B."""
    return Network(
        name="two-gen",
        nodes=(Node("A", "A"), Node("B", "B", 80.0)),
        generators=(Generator("cheap", "Cheap", "A", "gas", 100.0, 10.0),
                    Generator("local", "Local", "B", "gas", 100.0, 50.0)),
        lines=(Line("AB", "A-B", "A", "B", 30.0),),
        unmet_penalty_per_mwh=1000.0)
