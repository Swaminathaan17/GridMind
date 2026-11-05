"""Resilience analysis over a multi-period horizon.

Computes per-scenario metrics from a validated ``ScheduleResult`` (cost, unserved
energy, served percentage, renewable utilisation/curtailment, battery behaviour,
worst unmet periods) and runs storage-ablation experiments (the same scenario
solved with and without the battery fleet) and scenario suites.

Units: cost USD, power MW, energy MWh (all energies are power x step_hours).
"""
from __future__ import annotations

import time
from typing import Any, Iterable

import pandas as pd

from gridmind.multiperiod import ScheduleProblem
from gridmind.scenarios import HorizonScenario, apply_horizon_scenario
from gridmind.scheduling import ScheduleResult, solve_schedule


# ----------------------------------------------------------------- metrics ----
def schedule_metrics(problem: ScheduleProblem, result: ScheduleResult) -> dict[str, Any]:
    """Resilience and operating metrics for one solved horizon.

    Returns None-valued metrics when the solve did not produce an optimal
    schedule; never invents numbers for failed solves.
    """
    dt = problem.step_hours
    hor = problem.horizon
    row: dict[str, Any] = {
        "status": result.status,
        "termination": result.termination_condition,
        "solver": result.solver,
        "runtime_s": result.runtime_seconds,
        "validation_passed": None if result.validation is None else result.validation.passed,
        "periods": problem.periods,
        "step_hours": dt,
        "operating_cost_usd": None, "generation_cost_usd": None, "unmet_penalty_cost_usd": None,
        "total_demand_mwh": problem.total_demand_mwh,
        "unmet_energy_mwh": None, "demand_served_pct": None,
        "max_unmet_mw": None, "max_unmet_periods": "",
        "renewable_available_mwh": None, "renewable_used_mwh": None, "renewable_curtailed_mwh": None,
        "battery_count": len(problem.batteries),
        "battery_charge_mwh": 0.0, "battery_discharge_mwh": 0.0,
        "battery_final_soc_mwh": 0.0, "battery_simultaneous_periods": 0,
        "message": result.message,
    }
    if result.status != "optimal" or result.operating_cost_usd is None:
        return row

    row.update({
        "operating_cost_usd": result.operating_cost_usd,
        "generation_cost_usd": result.generation_cost_usd,
        "unmet_penalty_cost_usd": result.unmet_penalty_cost_usd,
        "unmet_energy_mwh": result.unmet_energy_mwh,
        "demand_served_pct": result.demand_served_pct,
        "max_unmet_mw": result.max_unmet_mw,
        "max_unmet_periods": ";".join(str(t) for t in result.max_unmet_periods),
    })

    ren_ids = [g.id for g in problem.network.generators if g.renewable]
    available = dt * sum(hor.availability_mw[t][g] for g in ren_ids for t in range(problem.periods))
    used = dt * sum(result.dispatch[g][t] for g in ren_ids for t in range(problem.periods))
    row["renewable_available_mwh"] = available
    row["renewable_used_mwh"] = used
    row["renewable_curtailed_mwh"] = available - used  # unused availability (no commitment model)

    charge = dt * sum(sum(s) for s in result.charge.values())
    discharge = dt * sum(sum(s) for s in result.discharge.values())
    simultaneous = sum(1 for b in result.charge for t in range(problem.periods)
                       if result.charge[b][t] > 1e-6 and result.discharge[b][t] > 1e-6)
    row["battery_charge_mwh"] = charge
    row["battery_discharge_mwh"] = discharge
    row["battery_final_soc_mwh"] = sum(result.soc[b][-1] for b in result.soc)
    row["battery_simultaneous_periods"] = simultaneous
    return row


# ------------------------------------------------------------ ablation ----
def storage_ablation(problem: ScheduleProblem, scenario: HorizonScenario,
                     allow_unmet: bool = True, battery_mode: str = "lp") -> dict[str, Any]:
    """Solve one scenario with and without storage (all other data identical).

    The with/without delta isolates what the battery fleet contributes under the
    scenario; assumptions are identical except for the batteries themselves.
    ``battery_mode`` is forwarded to every solve (see ``solve_schedule``).
    """
    with_storage = solve_schedule(problem, allow_unmet=allow_unmet, battery_mode=battery_mode)
    without = solve_schedule(problem.without_batteries(), allow_unmet=allow_unmet,
                             battery_mode=battery_mode)
    row: dict[str, Any] = {
        "scenario_id": scenario.id, "scenario": scenario.name,
        "with_storage": schedule_metrics(problem, with_storage),
        "without_storage": schedule_metrics(problem.without_batteries(), without),
        "cost_savings_usd": None, "unmet_reduction_mwh": None,
        "served_pct_gain": None,
    }
    a, b = row["with_storage"], row["without_storage"]
    if a["operating_cost_usd"] is not None and b["operating_cost_usd"] is not None:
        row["cost_savings_usd"] = b["operating_cost_usd"] - a["operating_cost_usd"]
        row["unmet_reduction_mwh"] = b["unmet_energy_mwh"] - a["unmet_energy_mwh"]
        row["served_pct_gain"] = a["demand_served_pct"] - b["demand_served_pct"]
    return row


# ----------------------------------------------------------------- suites ----
def run_resilience_suite(problem: ScheduleProblem, scenarios: Iterable[HorizonScenario],
                         allow_unmet: bool = True, battery_mode: str = "lp") -> pd.DataFrame:
    """Flat DataFrame: one row per scenario x {with, without} storage."""
    rows: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    for scenario in scenarios:
        stressed = apply_horizon_scenario(problem, scenario)
        ablation = storage_ablation(stressed, scenario, allow_unmet=allow_unmet,
                                    battery_mode=battery_mode)
        for storage_tag, metrics in (("with_storage", ablation["with_storage"]),
                                     ("without_storage", ablation["without_storage"])):
            flat = {"scenario_id": scenario.id, "scenario": scenario.name,
                    "storage": storage_tag,
                    "cost_savings_vs_no_storage_usd": ablation["cost_savings_usd"],
                    "unmet_reduction_vs_no_storage_mwh": ablation["unmet_reduction_mwh"]}
            flat.update(metrics)
            rows.append(flat)
    df = pd.DataFrame(rows)
    df["suite_runtime_s"] = time.perf_counter() - t0
    return df
