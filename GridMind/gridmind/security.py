"""Security-Constrained Preventive Scheduling (SCPS) data structures and contingency representations.

This module defines the Stage 5F data model for N-1 contingency representation,
SCPS problem definitions, monolithic optimization model construction, and solver execution.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from gridmind.network import Network
from gridmind.multiperiod import ScheduleProblem
from gridmind.scheduling import ScheduleResult, normalize_battery_mode
from gridmind.solver import SolverUnavailableError, resolve_solver, solver_name

CONTINGENCY_KINDS: tuple[str, ...] = ("generator", "line")
CLEAN_EPS = 1e-9


def _clean(x: float | None) -> float:
    if x is None:
        return 0.0
    return 0.0 if abs(x) < CLEAN_EPS else float(x)


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


# ----------------------------------------------------------------- result structures ----
@dataclass
class ContingencySchedule:
    """Corrective dispatch trajectory for one contingency."""
    contingency: Contingency
    dispatch: dict[str, list[float]] = field(default_factory=dict)
    flows: dict[str, list[float]] = field(default_factory=dict)
    unmet: dict[str, list[float]] = field(default_factory=dict)
    served: dict[str, list[float]] = field(default_factory=dict)
    charge: dict[str, list[float]] = field(default_factory=dict)
    discharge: dict[str, list[float]] = field(default_factory=dict)
    soc: dict[str, list[float]] = field(default_factory=dict)
    incremental_cost_usd: float | None = None
    redispatch_cost_usd: float | None = None
    unmet_penalty_cost_usd: float | None = None
    battery_throughput_cost_usd: float | None = None
    tiebreak_cost_usd: float | None = None
    unmet_energy_mwh: float | None = None
    is_secure: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = vars(self).copy()
        d["contingency"] = self.contingency.to_dict()
        return d


@dataclass
class SCPSResult:
    """Complete outcome of a Security-Constrained Preventive Scheduling solve."""
    status: str                         # "optimal", "infeasible", "error", "solver_unavailable", etc.
    solver: str
    termination_condition: str
    message: str
    runtime_seconds: float
    periods: int = 0
    step_hours: float = 0.0
    n1_secure: bool = False             # Provisional; authoritative verifier added in 5F.4
    strict: bool = False
    battery_mode: str = "lp"

    # Cost accounting
    total_objective_usd: float | None = None
    preventive_operating_cost_usd: float | None = None
    preventive_generation_cost_usd: float | None = None
    preventive_unmet_penalty_usd: float | None = None
    preventive_battery_throughput_usd: float | None = None
    preventive_tiebreak_usd: float | None = None

    corrective_redispatch_cost_usd: float | None = None
    contingency_unmet_penalty_usd: float | None = None
    corrective_battery_throughput_usd: float | None = None
    contingency_tiebreak_usd: float | None = None
    contingency_cost_usd: float | None = None  # Average incremental cost across contingencies

    # Schedules
    preventive_result: ScheduleResult | None = None
    contingencies: dict[str, ContingencySchedule] = field(default_factory=dict)
    validation: Any = None              # SCPSVerificationReport deferred to 5F.4

    @property
    def ok(self) -> bool:
        return self.status == "optimal" and (self.validation is None or getattr(self.validation, "passed", True))

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in vars(self).items() if k not in ("preventive_result", "contingencies", "validation")}
        d["preventive_result"] = self.preventive_result.to_dict() if self.preventive_result else None
        d["contingencies"] = {k: v.to_dict() for k, v in self.contingencies.items()}
        d["validation"] = self.validation.to_dict() if self.validation and hasattr(self.validation, "to_dict") else None
        d["ok"] = self.ok
        return d


# ----------------------------------------------------------------- model construction ----
def build_scps_model(scps_problem: SCPSProblem, *, allow_unmet: bool = True, battery_mode: str = "lp") -> Any:
    """Construct the monolithic Pyomo model for Security-Constrained Preventive Scheduling (SCPS).

    Formulation implements STAGE5F_SPEC.md (Revision 3) exactly:
    - Preventive variables p0, f0, u0, ch0, dis0, e0 (and binary z0 in exclusive mode).
    - Per-contingency recourse variables pk, fk, uk, chk, disk, ek (and binary zk in exclusive mode).
    - Common preventive schedule coupled across all contingencies.
    - Generator outages force output to 0.0 and exempt failed generator from downward corrective ramp.
    - Surviving generators respect corrective ramp limits relative to preventive dispatch at the same period.
    - Line outages force line flow to 0.0.
    - Batteries in contingency stage begin from shared initial SoC (E0_b); no terminal SoC in contingency stage.
    - Incremental corrective recourse objective with scale-invariant 1 / |K| equal contingency weighting.
    - Strict mode enforces u0 == 0 and uk == 0.
    """
    import pyomo.environ as pyo

    battery_mode = normalize_battery_mode(battery_mode)
    scps_problem.validate()

    prob = scps_problem.problem
    net = prob.network
    hor = prob.horizon
    T = list(range(prob.periods))
    dt = prob.step_hours
    nodes = [n.id for n in net.nodes]
    gens = [g.id for g in net.generators]
    lines = [l.id for l in net.lines]
    batts = [b.id for b in prob.batteries]
    gen_of = {g.id: g for g in net.generators}
    conts = [c.id for c in scps_problem.contingency_set]
    cont_of = {c.id: c for c in scps_problem.contingency_set}
    K_len = len(conts)

    # Preventive ramp index sets
    ramp_up = {g.id: g.ramp_up_mw for g in net.generators if g.ramp_up_mw is not None}
    ramp_down = {g.id: g.ramp_down_mw for g in net.generators if g.ramp_down_mw is not None}
    ramp_up_t = [(g, t) for g in ramp_up for t in T[1:]]
    ramp_down_t = [(g, t) for g in ramp_down for t in T[1:]]
    ramp_init_up = [(g, g0) for g in ramp_up for g0 in [prob.initial_dispatch_mw.get(g)] if g0 is not None]
    ramp_init_down = [(g, g0) for g in ramp_down for g0 in [prob.initial_dispatch_mw.get(g)] if g0 is not None]
    batt_of = {b.id: b for b in prob.batteries}
    with_final = [b.id for b in prob.batteries if b.final_soc_mwh is not None]

    gens_at = {n: [g.id for g in net.generators if g.node == n] for n in nodes}
    into = {n: [l.id for l in net.lines if l.to_node == n] for n in nodes}
    out_of = {n: [l.id for l in net.lines if l.from_node == n] for n in nodes}
    batts_at = {n: [b.id for b in prob.batteries if b.node == n] for n in nodes}

    m = pyo.ConcreteModel(name="GridMindSCPS")
    m.T = pyo.Set(initialize=T, ordered=True)
    m.N = pyo.Set(initialize=nodes, ordered=True)
    m.G = pyo.Set(initialize=gens, ordered=True)
    m.L = pyo.Set(initialize=lines, ordered=True)
    m.B = pyo.Set(initialize=batts, ordered=True)
    m.K = pyo.Set(initialize=conts, ordered=True)

    # Common parameters
    m.demand = pyo.Param(m.N, m.T, initialize=lambda mm, n, t: hor.demand(n, t))
    m.gen_limit = pyo.Param(m.G, m.T, initialize=lambda mm, g, t: hor.generation_limit(gen_of[g], t))
    m.line_cap = pyo.Param(m.L, m.T, initialize=lambda mm, l, t: hor.line_limit(net.line(l), t))

    # Battery parameters
    m.bat_cap = pyo.Param(m.B, initialize={b.id: b.capacity_mwh for b in prob.batteries})
    m.bat_min = pyo.Param(m.B, initialize={b.id: b.min_soc_mwh for b in prob.batteries})
    m.bat_pc = pyo.Param(m.B, initialize={b.id: b.max_charge_mw for b in prob.batteries})
    m.bat_pd = pyo.Param(m.B, initialize={b.id: b.max_discharge_mw for b in prob.batteries})
    m.bat_ec = pyo.Param(m.B, initialize={b.id: b.eta_charge for b in prob.batteries})
    m.bat_ed = pyo.Param(m.B, initialize={b.id: b.eta_discharge for b in prob.batteries})
    m.bat_soc0 = pyo.Param(m.B, initialize={b.id: b.initial_soc_mwh for b in prob.batteries})

    # =========================================================================
    # 1. Preventive stage variables and constraints
    # =========================================================================
    m.p0 = pyo.Var(m.G, m.T, domain=pyo.NonNegativeReals)
    m.f0 = pyo.Var(m.L, m.T, domain=pyo.Reals)
    m.u0 = pyo.Var(m.N, m.T, domain=pyo.NonNegativeReals)
    m.ch0 = pyo.Var(m.B, m.T, domain=pyo.NonNegativeReals)
    m.dis0 = pyo.Var(m.B, m.T, domain=pyo.NonNegativeReals)
    m.e0 = pyo.Var(m.B, m.T, domain=pyo.NonNegativeReals)

    m.gen_ub0 = pyo.Constraint(m.G, m.T, rule=lambda mm, g, t: mm.p0[g, t] <= mm.gen_limit[g, t])
    m.line_ub0 = pyo.Constraint(m.L, m.T, rule=lambda mm, l, t: mm.f0[l, t] <= mm.line_cap[l, t])
    m.line_lb0 = pyo.Constraint(m.L, m.T, rule=lambda mm, l, t: mm.f0[l, t] >= -mm.line_cap[l, t])

    m.ramp_up0 = pyo.Constraint(ramp_up_t, rule=lambda mm, g, t: mm.p0[g, t] - mm.p0[g, t - 1] <= ramp_up[g])
    m.ramp_down0 = pyo.Constraint(ramp_down_t, rule=lambda mm, g, t: mm.p0[g, t - 1] - mm.p0[g, t] <= ramp_down[g])
    m.ramp_init_up0 = pyo.Constraint(ramp_init_up, rule=lambda mm, g, g0: mm.p0[g, 0] - g0 <= ramp_up[g])
    m.ramp_init_down0 = pyo.Constraint(ramp_init_down, rule=lambda mm, g, g0: g0 - mm.p0[g, 0] <= ramp_down[g])

    m.charge_ub0 = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.ch0[b, t] <= mm.bat_pc[b])
    m.discharge_ub0 = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.dis0[b, t] <= mm.bat_pd[b])
    m.soc_ub0 = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.e0[b, t] <= mm.bat_cap[b])
    m.soc_lb0 = pyo.Constraint(m.B, m.T, rule=lambda mm, b, t: mm.e0[b, t] >= mm.bat_min[b])

    if battery_mode == "exclusive" and batts:
        m.z0 = pyo.Var(m.B, m.T, domain=pyo.Binary)
        m.exclusive_charge0 = pyo.Constraint(
            m.B, m.T, rule=lambda mm, b, t: mm.ch0[b, t] <= mm.bat_pc[b] * (1 - mm.z0[b, t])
        )
        m.exclusive_discharge0 = pyo.Constraint(
            m.B, m.T, rule=lambda mm, b, t: mm.dis0[b, t] <= mm.bat_pd[b] * mm.z0[b, t]
        )

    def soc_step0(mm: Any, b: str, t: int) -> Any:
        prev = mm.bat_soc0[b] if t == 0 else mm.e0[b, t - 1]
        return mm.e0[b, t] == prev + mm.bat_ec[b] * mm.ch0[b, t] * dt - mm.dis0[b, t] * dt / mm.bat_ed[b]

    m.soc_dyn0 = pyo.Constraint(m.B, m.T, rule=soc_step0)
    m.soc_final0 = pyo.Constraint(
        with_final, rule=lambda mm, b: mm.e0[b, prob.periods - 1] == batt_of[b].final_soc_mwh
    )

    def balance0(mm: Any, n: str, t: int) -> Any:
        return (
            sum(mm.p0[g, t] for g in gens_at[n])
            + sum(mm.f0[l, t] for l in into[n]) - sum(mm.f0[l, t] for l in out_of[n])
            + sum(mm.dis0[b, t] for b in batts_at[n]) - sum(mm.ch0[b, t] for b in batts_at[n])
            + mm.u0[n, t]
        ) == mm.demand[n, t]

    m.balance0 = pyo.Constraint(m.N, m.T, rule=balance0)
    if allow_unmet:
        m.unmet_ub0 = pyo.Constraint(m.N, m.T, rule=lambda mm, n, t: mm.u0[n, t] <= mm.demand[n, t])
    else:
        m.unmet_ub0 = pyo.Constraint(m.N, m.T, rule=lambda mm, n, t: mm.u0[n, t] == 0.0)

    # =========================================================================
    # 2. Contingency recourse variables and constraints
    # =========================================================================
    m.pk = pyo.Var(m.K, m.G, m.T, domain=pyo.NonNegativeReals)
    m.fk = pyo.Var(m.K, m.L, m.T, domain=pyo.Reals)
    m.uk = pyo.Var(m.K, m.N, m.T, domain=pyo.NonNegativeReals)
    m.chk = pyo.Var(m.K, m.B, m.T, domain=pyo.NonNegativeReals)
    m.disk = pyo.Var(m.K, m.B, m.T, domain=pyo.NonNegativeReals)
    m.ek = pyo.Var(m.K, m.B, m.T, domain=pyo.NonNegativeReals)

    # Generator limits under contingency
    def gen_ub_k(mm: Any, k: str, g: str, t: int) -> Any:
        c = cont_of[k]
        if c.kind == "generator" and c.component_id == g:
            return mm.pk[k, g, t] == 0.0
        return mm.pk[k, g, t] <= mm.gen_limit[g, t]

    m.gen_ub_k = pyo.Constraint(m.K, m.G, m.T, rule=gen_ub_k)

    # Line flow limits under contingency
    def line_ub_k(mm: Any, k: str, l: str, t: int) -> Any:
        c = cont_of[k]
        if c.kind == "line" and c.component_id == l:
            return mm.fk[k, l, t] == 0.0
        return mm.fk[k, l, t] <= mm.line_cap[l, t]

    def line_lb_k(mm: Any, k: str, l: str, t: int) -> Any:
        c = cont_of[k]
        if c.kind == "line" and c.component_id == l:
            return mm.fk[k, l, t] == 0.0
        return mm.fk[k, l, t] >= -mm.line_cap[l, t]

    m.line_ub_k = pyo.Constraint(m.K, m.L, m.T, rule=line_ub_k)
    m.line_lb_k = pyo.Constraint(m.K, m.L, m.T, rule=line_lb_k)

    # Corrective ramp limits (failed generator is EXEMPT)
    corr_ramp_up = [
        (k, g, t) for k in conts for g in ramp_up for t in T
        if not (cont_of[k].kind == "generator" and cont_of[k].component_id == g)
    ]
    corr_ramp_down = [
        (k, g, t) for k in conts for g in ramp_down for t in T
        if not (cont_of[k].kind == "generator" and cont_of[k].component_id == g)
    ]

    m.corr_ramp_up = pyo.Constraint(
        corr_ramp_up, rule=lambda mm, k, g, t: mm.pk[k, g, t] - mm.p0[g, t] <= ramp_up[g]
    )
    m.corr_ramp_down = pyo.Constraint(
        corr_ramp_down, rule=lambda mm, k, g, t: mm.p0[g, t] - mm.pk[k, g, t] <= ramp_down[g]
    )

    # Battery limits under contingency (no terminal SoC constraint in contingency)
    m.charge_ub_k = pyo.Constraint(m.K, m.B, m.T, rule=lambda mm, k, b, t: mm.chk[k, b, t] <= mm.bat_pc[b])
    m.discharge_ub_k = pyo.Constraint(m.K, m.B, m.T, rule=lambda mm, k, b, t: mm.disk[k, b, t] <= mm.bat_pd[b])
    m.soc_ub_k = pyo.Constraint(m.K, m.B, m.T, rule=lambda mm, k, b, t: mm.ek[k, b, t] <= mm.bat_cap[b])
    m.soc_lb_k = pyo.Constraint(m.K, m.B, m.T, rule=lambda mm, k, b, t: mm.ek[k, b, t] >= mm.bat_min[b])

    if battery_mode == "exclusive" and batts:
        m.zk = pyo.Var(m.K, m.B, m.T, domain=pyo.Binary)
        m.exclusive_charge_k = pyo.Constraint(
            m.K, m.B, m.T, rule=lambda mm, k, b, t: mm.chk[k, b, t] <= mm.bat_pc[b] * (1 - mm.zk[k, b, t])
        )
        m.exclusive_discharge_k = pyo.Constraint(
            m.K, m.B, m.T, rule=lambda mm, k, b, t: mm.disk[k, b, t] <= mm.bat_pd[b] * mm.zk[k, b, t]
        )

    def soc_step_k(mm: Any, k: str, b: str, t: int) -> Any:
        prev = mm.bat_soc0[b] if t == 0 else mm.ek[k, b, t - 1]
        return mm.ek[k, b, t] == prev + mm.bat_ec[b] * mm.chk[k, b, t] * dt - mm.disk[k, b, t] * dt / mm.bat_ed[b]

    m.soc_dyn_k = pyo.Constraint(m.K, m.B, m.T, rule=soc_step_k)

    def balance_k(mm: Any, k: str, n: str, t: int) -> Any:
        return (
            sum(mm.pk[k, g, t] for g in gens_at[n])
            + sum(mm.fk[k, l, t] for l in into[n]) - sum(mm.fk[k, l, t] for l in out_of[n])
            + sum(mm.disk[k, b, t] for b in batts_at[n]) - sum(mm.chk[k, b, t] for b in batts_at[n])
            + mm.uk[k, n, t]
        ) == mm.demand[n, t]

    m.balance_k = pyo.Constraint(m.K, m.N, m.T, rule=balance_k)
    if allow_unmet:
        m.unmet_ub_k = pyo.Constraint(m.K, m.N, m.T, rule=lambda mm, k, n, t: mm.uk[k, n, t] <= mm.demand[n, t])
    else:
        m.unmet_ub_k = pyo.Constraint(m.K, m.N, m.T, rule=lambda mm, k, n, t: mm.uk[k, n, t] == 0.0)

    # =========================================================================
    # 3. Monolithic Objective Function (Incremental Recourse Formulation)
    # =========================================================================
    penalty = net.unmet_penalty_per_mwh
    thr_cost = {b.id: b.throughput_cost_per_mwh for b in prob.batteries}
    eps = prob.tiebreak_epsilon

    # Base preventive cost
    c_prev = dt * (
        sum(net.generator(g).cost_per_mwh * m.p0[g, t] for g in m.G for t in m.T)
        + penalty * sum(m.u0[n, t] for n in m.N for t in m.T)
        + sum(thr_cost[b] * m.dis0[b, t] for b in m.B for t in m.T)
    ) + eps * dt * sum(m.ch0[b, t] + m.dis0[b, t] for b in m.B for t in m.T)

    # Incremental corrective recourse cost per contingency
    def delta_c(k: str) -> Any:
        redispatch_expr = dt * sum(net.generator(g).cost_per_mwh * (m.pk[k, g, t] - m.p0[g, t]) for g in m.G for t in m.T)
        unmet_expr = dt * penalty * sum(m.uk[k, n, t] for n in m.N for t in m.T)
        throughput_expr = dt * sum(thr_cost[b] * (m.disk[k, b, t] - m.dis0[b, t]) for b in m.B for t in m.T)
        tiebreak_expr = eps * dt * sum((m.chk[k, b, t] + m.disk[k, b, t]) - (m.ch0[b, t] + m.dis0[b, t]) for b in m.B for t in m.T)
        return redispatch_expr + unmet_expr + throughput_expr + tiebreak_expr

    c_cont_incr = (1.0 / K_len) * sum(delta_c(k) for k in conts)

    m.total_cost = pyo.Objective(expr=c_prev + c_cont_incr, sense=pyo.minimize)

    if battery_mode != "exclusive":
        m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)

    return m


# ----------------------------------------------------------------- solver execution ----
def _scps_failure(
    status: str, solver: str, term: str, msg: str, runtime: float,
    scps_problem: SCPSProblem, strict: bool, battery_mode: str = "lp"
) -> SCPSResult:
    return SCPSResult(
        status=status, solver=solver, termination_condition=term, message=msg,
        runtime_seconds=runtime, periods=scps_problem.periods, step_hours=scps_problem.step_hours,
        n1_secure=False, strict=strict, battery_mode=battery_mode
    )


def solve_security_constrained(
    scps_problem: SCPSProblem,
    *,
    allow_unmet: bool = True,
    battery_mode: str = "lp",
) -> SCPSResult:
    """Build and solve the monolithic Security-Constrained Preventive Scheduling problem.

    Supports:
    - ``allow_unmet=True`` (penalized mode) vs ``allow_unmet=False`` (strict security mode).
    - ``battery_mode="lp"`` (continuous relaxation) vs ``"exclusive"`` (MIP exclusivity).
    """
    strict = not allow_unmet
    try:
        battery_mode = normalize_battery_mode(battery_mode)
    except ValueError as exc:
        return _scps_failure("error", solver_name(), "invalid_battery_mode", str(exc), 0.0, scps_problem, strict, "lp")

    mip = battery_mode == "exclusive"
    try:
        name, solver = resolve_solver(mip=mip)
    except SolverUnavailableError as exc:
        return _scps_failure("solver_unavailable", solver_name(), "not_run", str(exc), 0.0, scps_problem, strict, battery_mode)

    import pyomo.environ as pyo
    from pyomo.opt import SolverStatus, TerminationCondition

    try:
        model = build_scps_model(scps_problem, allow_unmet=allow_unmet, battery_mode=battery_mode)
    except ValueError as exc:
        return _scps_failure("error", name, "model_build_failed", f"invalid SCPS problem: {exc}", 0.0, scps_problem, strict, battery_mode)

    t0 = time.perf_counter()
    try:
        results = solver.solve(model, load_solutions=False, tee=False)
    except Exception as exc:
        return _scps_failure("error", name, "solver_exception", f"{type(exc).__name__}: {exc}", time.perf_counter() - t0, scps_problem, strict, battery_mode)
    runtime = time.perf_counter() - t0

    term = results.solver.termination_condition
    term_s = str(term)
    if term != TerminationCondition.optimal or results.solver.status not in (SolverStatus.ok, SolverStatus.warning):
        status = {
            TerminationCondition.infeasible: "infeasible",
            TerminationCondition.unbounded: "unbounded",
            TerminationCondition.infeasibleOrUnbounded: "infeasible_or_unbounded",
        }.get(term, "error")
        msg = {
            "infeasible": "SCPS model is infeasible: no joint schedule satisfies all preventive and contingency constraints",
            "unbounded": "SCPS model is unbounded (check network and contingency data)",
        }.get(status, f"solver did not reach optimality (status={results.solver.status}, termination={term_s})")
        return _scps_failure(status, name, term_s, msg, runtime, scps_problem, strict, battery_mode)

    T = list(range(scps_problem.periods))
    net = scps_problem.network
    dt = scps_problem.step_hours
    penalty = net.unmet_penalty_per_mwh
    thr_cost = {b.id: b.throughput_cost_per_mwh for b in scps_problem.problem.batteries}
    eps = scps_problem.problem.tiebreak_epsilon
    conts = [c.id for c in scps_problem.contingency_set]
    cont_of = {c.id: c for c in scps_problem.contingency_set}
    K_len = len(conts)

    try:
        model.solutions.load_from(results)

        # 1. Extract preventive schedule
        p_disp = {g: [_clean(pyo.value(model.p0[g, t], exception=False)) for t in T] for g in model.G}
        p_flows = {l: [_clean(pyo.value(model.f0[l, t], exception=False)) for t in T] for l in model.L}
        p_unmet = {n: [_clean(pyo.value(model.u0[n, t], exception=False)) for t in T] for n in model.N}
        p_charge = {b: [_clean(pyo.value(model.ch0[b, t], exception=False)) for t in T] for b in model.B}
        p_dis = {b: [_clean(pyo.value(model.dis0[b, t], exception=False)) for t in T] for b in model.B}
        p_soc = {b: [_clean(pyo.value(model.e0[b, t], exception=False)) for t in T] for b in model.B}

        total_obj_val = pyo.value(model.total_cost, exception=False)

        # 2. Extract contingency schedules
        c_schedules: dict[str, ContingencySchedule] = {}
        for k in conts:
            disp_k = {g: [_clean(pyo.value(model.pk[k, g, t], exception=False)) for t in T] for g in model.G}
            flows_k = {l: [_clean(pyo.value(model.fk[k, l, t], exception=False)) for t in T] for l in model.L}
            unmet_k = {n: [_clean(pyo.value(model.uk[k, n, t], exception=False)) for t in T] for n in model.N}
            charge_k = {b: [_clean(pyo.value(model.chk[k, b, t], exception=False)) for t in T] for b in model.B}
            dis_k = {b: [_clean(pyo.value(model.disk[k, b, t], exception=False)) for t in T] for b in model.B}
            soc_k = {b: [_clean(pyo.value(model.ek[k, b, t], exception=False)) for t in T] for b in model.B}
            served_k = {n: [_clean(scps_problem.problem.horizon.demand(n, t) - unmet_k[n][t]) for t in T] for n in model.N}

            # Per-contingency incremental cost breakdown
            redisp_cost = dt * sum(net.generator(g).cost_per_mwh * (disp_k[g][t] - p_disp[g][t]) for g in model.G for t in T)
            unmet_pen = dt * penalty * sum(unmet_k[n][t] for n in model.N for t in T)
            batt_cost = dt * sum(thr_cost[b] * (dis_k[b][t] - p_dis[b][t]) for b in model.B for t in T)
            tb_cost = eps * dt * sum((charge_k[b][t] + dis_k[b][t]) - (p_charge[b][t] + p_dis[b][t]) for b in model.B for t in T)
            unmet_en = dt * sum(sum(series) for series in unmet_k.values())
            is_sec = unmet_en <= CLEAN_EPS

            c_schedules[k] = ContingencySchedule(
                contingency=cont_of[k],
                dispatch=disp_k,
                flows=flows_k,
                unmet=unmet_k,
                served=served_k,
                charge=charge_k,
                discharge=dis_k,
                soc=soc_k,
                incremental_cost_usd=_clean(redisp_cost + unmet_pen + batt_cost + tb_cost),
                redispatch_cost_usd=_clean(redisp_cost),
                unmet_penalty_cost_usd=_clean(unmet_pen),
                battery_throughput_cost_usd=_clean(batt_cost),
                tiebreak_cost_usd=_clean(tb_cost),
                unmet_energy_mwh=_clean(unmet_en),
                is_secure=is_sec,
            )

    except Exception as exc:
        return _scps_failure("error", name, term_s, f"could not extract solution: {exc}", runtime, scps_problem, strict, battery_mode)

    # Preventive schedule cost calculations
    prev_gen_cost = dt * sum(net.generator(g).cost_per_mwh * p_disp[g][t] for g in model.G for t in T)
    prev_unmet_pen = dt * penalty * sum(p_unmet[n][t] for n in model.N for t in T)
    prev_batt_cost = dt * sum(thr_cost[b] * p_dis[b][t] for b in model.B for t in T)
    prev_tb_cost = eps * dt * sum(p_charge[b][t] + p_dis[b][t] for b in model.B for t in T)
    prev_op_cost = prev_gen_cost + prev_unmet_pen + prev_batt_cost + prev_tb_cost

    p_unmet_en = dt * sum(sum(s) for s in p_unmet.values())
    p_served = {n: [_clean(scps_problem.problem.horizon.demand(n, t) - p_unmet[n][t]) for t in T] for n in model.N}
    tot_demand = scps_problem.problem.total_demand_mwh
    max_p_unmet = max((max(row, default=0.0) for row in p_unmet.values()), default=0.0)
    max_p_periods = tuple(t for t in T if any(p_unmet[n][t] > CLEAN_EPS for n in p_unmet))

    prev_res = ScheduleResult(
        status="optimal", solver=name, termination_condition=term_s,
        message="optimal preventive schedule found",
        runtime_seconds=runtime, periods=scps_problem.periods, step_hours=dt,
        objective=_clean(prev_op_cost),
        operating_cost_usd=_clean(prev_op_cost - prev_tb_cost),
        generation_cost_usd=_clean(prev_gen_cost),
        unmet_penalty_cost_usd=_clean(prev_unmet_pen),
        battery_throughput_cost_usd=_clean(prev_batt_cost),
        tiebreak_cost_usd=_clean(prev_tb_cost),
        total_demand_mwh=tot_demand, unmet_energy_mwh=_clean(p_unmet_en),
        served_demand_mwh=_clean(tot_demand - p_unmet_en),
        demand_served_pct=_clean(100.0 * (tot_demand - p_unmet_en) / tot_demand) if tot_demand > 0 else 100.0,
        max_unmet_mw=_clean(max_p_unmet), max_unmet_periods=max_p_periods,
        dispatch=p_disp, flows=p_flows, unmet=p_unmet, served=p_served,
        charge=p_charge, discharge=p_dis, soc=p_soc,
        cost_by_period_usd=[], validation=None, strict=strict, battery_mode=battery_mode
    )

    # Average incremental costs across contingencies
    avg_redisp = (1.0 / K_len) * sum(cs.redispatch_cost_usd or 0.0 for cs in c_schedules.values())
    avg_unmet_pen = (1.0 / K_len) * sum(cs.unmet_penalty_cost_usd or 0.0 for cs in c_schedules.values())
    avg_batt_cost = (1.0 / K_len) * sum(cs.battery_throughput_cost_usd or 0.0 for cs in c_schedules.values())
    avg_tb_cost = (1.0 / K_len) * sum(cs.tiebreak_cost_usd or 0.0 for cs in c_schedules.values())
    avg_incr_total = avg_redisp + avg_unmet_pen + avg_batt_cost + avg_tb_cost

    # Overall N-1 security independently verified via verify_scps_result
    from gridmind.security_validation import verify_scps_result

    raw_result = SCPSResult(
        status="optimal", solver=name, termination_condition=term_s,
        message="optimal solution found",
        runtime_seconds=runtime, periods=scps_problem.periods, step_hours=dt,
        n1_secure=False, strict=strict, battery_mode=battery_mode,
        total_objective_usd=_clean(total_obj_val),
        preventive_operating_cost_usd=_clean(prev_op_cost - prev_tb_cost),
        preventive_generation_cost_usd=_clean(prev_gen_cost),
        preventive_unmet_penalty_usd=_clean(prev_unmet_pen),
        preventive_battery_throughput_usd=_clean(prev_batt_cost),
        preventive_tiebreak_usd=_clean(prev_tb_cost),
        corrective_redispatch_cost_usd=_clean(avg_redisp),
        contingency_unmet_penalty_usd=_clean(avg_unmet_pen),
        corrective_battery_throughput_usd=_clean(avg_batt_cost),
        contingency_tiebreak_usd=_clean(avg_tb_cost),
        contingency_cost_usd=_clean(avg_incr_total),
        preventive_result=prev_res,
        contingencies=c_schedules,
        validation=None
    )

    val_report = verify_scps_result(scps_problem, raw_result, tol=1e-5, battery_mode=battery_mode)
    raw_result.validation = val_report
    raw_result.n1_secure = val_report.n1_secure
    return raw_result
