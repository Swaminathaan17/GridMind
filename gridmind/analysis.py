"""Bottleneck detection and sensitivity analysis (built on solve_dispatch)."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

import pandas as pd

from gridmind.network import Network
from gridmind.optimizer import SolveResult, solve_dispatch
from gridmind.scenarios import scale_demand, scale_generator_cost, scale_line_capacity

BIND_TOL = 1e-6


# ---------------------------------------------------------------- congestion
def line_utilization(network: Network, flows: Mapping[str, float]) -> list[dict[str, Any]]:
    rows = []
    for l in network.lines:
        cap = l.effective_capacity_mw
        flow = flows.get(l.id, 0.0)
        rows.append({"line": l.id, "flow_mw": flow, "capacity_mw": cap, "in_service": l.in_service,
                     "utilization": (abs(flow) / cap) if cap > 0 else None})
    return rows


def overloaded_lines(network: Network, flows: Mapping[str, float], tol: float = BIND_TOL) -> list[dict[str, Any]]:
    """Lines whose |flow| EXCEEDS their effective capacity (flow-schedule check).
    A valid optimiser solution never overloads a line; this is meaningful for
    relaxed/baseline schedules (see congestion_analysis)."""
    return [{"line": l.id, "flow_mw": flows[l.id], "capacity_mw": l.effective_capacity_mw,
             "overload_mw": abs(flows[l.id]) - l.effective_capacity_mw}
            for l in network.lines if l.id in flows and abs(flows[l.id]) > l.effective_capacity_mw + tol]


def binding_constraints(network: Network, result: SolveResult, tol: float = BIND_TOL) -> list[dict[str, Any]]:
    """Constraints active at the optimum, detected from primal slack (robust),
    annotated with the solver's raw dual value when available."""
    if result.status != "optimal":
        return []
    out: list[dict[str, Any]] = []
    for l in network.lines:
        cap = l.effective_capacity_mw
        if l.in_service and cap > 0 and abs(result.flows[l.id]) >= cap - tol:
            side = "line_ub" if result.flows[l.id] > 0 else "line_lb"
            out.append({"type": "line_capacity", "id": l.id, "value_mw": result.flows[l.id], "limit_mw": cap,
                        "dual": result.duals.get(f"{side}[{l.id}]")})
    for g in network.generators:
        if not g.online:
            continue
        p = result.dispatch[g.id]
        cap = min(g.capacity_mw, g.available_mw)
        if cap <= 0 or p < cap - tol:
            continue
        if g.available_mw < g.capacity_mw - BIND_TOL:
            out.append({"type": "renewable_availability", "id": g.id, "value_mw": p, "limit_mw": g.available_mw,
                        "dual": result.duals.get(f"avail_cap[{g.id}]")})
        else:
            out.append({"type": "generator_capacity", "id": g.id, "value_mw": p, "limit_mw": g.capacity_mw,
                        "dual": result.duals.get(f"gen_limit[{g.id}]")})
    for n in network.nodes:
        if result.unmet[n.id] > tol:
            out.append({"type": "unmet_demand", "id": n.id, "value_mw": result.unmet[n.id], "limit_mw": n.demand_mw,
                        "dual": None})
    return out


def congestion_analysis(network: Network) -> dict[str, Any]:
    """Solve the real network and a copy with every in-service line limit lifted;
    lines that exceed their true limit in the relaxed schedule are the bottlenecks
    and the objective gap is the cost of congestion."""
    big = max(1.0, network.total_demand_mw) * 10.0
    relaxed_net = replace(network, lines=tuple(
        replace(l, capacity_mw=max(l.capacity_mw, big)) if l.in_service else l for l in network.lines))
    real, relaxed = solve_dispatch(network), solve_dispatch(relaxed_net)
    info: dict[str, Any] = {"constrained_status": real.status, "relaxed_status": relaxed.status,
                            "bottleneck_lines": [], "congestion_cost": None}
    if real.status == "optimal" and relaxed.status == "optimal":
        info["bottleneck_lines"] = overloaded_lines(network, relaxed.flows)
        info["congestion_cost"] = real.objective - relaxed.objective
        info["binding_constraints"] = binding_constraints(network, real)
    return info


# --------------------------------------------------------------- sensitivity
DEFAULT_MULTIPLIERS = {"demand": (0.8, 0.9, 1.0, 1.1, 1.2, 1.3),
                       "generator_cost": (0.5, 0.75, 1.0, 1.25, 1.5, 2.0),
                       "line_capacity": (0.25, 0.5, 0.75, 1.0, 1.25, 1.5)}


def run_sensitivity(network: Network, kind: str, multipliers: Sequence[float] | None = None,
                    target: str | None = None) -> pd.DataFrame:
    """One-at-a-time sensitivity. kind: demand | generator_cost | line_capacity.
    target: generator id (generator_cost; None = all), line id (line_capacity; None = all lines).
    delta_objective is measured against the multiplier=1.0 run; marginal_usd_per_step
    is the objective change versus the previous row."""
    if kind not in DEFAULT_MULTIPLIERS:
        raise ValueError(f"kind must be one of {sorted(DEFAULT_MULTIPLIERS)}")
    mults = sorted(set(multipliers or DEFAULT_MULTIPLIERS[kind]) | {1.0})
    transforms = {"demand": lambda f: scale_demand(network, f),
                  "generator_cost": lambda f: scale_generator_cost(network, f, target),
                  "line_capacity": lambda f: scale_line_capacity(network, f, target)}
    rows: list[dict[str, Any]] = []
    for f in mults:
        res = solve_dispatch(transforms[kind](f))
        rows.append({"kind": kind, "target": target or "all", "multiplier": f, "status": res.status,
                     "objective_usd": res.objective, "unmet_mw": res.unmet_demand_mw,
                     "runtime_s": res.runtime_seconds})
    df = pd.DataFrame(rows)
    base = df.loc[df["multiplier"] == 1.0, "objective_usd"].iloc[0]
    df["delta_objective_usd"] = df["objective_usd"] - base if base is not None and pd.notna(base) else None
    df["marginal_usd_per_step"] = df["objective_usd"].diff()
    return df
