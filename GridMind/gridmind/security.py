"""Security-Constrained Preventive Scheduling (SCPS) data structures and contingency representations.

This module defines the Stage 5F data model for N-1 contingency representation
and SCPS problem definitions. It does NOT construct optimization models or solve
them; that capability lives in milestone 5F.3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from gridmind.network import Network
from gridmind.multiperiod import ScheduleProblem

CONTINGENCY_KINDS: tuple[str, ...] = ("generator", "line")


@dataclass(frozen=True)
class Contingency:
    """A single N-1 contingency representing an outage of one network component.

    Attributes:
        id: Unique identifier for this contingency (e.g. "gen:gas-north", "line:line-1").
        kind: Type of component outaged ("generator" or "line").
        component_id: The ID of the generator or line that fails.
        name: Optional human-readable description.
    """
    id: str
    kind: str
    component_id: str
    name: str = ""

    def validate(self, network: Network) -> None:
        """Validate component identity, existence, and kind against a Network.

        Raises ValueError or KeyError if kind is unsupported or component does not exist.
        """
        if not self.id or not isinstance(self.id, str):
            raise ValueError(f"Contingency id must be a non-empty string, got {self.id!r}")
        if self.kind not in CONTINGENCY_KINDS:
            raise ValueError(f"Invalid contingency kind: {self.kind!r}; must be one of {CONTINGENCY_KINDS}")
        if not self.component_id or not isinstance(self.component_id, str):
            raise ValueError(f"Contingency component_id must be a non-empty string, got {self.component_id!r}")

        if self.kind == "generator":
            try:
                network.generator(self.component_id)
            except KeyError as exc:
                raise KeyError(f"Contingency '{self.id}' references unknown generator '{self.component_id}'") from exc
        elif self.kind == "line":
            try:
                network.line(self.component_id)
            except KeyError as exc:
                raise KeyError(f"Contingency '{self.id}' references unknown line '{self.component_id}'") from exc

    def to_dict(self) -> dict[str, Any]:
        """Serialize contingency to a JSON-compatible dictionary."""
        return {
            "id": self.id,
            "kind": self.kind,
            "component_id": self.component_id,
            "name": self.name,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Contingency:
        """Deserialize contingency from dictionary."""
        try:
            return Contingency(
                id=str(data["id"]),
                kind=str(data["kind"]),
                component_id=str(data["component_id"]),
                name=str(data.get("name", "")),
            )
        except KeyError as exc:
            raise ValueError(f"Contingency data missing required key: {exc}") from exc
        except TypeError as exc:
            raise ValueError(f"Contingency data malformed: {exc}") from exc


@dataclass(frozen=True)
class ContingencySet:
    """A validated collection of N-1 contingencies."""
    contingencies: tuple[Contingency, ...] = ()

    def validate(self, network: Network) -> None:
        """Validate all contingencies and check for duplicate IDs or repeated element outages.

        Raises ValueError on duplicate contingency IDs, duplicate element outages,
        or invalid element references.
        """
        seen_ids: set[str] = set()
        seen_components: set[tuple[str, str]] = set()

        for c in self.contingencies:
            if c.id in seen_ids:
                raise ValueError(f"Duplicate contingency id '{c.id}' in ContingencySet")
            seen_ids.add(c.id)

            comp_key = (c.kind, c.component_id)
            if comp_key in seen_components:
                raise ValueError(
                    f"Duplicate outage for {c.kind} '{c.component_id}' in ContingencySet (contingency '{c.id}')"
                )
            seen_components.add(comp_key)

            c.validate(network)

    def __len__(self) -> int:
        return len(self.contingencies)

    def __iter__(self):
        return iter(self.contingencies)

    @classmethod
    def auto_n_1(
        cls,
        network: Network,
        include_generators: bool = True,
        include_lines: bool = True,
    ) -> ContingencySet:
        """Generate canonical N-1 contingency set for all eligible in-service components.

        Consistent with GridMind's network outage modeling:
        - Only generators with online=True are eligible for N-1 outages.
        - Only lines with in_service=True are eligible for N-1 outages.
        """
        network.validate()
        contingency_list: list[Contingency] = []

        if include_generators:
            for g in network.generators:
                if g.online:
                    contingency_list.append(
                        Contingency(
                            id=f"gen:{g.id}",
                            kind="generator",
                            component_id=g.id,
                            name=f"N-1 generator outage of {g.name} ({g.id})",
                        )
                    )

        if include_lines:
            for l in network.lines:
                if l.in_service:
                    contingency_list.append(
                        Contingency(
                            id=f"line:{l.id}",
                            kind="line",
                            component_id=l.id,
                            name=f"N-1 line outage of {l.name} ({l.id})",
                        )
                    )

        c_set = cls(contingencies=tuple(contingency_list))
        c_set.validate(network)
        return c_set

    def to_dict(self) -> dict[str, Any]:
        """Serialize contingency set to dictionary."""
        return {
            "contingencies": [c.to_dict() for c in self.contingencies]
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ContingencySet:
        """Deserialize contingency set from dictionary."""
        try:
            items = data.get("contingencies", [])
            return ContingencySet(contingencies=tuple(Contingency.from_dict(c) for c in items))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Malformed contingency set data: {exc}") from exc


@dataclass(frozen=True)
class SCPSProblem:
    """Security-Constrained Preventive Scheduling Problem definition.

    Couples a standard multi-period ScheduleProblem with a validated ContingencySet.
    Consistent with Stage 5F.1 specifications:
    - Contingency weights are strictly fixed to equal weights (1 / len(contingency_set))
      as an internal scale-invariant optimization convention; custom weights are not accepted.
    - An empty contingency set is rejected when N-1 security assessment is requested.
    """
    problem: ScheduleProblem
    contingency_set: ContingencySet
    notes: str = ""

    def validate(self) -> None:
        """Validate problem and non-empty contingency set."""
        self.problem.validate()
        if len(self.contingency_set) == 0:
            raise ValueError("SCPSProblem requires a non-empty contingency_set for N-1 security assessment")
        self.contingency_set.validate(self.problem.network)

    @property
    def network(self) -> Network:
        return self.problem.network

    @property
    def periods(self) -> int:
        return self.problem.periods

    @property
    def step_hours(self) -> float:
        return self.problem.step_hours

    def to_dict(self) -> dict[str, Any]:
        """Serialize SCPS problem definition to dictionary."""
        problem_dict = self.problem.to_dict()
        problem_dict["network"] = self.problem.network.to_dict()
        return {
            "problem": problem_dict,
            "contingency_set": self.contingency_set.to_dict(),
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any], *, network: Network | None = None) -> SCPSProblem:
        """Deserialize SCPS problem definition from dictionary."""
        try:
            problem_data = dict(data["problem"])
            if network is not None and "network" not in problem_data:
                problem_data["network"] = network.to_dict()
            problem = ScheduleProblem.from_dict(problem_data, network=network)
            c_set = ContingencySet.from_dict(data["contingency_set"])
            notes = str(data.get("notes", ""))
            scps_prob = SCPSProblem(problem=problem, contingency_set=c_set, notes=notes)
            scps_prob.validate()
            return scps_prob
        except KeyError as exc:
            raise ValueError(f"SCPSProblem data missing required key: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Malformed SCPSProblem data: {exc}") from exc
