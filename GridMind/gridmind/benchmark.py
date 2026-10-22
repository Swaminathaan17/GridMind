"""Baselines, scenario evaluation and CSV-exporting batch benchmark.

All costs are USD for ONE interval (network.interval_hours, default 1 h) and use
the same definition as the optimiser objective: generation cost + unmet-demand penalty.

Reference points (both ignore the transmission network, so they are *references*,
not dispatchable schedules):
  proportional baseline  - cost-blind: every available generator runs at the same
                           fraction of its limit so that supply = demand.
  copper-plate bound     - merit-order dispatch with unlimited transmission; a lower
                           bound on the network-constrained optimum.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from gridmind.analysis import binding_constraints
from gridmind.multiperiod import ScheduleProblem
from gridmind.network import Network
from gridmind.optimizer import SolveResult, solve_dispatch
from gridmind.scenarios import HorizonScenario, Scenario, apply_scenario, apply_horizon_scenario
from gridmind.scheduling import solve_schedule


def _cost(network: Network, dispatch: dict[str, float], unmet_mw: float) -> float:
    gen = sum(g.cost_per_mwh * dispatch[g.id] for g in network.generators)
    return network.interval_hours * (gen + network.unmet_penalty_per_mwh * unmet_mw)


def proportional_baseline_cost(network: Network) -> float:
    demand = network.total_demand_mw
    supply = sum(g.effective_limit_mw for g in network.generators)
    frac = min(1.0, demand / supply) if supply > 0 else 0.0
    dispatch = {g.id: frac * g.effective_limit_mw for g in network.generators}
    return _cost(network, dispatch, max(0.0, demand - supply))


def copperplate_lower_bound(network: Network) -> float:
    remaining = network.total_demand_mw
    dispatch = {g.id: 0.0 for g in network.generators}
    for g in sorted(network.generators, key=lambda x: (x.cost_per_mwh, x.id)):
        if g.cost_per_mwh >= network.unmet_penalty_per_mwh or remaining <= 0:
            continue  # unmet demand is cheaper than this unit
        dispatch[g.id] = min(g.effective_limit_mw, remaining)
        remaining -= dispatch[g.id]
    return _cost(network, dispatch, remaining)


def compare_to_baseline(network: Network, result: SolveResult) -> dict[str, float | None]:
    base, bound = proportional_baseline_cost(network), copperplate_lower_bound(network)
    out: dict[str, float | None] = {"baseline_cost_usd": base, "copperplate_bound_usd": bound,
                                    "saving_usd": None, "saving_pct": None, "congestion_cost_usd": None}
    if result.status == "optimal" and result.objective is not None:
        out["saving_usd"] = base - result.objective
        out["saving_pct"] = 100.0 * (base - result.objective) / base if base > 0 else None
        out["congestion_cost_usd"] = result.objective - bound
    return out


def evaluate_scenario(base_network: Network, scenario: Scenario, allow_unmet: bool = True) -> dict[str, Any]:
    t0 = time.perf_counter()
    net = apply_scenario(base_network, scenario)
    res = solve_dispatch(net, allow_unmet=allow_unmet)
    row: dict[str, Any] = {
        "scenario_id": scenario.id, "scenario": scenario.name, "status": res.status,
        "termination": res.termination_condition,
        "objective_usd": res.objective, "generation_cost_usd": res.generation_cost,
        "unmet_mw": res.unmet_demand_mw, "total_demand_mw": net.total_demand_mw,
        "runtime_s": res.runtime_seconds, "total_time_s": time.perf_counter() - t0,
        "validation_passed": None if res.validation is None else res.validation.passed,
        "binding_lines": sum(1 for b in binding_constraints(net, res) if b["type"] == "line_capacity"),
        "demand_multiplier": scenario.demand_multiplier, "renewable_multiplier": scenario.renewable_multiplier,
        "line_outages": ";".join(scenario.line_outages), "generator_outages": ";".join(scenario.generator_outages)}
    row["unmet_pct"] = (100.0 * res.unmet_demand_mw / net.total_demand_mw
                        if res.unmet_demand_mw is not None and net.total_demand_mw > 0 else None)
    row.update(compare_to_baseline(net, res))
    return row


def run_benchmark(network: Network, scenarios: Iterable[Scenario]) -> pd.DataFrame:
    return pd.DataFrame([evaluate_scenario(network, s) for s in scenarios])


def export_csv(df: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


# ===================================== multi-period references and batches ====
def myopic_copperplate_cost(problem: ScheduleProblem) -> float:
    """Independent merit-order cost per period (unlimited transmission, no
    storage, no ramps), summed over the horizon.

    This is a *reference*, not a bound: it ignores the network and every
    inter-temporal constraint, so it can be either cheaper or more expensive
    than the true networked optimum (storage alone can make the real problem
    cheaper than any no-storage reference). It measures the value the
    optimiser adds over solving each period in isolation.
    """
    dt = problem.step_hours
    penalty = problem.network.unmet_penalty_per_mwh
    total = 0.0
    for t in range(problem.periods):
        remaining = sum(problem.horizon.demand_mw[t].values())
        for g in sorted(problem.network.generators, key=lambda x: (x.cost_per_mwh, x.id)):
            if g.cost_per_mwh >= penalty or remaining <= 0:
                continue
            take = min(problem.horizon.generation_limit(g, t), remaining)
            total += dt * g.cost_per_mwh * take
            remaining -= take
        total += dt * penalty * max(0.0, remaining)
    return total


def proportional_horizon_cost(problem: ScheduleProblem) -> float:
    """Cost-blind per-period baseline: every generator runs at the same
    fraction of its period limit so that supply = demand (network ignored).
    A reference point, never a bound."""
    dt = problem.step_hours
    penalty = problem.network.unmet_penalty_per_mwh
    total = 0.0
    for t in range(problem.periods):
        demand = sum(problem.horizon.demand_mw[t].values())
        limits = {g.id: problem.horizon.generation_limit(g, t) for g in problem.network.generators}
        supply = sum(limits.values())
        frac = min(1.0, demand / supply) if supply > 0 else 0.0
        used = frac * supply
        total += dt * sum(problem.network.generator(g).cost_per_mwh * frac * lim for g, lim in limits.items())
        total += dt * penalty * max(0.0, demand - used)
    return total


def evaluate_horizon_scenario(problem: ScheduleProblem, scenario: HorizonScenario,
                              allow_unmet: bool = True, battery_mode: str = "lp") -> dict[str, Any]:
    """Solve one horizon scenario and produce one flat benchmark row."""
    stressed = apply_horizon_scenario(problem, scenario)
    res = solve_schedule(stressed, allow_unmet=allow_unmet, battery_mode=battery_mode)
    row: dict[str, Any] = {
        "scenario_id": scenario.id, "scenario": scenario.name, "status": res.status,
        "termination": res.termination_condition, "solver": res.solver,
        "operating_cost_usd": res.operating_cost_usd,
        "generation_cost_usd": res.generation_cost_usd,
        "unmet_energy_mwh": res.unmet_energy_mwh,
        "total_demand_mwh": stressed.total_demand_mwh,
        "demand_served_pct": res.demand_served_pct,
        "runtime_s": res.runtime_seconds,
        "validation_passed": None if res.validation is None else res.validation.passed,
        "battery_count": len(stressed.batteries),
        "battery_charge_mwh": None, "battery_discharge_mwh": None,
        "demand_multiplier": scenario.demand_multiplier,
        "renewable_multiplier": scenario.renewable_multiplier,
        "demand_spikes": ";".join(f"{s.node or 'all'}@[{s.start},{s.end})x{s.multiplier}"
                                  for s in scenario.demand_spikes),
        "renewable_drops": ";".join(f"{d.generator or 'all'}@[{d.start},{d.end})x{d.factor}"
                                    for d in scenario.renewable_drops),
        "generator_outages": ";".join(f"{o.element_id}@[{o.start},{o.end})" for o in scenario.generator_outages),
        "line_outages": ";".join(f"{o.element_id}@[{o.start},{o.end})" for o in scenario.line_outages),
        "myopic_copperplate_usd": myopic_copperplate_cost(stressed),
        "proportional_baseline_usd": proportional_horizon_cost(stressed),
        "gap_vs_myopic_usd": None,
    }
    if res.operating_cost_usd is not None:
        row["gap_vs_myopic_usd"] = res.operating_cost_usd - row["myopic_copperplate_usd"]
        row["battery_charge_mwh"] = dt_sum(res.charge, stressed.step_hours)
        row["battery_discharge_mwh"] = dt_sum(res.discharge, stressed.step_hours)
    return row


def dt_sum(series_map: dict[str, list[float]], dt: float) -> float:
    return dt * sum(sum(s) for s in series_map.values())


def run_mp_benchmark(problem: ScheduleProblem, scenarios: Iterable[HorizonScenario],
                     battery_mode: str = "lp") -> pd.DataFrame:
    return pd.DataFrame([evaluate_horizon_scenario(problem, s, battery_mode=battery_mode)
                         for s in scenarios])


def aggregate_benchmark_summary(df: pd.DataFrame) -> dict[str, Any]:
    """Aggregate counts and totals for a benchmark batch (single- or multi-period)."""
    out: dict[str, Any] = {
        "rows": int(len(df)),
        "status_counts": df["status"].value_counts().to_dict(),
    }
    if "runtime_s" in df:
        out["total_solver_runtime_s"] = float(df["runtime_s"].sum())
        out["mean_solver_runtime_s"] = float(df["runtime_s"].mean())
    cost_col = "operating_cost_usd" if "operating_cost_usd" in df else "objective_usd"
    if cost_col in df:
        optimal = df[df["status"] == "optimal"]
        out["mean_operating_cost_usd"] = float(optimal[cost_col].mean()) if len(optimal) else None
    if "unmet_energy_mwh" in df:
        out["scenarios_with_unmet"] = int((df["unmet_energy_mwh"].fillna(0) > 1e-6).sum())
    if "validation_passed" in df:
        out["validation_passed_all"] = bool((df["validation_passed"].fillna(False)).all())
    return out
