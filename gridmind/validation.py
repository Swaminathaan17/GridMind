"""Independent validation of a returned solution.

Deliberately does NOT use Pyomo: it re-derives every constraint and the
objective directly from the Network data, so a modelling bug or a sloppy solver
termination cannot silently pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Mapping, Sequence

from gridmind.network import Network

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gridmind.multiperiod import ScheduleProblem

DEFAULT_TOL = 1e-5  # MW (absolute); objective uses relative tolerance


@dataclass
class Check:
    name: str
    passed: bool
    max_violation: float
    detail: str = ""
    severity: str = "error"      # "error" (fails validation) or "warning" (advisory)
    category: str = "physical"   # "physical" | "model" | "exclusivity"


@dataclass
class ValidationReport:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    physical_feasible: bool | None = None   # None => not evaluated
    model_valid: bool | None = None         # None => not evaluated
    battery_exclusive: bool | None = None   # None => not evaluated

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "warning" and not c.passed]

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "checks": [vars(c).copy() for c in self.checks],
                "physical_feasible": self.physical_feasible,
                "model_valid": self.model_valid,
                "battery_exclusive": self.battery_exclusive}


def _report(checks: list[Check], *, evaluate_exclusivity: bool) -> ValidationReport:
    """Build a report, preserving legacy ``passed`` while adding facets.

    ``passed`` is exactly ``all(c.passed for c in checks)`` (empty list -> True),
    identical to the previous construction. The facets summarise checks by
    ``category``. ``battery_exclusive`` is populated only when
    ``evaluate_exclusivity`` is true; otherwise it stays ``None`` and an empty
    exclusivity category is never inferred to be a success.
    """
    physical = [c for c in checks if c.category == "physical"]
    model = [c for c in checks if c.category == "model"]
    exclusivity = [c for c in checks if c.category == "exclusivity"]
    return ValidationReport(
        passed=all(c.passed for c in checks),
        checks=checks,
        physical_feasible=all(c.passed for c in physical),
        model_valid=all(c.passed for c in model),
        battery_exclusive=(all(c.passed for c in exclusivity) if evaluate_exclusivity else None))


def recompute_objective(network: Network, dispatch: Mapping[str, float], unmet: Mapping[str, float]) -> tuple[float, float]:
    """Return (generation_cost, unmet_penalty_cost) in USD for the interval."""
    h = network.interval_hours
    gen_cost = h * sum(g.cost_per_mwh * dispatch[g.id] for g in network.generators)
    penalty = h * network.unmet_penalty_per_mwh * sum(unmet.values())
    return gen_cost, penalty


def validate_solution(
    network: Network,
    dispatch: Mapping[str, float],
    flows: Mapping[str, float],
    unmet: Mapping[str, float],
    reported_objective: float | None = None,
    tol: float = DEFAULT_TOL,
    allow_unmet: bool = True,
) -> ValidationReport:
    checks: list[Check] = []

    def add(name: str, violation: float, detail: str = "", category: str = "physical") -> None:
        checks.append(Check(name, violation <= tol, float(max(violation, 0.0)), detail,
                            category=category))

    missing = (
        [g.id for g in network.generators if g.id not in dispatch]
        + [l.id for l in network.lines if l.id not in flows]
        + [n.id for n in network.nodes if n.id not in unmet]
    )
    checks.append(Check("completeness", not missing, float(len(missing)),
                        f"missing values for: {missing}" if missing else ""))
    if missing:
        return _report(checks, evaluate_exclusivity=False)

    add("generator_limits", max((max(-dispatch[g.id], dispatch[g.id] - (g.effective_limit_mw if g.online else 0.0))
                                 for g in network.generators), default=0.0),
        "0 <= p <= min(capacity, availability) (0 when generator is out)")
    add("availability_limits", max((dispatch[g.id] - g.available_mw for g in network.generators if g.online),
                                   default=0.0), "p <= availability for every in-service generator")
    add("line_capacity", max((abs(flows[l.id]) - l.effective_capacity_mw for l in network.lines), default=0.0),
        "|flow| <= capacity (0 when line is out)")
    add("outages_respected", max(
        [abs(flows[l.id]) for l in network.lines if not l.in_service]
        + [dispatch[g.id] for g in network.generators if not g.online] + [0.0]),
        "out-of-service elements carry no flow / output")
    add("unmet_bounds", max((max(-unmet[n.id], unmet[n.id] - n.demand_mw) for n in network.nodes), default=0.0),
        "0 <= unmet <= demand")
    if not allow_unmet:
        add("no_unmet_in_strict_mode", max(unmet.values(), default=0.0), "strict mode forbids unmet demand")

    worst = 0.0
    for n in network.nodes:
        gen = sum(dispatch[g.id] for g in network.generators if g.node == n.id)
        inflow = sum(flows[l.id] for l in network.lines if l.to_node == n.id)
        outflow = sum(flows[l.id] for l in network.lines if l.from_node == n.id)
        worst = max(worst, abs(gen + inflow - outflow - (n.demand_mw - unmet[n.id])))
    add("power_balance", worst, "generation + inflow - outflow = demand - unmet at every node")

    if reported_objective is not None:
        gen_cost, penalty = recompute_objective(network, dispatch, unmet)
        diff = abs(gen_cost + penalty - reported_objective)
        rel = diff / max(1.0, abs(reported_objective))
        checks.append(Check("objective_consistency", rel <= 1e-6, float(diff),
                            f"recomputed {gen_cost + penalty:.6f} vs reported {reported_objective:.6f}",
                            category="model"))
    return _report(checks, evaluate_exclusivity=False)


# ------------------------------------------------- multi-period validation ----
def recompute_schedule_costs(problem: "ScheduleProblem", dispatch: Mapping[str, list[float]],
                             unmet: Mapping[str, list[float]],
                             charge: Mapping[str, list[float]],
                             discharge: Mapping[str, list[float]]) -> dict[str, float]:
    """Recompute every cost component from the problem data and the raw arrays.

    Returns USD: generation, unmet penalty, battery throughput, the epsilon
    tie-break term and their sum (the pure operating cost excludes the tie-break).
    """
    net, dt = problem.network, problem.step_hours
    penalty = net.unmet_penalty_per_mwh
    gen = dt * sum(net.generator(g).cost_per_mwh * v for g, series in dispatch.items() for v in series)
    pen = dt * penalty * sum(v for series in unmet.values() for v in series)
    thr = dt * sum(problem.battery(b).throughput_cost_per_mwh * v
                   for b, series in discharge.items() for v in series)
    tie = dt * problem.tiebreak_epsilon * (sum(v for series in charge.values() for v in series)
                                           + sum(v for series in discharge.values() for v in series))
    return {"generation_cost_usd": gen, "unmet_penalty_cost_usd": pen,
            "battery_throughput_cost_usd": thr, "tiebreak_cost_usd": tie,
            "operating_cost_usd": gen + pen + thr}


def validate_schedule(problem: "ScheduleProblem", dispatch, flows, unmet, charge, discharge, soc,
                      reported_objective: float | None = None, tol: float = DEFAULT_TOL,
                      allow_unmet: bool = True, battery_mode: str = "lp") -> ValidationReport:
    """Independently re-derive every multi-period constraint and the objective.

    Like the single-period validator this uses NO Pyomo: every (element, period)
    pair is re-checked straight from the ScheduleProblem data - generator limits
    and availability, ramp coupling, line capacities, outages, power balance at
    every node and period, unmet-demand bounds, battery power limits, SoC
    dynamics, SoC bounds, initial/terminal SoC, simultaneous charge/discharge,
    and the reported objective.

    ``battery_mode`` ("lp" or "exclusive") does not change any individual check
    outcome or the overall ``passed`` result. On the normal (non short-circuit)
    path the exclusivity diagnostic always runs, so ``battery_exclusive`` is
    populated from the exclusivity-category checks in both modes; the parameter
    is recorded for forward compatibility and appears in the model/CLI surface.
    """
    evaluate_exclusivity = True
    net, hor, dt = problem.network, problem.horizon, problem.step_hours
    T = list(range(problem.periods))
    checks: list[Check] = []

    def add(name: str, violation: float, detail: str = "", category: str = "physical") -> None:
        checks.append(Check(name, violation <= tol, float(max(violation, 0.0)), detail,
                            category=category))

    gens = [g.id for g in net.generators]
    lines = [l.id for l in net.lines]
    nodes = [n.id for n in net.nodes]
    batts = [b.id for b in problem.batteries]
    missing = ([g for g in gens if g not in dispatch]
               + [l for l in lines if l not in flows]
               + [n for n in nodes if n not in unmet]
               + [b for b in batts if not (b in charge and b in discharge and b in soc)])
    checks.append(Check("completeness", not missing, float(len(missing)),
                        f"missing values for: {missing}" if missing else ""))
    if missing:
        return _report(checks, evaluate_exclusivity=False)

    bad_len = ([g for g in gens if len(dispatch[g]) != len(T)]
               + [l for l in lines if len(flows[l]) != len(T)]
               + [n for n in nodes if len(unmet[n]) != len(T)]
               + [b for b in batts if not (len(charge[b]) == len(discharge[b]) == len(soc[b]) == len(T))])
    checks.append(Check("horizon_length", not bad_len, float(len(bad_len)),
                        f"wrong number of periods for: {bad_len}" if bad_len else ""))
    if bad_len:
        return _report(checks, evaluate_exclusivity=False)

    add("generator_limits",
        max((max(-dispatch[g][t], dispatch[g][t] - hor.generation_limit(net.generator(g), t))
             for g in gens for t in T), default=0.0),
        "0 <= p[g,t] <= min(capacity, availability) * online[g,t]")

    ramp_viol = 0.0
    for g_obj in net.generators:
        gid = g_obj.id
        for t in T[1:]:
            if g_obj.ramp_up_mw is not None:
                ramp_viol = max(ramp_viol, dispatch[gid][t] - dispatch[gid][t - 1] - g_obj.ramp_up_mw)
            if g_obj.ramp_down_mw is not None:
                ramp_viol = max(ramp_viol, dispatch[gid][t - 1] - dispatch[gid][t] - g_obj.ramp_down_mw)
        init = problem.initial_dispatch_mw.get(gid)
        if init is not None:
            if g_obj.ramp_up_mw is not None:
                ramp_viol = max(ramp_viol, dispatch[gid][0] - init - g_obj.ramp_up_mw)
            if g_obj.ramp_down_mw is not None:
                ramp_viol = max(ramp_viol, init - dispatch[gid][0] - g_obj.ramp_down_mw)
    add("ramp_limits", ramp_viol,
        "|p[g,t] - p[g,t-1]| within ramp limits (and vs declared initial output)")

    add("line_capacity",
        max((abs(flows[l][t]) - hor.line_limit(net.line(l), t) for l in lines for t in T), default=0.0),
        "|f[l,t]| <= cap[l,t] (cap is 0 during an outage)")

    add("outages_respected",
        max([abs(flows[l][t]) for l in lines for t in T if not hor.line_in_service[t][l]]
            + [dispatch[g][t] for g in gens for t in T if not hor.generator_online[t][g]]
            + [0.0]),
        "out-of-service elements carry no flow / output")

    add("unmet_bounds", max((max(-unmet[n][t], unmet[n][t] - hor.demand(n, t)) for n in nodes for t in T),
                            default=0.0), "0 <= u[n,t] <= demand[n,t]")
    if not allow_unmet:
        add("no_unmet_in_strict_mode", max((v for s in unmet.values() for v in s), default=0.0),
            "strict mode forbids unmet demand")

    worst_balance = 0.0
    for n in nodes:
        for t in T:
            gen_at = sum(dispatch[g][t] for g in gens if net.generator(g).node == n)
            inflow = sum(flows[l][t] for l in lines if net.line(l).to_node == n)
            outflow = sum(flows[l][t] for l in lines if net.line(l).from_node == n)
            dis_at = sum(discharge[b][t] for b in batts if problem.battery(b).node == n)
            ch_at = sum(charge[b][t] for b in batts if problem.battery(b).node == n)
            worst_balance = max(worst_balance, abs(gen_at + inflow - outflow + dis_at - ch_at
                                                   + unmet[n][t] - hor.demand(n, t)))
    add("power_balance", worst_balance,
        "generation + inflow - outflow + discharge - charge + unmet = demand at every node and period")

    batt_viol = soc_viol = sim_viol = 0.0
    for b_obj in problem.batteries:
        bid = b_obj.id
        prev = b_obj.initial_soc_mwh
        for t in T:
            batt_viol = max(batt_viol, charge[bid][t] - b_obj.max_charge_mw,
                            discharge[bid][t] - b_obj.max_discharge_mw, -charge[bid][t], -discharge[bid][t])
            expected = prev + b_obj.eta_charge * charge[bid][t] * dt - discharge[bid][t] * dt / b_obj.eta_discharge
            soc_viol = max(soc_viol, -soc[bid][t], soc[bid][t] - b_obj.capacity_mwh, abs(soc[bid][t] - expected))
            sim_viol = max(sim_viol, min(charge[bid][t], discharge[bid][t]))
            prev = soc[bid][t]
        soc_viol = max(soc_viol, b_obj.min_soc_mwh - min(soc[bid], default=b_obj.min_soc_mwh))
        if b_obj.final_soc_mwh is not None:
            soc_viol = max(soc_viol, abs(soc[bid][T[-1]] - b_obj.final_soc_mwh))
    add("battery_power_limits", batt_viol,
        "0 <= charge <= max_charge; 0 <= discharge <= max_discharge")
    add("battery_soc_dynamics", soc_viol,
        "e[t] = e[t-1] + eta_c*ch*dt - dis*dt/eta_d; min_soc <= e <= capacity; initial and final SoC honoured")
    add("simultaneous_charge_discharge", sim_viol,
        "no battery charges and discharges in the same period (not enforced under the LP relaxation; "
        "see README, 'simultaneity')", category="exclusivity")

    if reported_objective is not None:
        costs = recompute_schedule_costs(problem, dispatch, unmet, charge, discharge)
        recomputed = costs["operating_cost_usd"] + costs["tiebreak_cost_usd"]
        diff = abs(recomputed - reported_objective)
        rel = diff / max(1.0, abs(reported_objective))
        checks.append(Check("objective_consistency", rel <= 1e-6, float(diff),
                            f"recomputed {recomputed:.6f} vs reported {reported_objective:.6f}",
                            category="model"))
    return _report(checks, evaluate_exclusivity=evaluate_exclusivity)


# ------------------------------------------- executed-trajectory verification ----
def verify_executed_trajectory(
    problem: "ScheduleProblem",
    realized_demand: Sequence[Mapping[str, float]],
    realized_avail: Sequence[Mapping[str, float]],
    *,
    dispatch: Mapping[str, Sequence[float]],
    unmet: Mapping[str, Sequence[float]],
    flows: Mapping[str, Sequence[float]] | None = None,
    charge: Mapping[str, Sequence[float]] | None = None,
    discharge: Mapping[str, Sequence[float]] | None = None,
    soc: Mapping[str, Sequence[float]] | None = None,
    battery_initial_soc: Mapping[str, float] | None = None,
    allow_unmet: bool = True,
    tol: float = DEFAULT_TOL,
    battery_mode: str = "lp",
) -> ValidationReport:
    """Independently re-check an *executed* controller trajectory.

    Unlike :func:`validate_schedule`, this validates the actions a controller
    actually ran against the **realized** demand/availability (not the forecast),
    so it verifies physical consistency under the true conditions. The physical
    facts (generator limits, line capacities, outage flags) come from ``problem``;
    only the demand/availability rows are replaced by the realized ones.

    The caller supplies the executed per-period arrays (each of length
    ``problem.periods``) plus optional battery arrays. When battery arrays are
    omitted the battery is treated as frozen (charge=discharge=0, SoC constant at
    ``battery_initial_soc``). Returns the same :class:`ValidationReport` shape used
    everywhere else, so failures surface identically.
    """
    periods = problem.periods
    n_nodes = [n.id for n in problem.network.nodes]
    for t in range(periods):
        if set(realized_demand[t]) != set(n_nodes):
            raise ValueError("realized_demand rows must cover exactly the network nodes")
    realized_horizon = replace(problem.horizon,
                              demand_mw=tuple(dict(realized_demand[t]) for t in range(periods)),
                              availability_mw=tuple(dict(realized_avail[t]) for t in range(periods)))
    realized_problem = replace(problem, horizon=realized_horizon)

    gen_ids = [g.id for g in problem.network.generators]
    line_ids = [l.id for l in problem.network.lines]
    batt_ids = [b.id for b in problem.batteries]
    disp = {g: list(dispatch[g]) for g in gen_ids if g in dispatch}
    if flows is None:
        fl = {l: [0.0] * periods for l in line_ids}
    else:
        fl = {l: list(flows[l]) for l in line_ids if l in flows}
    un = {n: list(unmet[n]) for n in n_nodes if n in unmet}
    if charge is None:
        ch = {b: [0.0] * periods for b in batt_ids}
    else:
        ch = {b: list(charge[b]) for b in batt_ids if b in charge}
    if discharge is None:
        dis = {b: [0.0] * periods for b in batt_ids}
    else:
        dis = {b: list(discharge[b]) for b in batt_ids if b in discharge}
    if soc is None:
        init = battery_initial_soc or {b.id: b.initial_soc_mwh for b in problem.batteries}
        so = {b: [float(init.get(b, problem.battery(b).initial_soc_mwh))] * periods
              for b in batt_ids}
    else:
        so = {b: list(soc[b]) for b in batt_ids if b in soc}
    return validate_schedule(realized_problem, disp, fl, un, ch, dis, so,
                             reported_objective=None, tol=tol, allow_unmet=allow_unmet,
                             battery_mode=battery_mode)
