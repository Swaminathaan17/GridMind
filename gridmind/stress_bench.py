"""Stage 5C benchmark harness (reproducible, paired, no fabrication).

Runs the configured methods on identical realized stress conditions with causal
forecast/realization separation. Schema v2 stores each method's actually executed
trace, validates that trace without re-solving, and separates solver status from
policy status so solver-optimal but policy-infeasible methods are excluded from
comparisons explicitly.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from gridmind.control import ControlConfig
from gridmind.multiperiod import ScheduleProblem
from gridmind.stress import (METHOD_NAMES, STRESS_SCHEMA_VERSION, Disturbance,
                             ForecastErrorModel, MethodResult, StressEnvironment,
                             StressSchedule, run_method_open_loop,
                             run_method_rolling_horizon,
                             run_method_stochastic,
                             run_method_stochastic_rolling_horizon)
from gridmind.validation import DEFAULT_TOL


STRESS_BENCH_SCHEMA_VERSION = 2


@dataclass
class StressBenchmarkConfig:
    methods: tuple[str, ...] = METHOD_NAMES
    battery_mode: str = "lp"
    allow_unmet: bool = True
    demand_sigma: float = 0.06
    renewable_sigma: float = 0.12
    lookahead: int = 6
    n_scenarios_stochastic: int = 20
    risk_weight_neutral: float = 0.0
    risk_weight_aware: float = 0.5
    cvar_alpha: float = 0.95
    lead_periods: int = 1
    tol: float = DEFAULT_TOL
    seed: int = 20260301

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StressBenchmarkRecord:
    schema_version: int = STRESS_BENCH_SCHEMA_VERSION
    config: StressBenchmarkConfig = field(default_factory=StressBenchmarkConfig)
    problem: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    exclusions: list[dict[str, Any]] = field(default_factory=list)
    included_methods: list[str] = field(default_factory=list)
    excluded_methods: list[str] = field(default_factory=list)
    comparison: dict[str, Any] = field(default_factory=dict)
    runtime_seconds: float = 0.0
    started_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stress_schema_version": STRESS_SCHEMA_VERSION,
            "config": self.config.to_dict(),
            "problem": self.problem,
            "environment": self.environment,
            "results": self.results,
            "failures": self.failures,
            "exclusions": self.exclusions,
            "included_methods": self.included_methods,
            "excluded_methods": self.excluded_methods,
            "comparison": self.comparison,
            "runtime_seconds": self.runtime_seconds,
        }


def _base_problem() -> ScheduleProblem:
    from gridmind.multiperiod import sample_schedule_problem
    return sample_schedule_problem()


def _reconstruct_stress_trajectories(problem: ScheduleProblem, method_result: MethodResult,
                                     realized_d: tuple, realized_a: tuple,
                                     battery_mode: str = "lp", allow_unmet: bool = True,
                                     tol: float = DEFAULT_TOL):
    """Compatibility shim: return the stored executed trace, never a re-solve."""
    del problem, realized_d, realized_a, battery_mode, allow_unmet, tol
    return list(method_result.trace)


def _error_result(method: str, exc: BaseException) -> dict[str, Any]:
    return {
        "method": method,
        "status": "error",
        "solver_status": "error",
        "policy_status": "error",
        "validation_status": "skipped",
        "feasible_for_comparison": False,
        "included_for_comparison": False,
        "exclusion_reason": f"execution_error: {exc}",
        "policy_label": "",
        "trace": [],
        "executed_trace": [],
        "periods_expected": 0,
        "periods_executed": 0,
        "message": str(exc),
        "attempted": True,
        "execution_complete": False,
    }


def _run_selected(env: StressEnvironment, config: StressBenchmarkConfig,
                  selected: tuple[str, ...]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    results: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for method in selected:
        try:
            if method == "deterministic_open_loop":
                out = run_method_open_loop(
                    env, battery_mode=config.battery_mode,
                    allow_unmet=config.allow_unmet, tol=config.tol,
                    perfect_foresight=False)
            elif method == "stochastic_risk_neutral":
                out = run_method_stochastic(
                    env, risk_weight=config.risk_weight_neutral,
                    cvar_alpha=config.cvar_alpha, lead_periods=config.lead_periods,
                    n_scenarios=config.n_scenarios_stochastic,
                    battery_mode=config.battery_mode,
                    allow_unmet=config.allow_unmet, tol=config.tol)
            elif method == "stochastic_risk_aware":
                out = run_method_stochastic(
                    env, risk_weight=config.risk_weight_aware,
                    cvar_alpha=config.cvar_alpha, lead_periods=config.lead_periods,
                    n_scenarios=config.n_scenarios_stochastic,
                    battery_mode=config.battery_mode,
                    allow_unmet=config.allow_unmet, tol=config.tol)
            elif method in ("stochastic_rolling_risk_neutral",
                            "stochastic_rolling_risk_aware"):
                risk_weight = (config.risk_weight_neutral
                               if method.endswith("risk_neutral")
                               else config.risk_weight_aware)
                out = run_method_stochastic_rolling_horizon(
                    env, risk_weight=risk_weight, cvar_alpha=config.cvar_alpha,
                    lead_periods=config.lead_periods,
                    n_scenarios=config.n_scenarios_stochastic,
                    lookahead=config.lookahead,
                    battery_mode=config.battery_mode,
                    allow_unmet=config.allow_unmet,
                    demand_sigma=config.demand_sigma,
                    renewable_sigma=config.renewable_sigma,
                    seed=config.seed, tol=config.tol)
            elif method == "rolling_horizon":
                ctrl_cfg = ControlConfig(
                    lookahead=config.lookahead, mode="deterministic",
                    battery_mode=config.battery_mode,
                    allow_unmet=config.allow_unmet,
                    demand_sigma=config.demand_sigma,
                    renewable_sigma=config.renewable_sigma,
                    seed=config.seed, cvar_confidence=config.cvar_alpha)
                out = run_method_rolling_horizon(env, ctrl_cfg, tol=config.tol)
            else:  # pragma: no cover - guarded by caller
                raise ValueError(f"unknown method {method!r}")
            results[method] = out.to_dict()
            if out.solver_status not in ("optimal", ""):
                failures.append({"method": method, "error": out.message or out.solver_status,
                                 "solver_status": out.solver_status,
                                 "policy_status": out.policy_status})
        except Exception as exc:  # noqa: BLE001
            failures.append({"method": method, "error": str(exc),
                             "solver_status": "error", "policy_status": "error"})
            results[method] = _error_result(method, exc)
    return results, failures


def _classify(results: dict[str, Any], selected: tuple[str, ...]
              ) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    included: list[str] = []
    exclusions: list[dict[str, Any]] = []
    for method in selected:
        payload = results.get(method)
        if not isinstance(payload, dict):
            payload = _error_result(method, ValueError("method returned no result"))
            results[method] = payload
        payload["attempted"] = True
        payload["execution_complete"] = bool(
            payload.get("trace") and payload.get("periods_executed")
            == payload.get("periods_expected"))
        included_ok = bool(payload.get("feasible_for_comparison")) \
            and payload.get("validation_status") == "passed" \
            and payload.get("solver_status") == "optimal"
        payload["included_for_comparison"] = included_ok
        if included_ok:
            included.append(method)
            continue
        reason = payload.get("exclusion_reason")
        if not reason:
            solver_status = str(payload.get("solver_status", "unknown"))
            reason = (f"validation_{payload.get('validation_status', 'unknown')}"
                      if solver_status == "optimal"
                      else (solver_status if solver_status.startswith("solver_")
                            else f"solver_{solver_status}"))
        exclusions.append({
            "method": method,
            "reason": reason,
            "solver_status": payload.get("solver_status"),
            "policy_status": payload.get("policy_status"),
            "validation_status": payload.get("validation_status"),
            "terminal_soc_deviation_mwh": payload.get("terminal_soc_deviation_mwh"),
            "metric_discrepancies": payload.get("metric_discrepancies") or [],
        })
    comparison = {
        "attempted_methods": list(selected),
        "solver_optimal_methods": [m for m in selected
                                   if results.get(m, {}).get("solver_status") == "optimal"],
        "successfully_executed_methods": [m for m in selected
                                          if results.get(m, {}).get("execution_complete")],
        "validation_passed_methods": [m for m in selected
                                      if results.get(m, {}).get("validation_status") == "passed"],
        "included_methods": included,
        "excluded_methods": [e["method"] for e in exclusions],
    }
    return included, exclusions, comparison


def run_stress_benchmark(problem: ScheduleProblem | None = None,
                         schedule: StressSchedule | None = None,
                         config: StressBenchmarkConfig | None = None
                         ) -> StressBenchmarkRecord:
    """Run the configured methods on identical realized stress conditions."""
    t0 = time.perf_counter()
    config = config or StressBenchmarkConfig()
    unknown = [m for m in config.methods if m not in METHOD_NAMES]
    if unknown:
        raise ValueError(f"unknown benchmark methods: {unknown}; supported: {METHOD_NAMES}")
    selected = tuple(m for m in METHOD_NAMES if m in config.methods)
    if not selected:
        raise ValueError("at least one benchmark method must be selected")
    problem = problem or _base_problem()
    problem.validate()
    if schedule is None:
        schedule = StressSchedule(
            disturbances=(Disturbance(kind="demand_spike", onset=8, duration=4,
                                      severity=1.25, target=None),
                          Disturbance(kind="renewable_shortfall", onset=10, duration=6,
                                      severity=0.35, target=None),
                          Disturbance(kind="low_initial_soc", onset=0, duration=1,
                                      severity=0.1, target="battery-central")),
            seed=config.seed, name="compound_stress_v1")
    error_model = ForecastErrorModel(demand_sigma=config.demand_sigma,
                                     renewable_sigma=config.renewable_sigma,
                                     seed=config.seed)
    env = StressEnvironment(base_problem=problem, stress=schedule,
                            error_model=error_model)
    record = StressBenchmarkRecord(config=config, started_at=t0)
    record.environment = env.to_dict()
    record.problem = {"periods": problem.periods, "step_hours": problem.step_hours,
                      "network": problem.network.name}
    record.schema_version = STRESS_BENCH_SCHEMA_VERSION

    results, failures = _run_selected(env, config, selected)
    included, exclusions, comparison = _classify(results, selected)
    record.results = results
    record.failures = failures
    record.exclusions = exclusions
    record.included_methods = included
    record.excluded_methods = [e["method"] for e in exclusions]
    record.comparison = comparison
    record.runtime_seconds = time.perf_counter() - t0
    return record


def write_stress_benchmark(record: StressBenchmarkRecord,
                           out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = record.to_dict()
    p = out / "stress_benchmark.json"
    p.write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")
    summary = {
        "schema_version": payload["schema_version"],
        "stress_schema_version": payload["stress_schema_version"],
        "runtime_seconds": payload["runtime_seconds"],
        "failures": payload["failures"],
        "exclusions": payload["exclusions"],
        "included_methods": payload["included_methods"],
        "excluded_methods": payload["excluded_methods"],
        "comparison": payload["comparison"],
        "results": {k: {"method": v.get("method"),
                        "status": v.get("status"),
                        "solver_status": v.get("solver_status"),
                        "policy_status": v.get("policy_status"),
                        "validation_status": v.get("validation_status"),
                        "feasible_for_comparison": v.get("feasible_for_comparison"),
                        "included_for_comparison": v.get("included_for_comparison"),
                        "exclusion_reason": v.get("exclusion_reason"),
                        "policy_label": v.get("policy_label"),
                        "terminal_soc_deviation_mwh": v.get("terminal_soc_deviation_mwh"),
                        "metrics_match": v.get("trace_metrics_match"),
                        "trace_periods": len(v.get("trace") or []),
                        "periods_expected": v.get("periods_expected"),
                        "realized_operating_cost_usd": v.get("realized_operating_cost_usd"),
                        "unmet_energy_mwh": v.get("unmet_energy_mwh"),
                        "solver": v.get("solver"),
                        "provenance": {
                            "problem_sha256": v.get("problem_sha256"),
                            "realized_conditions_sha256": v.get("realized_conditions_sha256"),
                            "forecast_sha256": v.get("forecast_sha256"),
                            "config_sha256": v.get("config_sha256"),
                        }}
                    for k, v in payload["results"].items()},
    }
    s = out / "stress_benchmark_summary.json"
    s.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    return {"json": p, "summary": s}
