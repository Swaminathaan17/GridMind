"""Reproducible comparative benchmark for scheduling policies under uncertainty.

This module orchestrates a *multi-seed* comparison of three battery-commitment
policies - ``deterministic`` (mean/base problem), ``risk_neutral`` (two-stage
expected-cost) and ``risk_aware`` (two-stage CVaR-weighted) - on synthetic
scenarios with a documented, reproducible protocol.

What "fair" means here (and why the numbers are trustworthy):

* Every seed generates ONE scenario set, which is split once into in-sample and
  holdout subsets by :func:`~gridmind.uncertainty.split_scenarios`. The three
  policies are fitted on the *same* in-sample set and evaluated on the *same*
  holdout set, so the comparison is paired and apples-to-apples.
* Evaluation is non-clairvoyant: the policy's first-stage commitments are fixed
  and only recourse (later periods) adapts to the realized scenario
  (:func:`~gridmind.risk_eval.evaluate_policy`).
* Metrics are probability-weighted where the scenario set defines weights.
* Failures are recorded, never fabricated: a run that does not produce a policy
  for some policy name is flagged and excluded from that policy's aggregation,
  with an explicit count.

Statistical integrity:

* Aggregates report mean / median / std / p95 over seeds for each policy.
* Paired comparisons use the *same scenarios* for both policies: we accumulate
  per-scenario cost differences (risk_neutral minus risk_aware, and so on),
  then report the mean difference, its standard deviation and Cohen's ``dz``
  (mean diff / SD of diffs). This is an effect size, NOT a significance test:
  no p-values or "significant" claims are made, because scenarios are synthetic
  and dependence structure is not modelled.

Everything is synthetic: results are model outputs, not real grid measurements.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from gridmind.multiperiod import ScheduleProblem
from gridmind.risk_eval import PolicyEvaluation, compare_policies
from gridmind.uncertainty import (
    UncertaintyConfig,
    generate_scenarios,
    split_scenarios,
)

#: metric names we aggregate across seeds, mapped to (source, direction).
#: "cost"/"risk" metrics are minimized; "unmet"/"prob" metrics follow them.
POLICY_NAMES = ("deterministic", "risk_neutral", "risk_aware")

#: version of the experiment protocol/record schema. Bump when the record shape
#: changes so downstream consumers can detect incompatibilities.
EXPERIMENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExperimentConfig:
    """Complete, serialisable specification of a comparative experiment.

    Defaults match the CLI defaults: 5 seeds x 40 scenarios, risk_weight 0.5,
    CVaR confidence 0.95, lead_periods 1, 50% holdout, LP battery mode.
    """
    seeds: tuple[int, ...] = (20260101, 20260102, 20260103, 20260104, 20260105)
    n_scenarios: int = 40
    demand_sigma: float = 0.08
    renewable_sigma: float = 0.18
    demand_ar1: float = 0.6
    holdout_fraction: float = 0.5
    risk_weight: float = 0.5
    cvar_alpha: float = 0.95
    lead_periods: int = 1
    battery_mode: str = "lp"
    allow_unmet: bool = True
    high_demand_factor: float = 1.25
    low_renewable_factor: float = 0.35
    weights: Mapping[str, float] = field(
        default_factory=lambda: {"ordinary": 0.55, "high_demand": 0.15,
                                 "low_renewable": 0.20, "compound": 0.10})

    def validate(self) -> None:
        if not self.seeds:
            raise ValueError("experiment requires at least one seed")
        if self.n_scenarios < 2:
            raise ValueError("n_scenarios must be >= 2 to allow an in/holdout split")
        if not (0.0 < self.holdout_fraction < 1.0):
            raise ValueError("holdout_fraction must lie strictly between 0 and 1")
        if not (0.0 <= self.risk_weight <= 1.0):
            raise ValueError("risk_weight must lie in [0, 1]")
        if not (0.0 < self.cvar_alpha < 1.0):
            raise ValueError("cvar_alpha must lie in (0, 1)")
        if self.lead_periods < 0:
            raise ValueError("lead_periods must be >= 0")
        if self.demand_sigma < 0 or self.renewable_sigma < 0:
            raise ValueError("sigma values must be >= 0")
        if not (-1.0 <= self.demand_ar1 <= 1.0):
            raise ValueError("demand_ar1 must lie in [-1, 1]")

    def uncertainty_config(self, seed: int) -> UncertaintyConfig:
        return UncertaintyConfig(
            n_scenarios=self.n_scenarios, seed=seed,
            demand_sigma=self.demand_sigma, renewable_sigma=self.renewable_sigma,
            high_demand_factor=self.high_demand_factor,
            low_renewable_factor=self.low_renewable_factor,
            demand_ar1=self.demand_ar1, weights=dict(self.weights))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "seeds": list(self.seeds),
            "n_scenarios": self.n_scenarios,
            "demand_sigma": self.demand_sigma,
            "renewable_sigma": self.renewable_sigma,
            "demand_ar1": self.demand_ar1,
            "holdout_fraction": self.holdout_fraction,
            "risk_weight": self.risk_weight,
            "cvar_alpha": self.cvar_alpha,
            "lead_periods": self.lead_periods,
            "battery_mode": self.battery_mode,
            "allow_unmet": self.allow_unmet,
            "high_demand_factor": self.high_demand_factor,
            "low_renewable_factor": self.low_renewable_factor,
            "weights": dict(self.weights),
        }


def default_experiment_config() -> ExperimentConfig:
    return ExperimentConfig()


# --------------------------------------------------------------- statistics ----
def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    # linear interpolation between order statistics (numpy-style, no numpy dep)
    rank = 0.95 * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def _describe(values: Iterable[float | None]) -> dict[str, Any]:
    """count/mean/median/std/p95 over the finite values (None entries dropped)."""
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(clean)
    if n == 0:
        return {"count": 0, "mean": None, "median": None, "std": None, "p95": None}
    return {
        "count": n,
        "mean": float(statistics.fmean(clean)),
        "median": float(statistics.median(clean)),
        "std": float(statistics.pstdev(clean)) if n > 1 else 0.0,
        "p95": _p95(clean),
    }


def _cohens_dz(diffs: Sequence[float]) -> float | None:
    """Standardized mean difference for PAIRED samples (Cohen's d_z).

    ``d_z = mean(diff) / sd(diff)``. Returns ``None`` when the SD is zero (no
    variance -> effect size undefined) or fewer than two differences exist. Not
    a significance test.
    """
    clean = [float(d) for d in diffs if d is not None and math.isfinite(float(d))]
    if len(clean) < 2:
        return None
    sd = statistics.stdev(clean)
    if sd == 0.0:
        return None
    return float(statistics.fmean(clean) / sd)


# -------------------------------------------------------------- run records ----
@dataclass
class ExperimentRecord:
    """Full record of a multi-seed experiment."""
    config: ExperimentConfig
    runs: list[dict[str, Any]] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    comparisons: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    runtime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "config": self.config.to_dict(),
            "runtime_seconds": self.runtime_seconds,
            "n_seeds": len(self.config.seeds),
            "aggregate": self.aggregate,
            "comparisons": self.comparisons,
            "runs": self.runs,
        }


# --------------------------------------------------------------- one seed ----
def run_one_seed(problem: ScheduleProblem, config: ExperimentConfig, seed: int
                 ) -> dict[str, Any]:
    """Fit + evaluate all three policies for a single seed and return a record.

    The record contains the scenario split, the stochastic fit summaries, and the
    per-policy out-of-sample evaluations (as dicts). Failure to fit a policy is
    represented by its status, never by fabricated numbers.
    """
    unc_config = config.uncertainty_config(seed)
    scenario_set = generate_scenarios(problem, unc_config)
    in_sample, holdout = split_scenarios(scenario_set,
                                         holdout_fraction=config.holdout_fraction)
    t0 = time.perf_counter()
    record = compare_policies(
        problem, in_sample, holdout,
        risk_weight=config.risk_weight, cvar_alpha=config.cvar_alpha,
        lead_periods=config.lead_periods, battery_mode=config.battery_mode,
        allow_unmet=config.allow_unmet)
    runtime = time.perf_counter() - t0
    record["seed"] = seed
    record["runtime_seconds"] = runtime
    record["uncertainty"] = unc_config.to_dict()
    record["n_total_scenarios"] = len(scenario_set.scenarios)
    record["in_sample_ids"] = [s.id for s in in_sample.scenarios]
    record["holdout_ids"] = [s.id for s in holdout.scenarios]
    return record


# --------------------------------------------------------------- aggregation ----
def _eval_for(run: Mapping[str, Any], policy: str) -> Mapping[str, Any] | None:
    ev = run.get("evaluations", {}).get(policy)
    if ev is None:
        return None
    return ev


def _feasible_run(run: Mapping[str, Any], policy: str) -> bool:
    ev = _eval_for(run, policy)
    return bool(ev is not None and ev.get("status") == "optimal")


def aggregate_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate per-policy metrics across seeds.

    For each policy we report the per-seed metric distributions (mean/median/
    std/p95) plus the feasible-run rate, failure count and the seeds that
    failed. Only optimal evaluations contribute to the metric distributions;
    the feasible-run rate is reported separately so failures stay visible.
    """
    out: dict[str, Any] = {"n_runs": len(runs), "policies": {}}
    metric_keys = (
        ("expected_cost_usd", "usd"),
        ("expected_unmet_energy_mwh", "mwh"),
        ("probability_of_unmet", "prob"),
        ("var_usd", "usd"),
        ("cvar_usd", "usd"),
        ("p95_cost_usd", "usd"),
        ("worst_case_cost_usd", "usd"),
        ("runtime_seconds", "s"),
    )
    for policy in POLICY_NAMES:
        feasible = [r for r in runs if _feasible_run(r, policy)]
        failed_seeds = [r.get("seed") for r in runs if not _feasible_run(r, policy)]
        metrics: dict[str, Any] = {}
        for key, _unit in metric_keys:
            values = [(_eval_for(r, policy) or {}).get(key) for r in feasible]
            metrics[key] = _describe(values)
        # scenario-count and validation coverage
        validation_flags = [(_eval_for(r, policy) or {}).get("validation_all_passed")
                            for r in feasible]
        out["policies"][policy] = {
            "feasible_runs": len(feasible),
            "failure_count": len(runs) - len(feasible),
            "failed_seeds": failed_seeds,
            "feasible_run_rate": (len(feasible) / len(runs)) if runs else None,
            "validation_all_passed_runs": sum(1 for v in validation_flags if v is True),
            "metrics": metrics,
        }
    return out


def _paired_scenario_costs(run: Mapping[str, Any], a: str, b: str
                           ) -> list[float] | None:
    """Per-scenario cost differences (a - b) on the SAME holdout scenarios.

    Both policies must have evaluated the same number of scenarios in the same
    order (they do, because the holdout set is shared and iterated in order). If
    either evaluation is missing/not optimal we return ``None`` so the run is
    excluded rather than silently paired with different scenarios.
    """
    ea = _eval_for(run, a)
    eb = _eval_for(run, b)
    if not (ea and eb):
        return None
    if ea.get("status") != "optimal" or eb.get("status") != "optimal":
        return None
    ca = ea.get("scenario_costs_usd")
    cb = eb.get("scenario_costs_usd")
    if not ca or not cb or len(ca) != len(cb):
        return None
    return [float(x) - float(y) for x, y in zip(ca, cb)]


def compare_aggregate(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Paired cross-seed comparison of policy metric distributions.

    Pairs compared: ``risk_aware - risk_neutral``, ``risk_neutral - deterministic``
    and ``risk_aware - deterministic``. For each pair we pool the per-scenario
    cost differences across seeds and report mean, sd, min, max and Cohen's
    ``dz``. No significance claims are made.
    """
    pairs = (("risk_aware", "risk_neutral"),
             ("risk_neutral", "deterministic"),
             ("risk_aware", "deterministic"))
    out: dict[str, Any] = {"pairs": {}, "metric": "scenario realized cost (USD)"}
    for a, b in pairs:
        pooled: list[float] = []
        seeds_used: list[int] = []
        seeds_skipped: list[int] = []
        for r in runs:
            diffs = _paired_scenario_costs(r, a, b)
            if diffs is None:
                seeds_skipped.append(r.get("seed"))
                continue
            pooled.extend(diffs)
            seeds_used.append(r.get("seed"))
        n = len(pooled)
        summary: dict[str, Any] = {
            "comparison": f"{a} - {b}",
            "seeds_used": len(seeds_used),
            "seeds_skipped": seeds_skipped,
            "n_pairs": n,
        }
        if n:
            summary.update({
                "mean_diff_usd": float(statistics.fmean(pooled)),
                "sd_diff_usd": float(statistics.stdev(pooled)) if n > 1 else 0.0,
                "min_diff_usd": float(min(pooled)),
                "max_diff_usd": float(max(pooled)),
                "cohens_dz": _cohens_dz(pooled),
            })
        else:
            summary.update({"mean_diff_usd": None, "sd_diff_usd": None,
                            "min_diff_usd": None, "max_diff_usd": None,
                            "cohens_dz": None})
        out["pairs"][f"{a}_minus_{b}"] = summary
    return out


# --------------------------------------------------------------- driver ----
def run_comparative_benchmark(problem: ScheduleProblem,
                              config: ExperimentConfig | None = None
                              ) -> ExperimentRecord:
    """Run the full multi-seed experiment and return an :class:`ExperimentRecord`."""
    config = config or default_experiment_config()
    config.validate()
    problem.validate()
    started = time.perf_counter()
    runs = [run_one_seed(problem, config, seed) for seed in config.seeds]
    record = ExperimentRecord(
        config=config, runs=runs,
        aggregate=aggregate_runs(runs),
        comparisons=compare_aggregate(runs),
        started_at=started,
        runtime_seconds=time.perf_counter() - started)
    return record


# --------------------------------------------------------------- writers ----
def write_experiment(record: ExperimentRecord, out_dir: str | Path
                     ) -> dict[str, Path]:
    """Write config, per-run records, per-scenario CSV and aggregate summary.

    Files (all under ``out_dir``):

    * ``experiment_config.json``      - the full protocol (seeds, assumptions).
    * ``runs/seed_<seed>.json``       - one complete run record per seed.
    * ``per_scenario_metrics.csv``    - one row per (seed, policy, scenario).
    * ``aggregate_summary.json``      - cross-seed aggregates + paired comparisons.
    * ``README.txt``                  - human-readable metrics/units/assumptions.

    Existing files are overwritten only inside ``out_dir`` (a dedicated results
    directory); nothing outside it is touched.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    config_path = out / "experiment_config.json"
    config_path.write_text(json.dumps(record.config.to_dict(), indent=2, default=float),
                           encoding="utf-8")
    paths["config"] = config_path

    runs_dir = out / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for run in record.runs:
        seed = run.get("seed")
        run_path = runs_dir / f"seed_{seed}.json"
        run_path.write_text(json.dumps(run, indent=2, default=float), encoding="utf-8")
    paths["runs_dir"] = runs_dir

    csv_path = out / "per_scenario_metrics.csv"
    _write_per_scenario_csv(record, csv_path)
    paths["per_scenario_csv"] = csv_path

    summary_path = out / "aggregate_summary.json"
    summary_path.write_text(json.dumps({
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "config": record.config.to_dict(),
        "runtime_seconds": record.runtime_seconds,
        "aggregate": record.aggregate,
        "comparisons": record.comparisons,
    }, indent=2, default=float), encoding="utf-8")
    paths["aggregate_summary"] = summary_path

    readme_path = out / "README.txt"
    readme_path.write_text(_readme_text(record), encoding="utf-8")
    paths["readme"] = readme_path
    return paths


def _write_per_scenario_csv(record: ExperimentRecord, path: Path) -> None:
    """One row per (seed, policy, holdout scenario) with realized cost/unmet."""
    import csv
    header = ["seed", "policy", "scenario_index", "scenario_id", "cost_usd",
              "unmet_energy_mwh", "probability"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for run in record.runs:
            seed = run.get("seed")
            holdout_ids = run.get("holdout_ids", [])
            for policy in POLICY_NAMES:
                ev = _eval_for(run, policy)
                if not ev or ev.get("status") != "optimal":
                    continue
                costs = ev.get("scenario_costs_usd", [])
                unmet = ev.get("scenario_unmet_mwh", [])
                probs = ev.get("scenario_probabilities", [])
                for i, cost in enumerate(costs):
                    writer.writerow([
                        seed, policy, i,
                        holdout_ids[i] if i < len(holdout_ids) else "",
                        cost,
                        unmet[i] if i < len(unmet) else "",
                        probs[i] if i < len(probs) else ""])


def _readme_text(record: ExperimentRecord) -> str:
    cfg = record.config
    lines = [
        "GridMind - Stage 5A comparative policy benchmark",
        "================================================",
        "",
        "SYNTHETIC DATA. These are model outputs under synthetic scenarios, not",
        "real grid measurements. No significance or superiority claims are made.",
        "",
        f"schema_version: {EXPERIMENT_SCHEMA_VERSION}",
        f"seeds: {list(cfg.seeds)}",
        f"scenarios per seed: {cfg.n_scenarios}  (holdout fraction {cfg.holdout_fraction})",
        f"risk_weight (lambda): {cfg.risk_weight}   CVaR confidence alpha: {cfg.cvar_alpha}",
        f"lead_periods (first-stage commitment): {cfg.lead_periods}",
        f"battery_mode: {cfg.battery_mode}   allow_unmet: {cfg.allow_unmet}",
        f"demand_sigma: {cfg.demand_sigma}  renewable_sigma: {cfg.renewable_sigma}"
        f"  demand_ar1: {cfg.demand_ar1}",
        f"scenario family weights: {dict(cfg.weights)}",
        "",
        "Policies",
        "--------",
        "deterministic : commit the first stage of the mean/base problem.",
        "risk_neutral  : two-stage expected-cost solve (lambda = 0).",
        "risk_aware    : two-stage CVaR-weighted solve (lambda > 0).",
        "All three are fitted on the same in-sample scenarios and evaluated on the",
        "same holdout scenarios by fixing the first stage and re-optimising recourse.",
        "",
        "Metrics and units",
        "-----------------",
        "expected_cost_usd            : probability-weighted mean realized cost (USD).",
        "expected_unmet_energy_mwh    : probability-weighted mean unserved energy (MWh).",
        "probability_of_unmet         : total probability mass of scenarios with unmet > 0.",
        "var_usd                      : alpha-quantile of realized cost (USD).",
        "cvar_usd                     : mean of the worst (1-alpha) cost tail (USD).",
        "p95_cost_usd                 : 95th-percentile realized cost (USD).",
        "worst_case_cost_usd          : max realized cost across holdout scenarios (USD).",
        "runtime_seconds              : out-of-sample evaluation wall time (s).",
        "",
        "Aggregation",
        "-----------",
        "Per policy and metric: count, mean, median, population std and p95 over seeds",
        "(feasible runs only). The feasible-run rate and failure count are reported",
        "separately so failures remain visible and never inflate a mean.",
        "",
        "Paired comparisons",
        "------------------",
        "For each policy pair the per-scenario realized-cost differences are pooled",
        "across seeds (same holdout scenarios on both sides). We report mean diff, sd,",
        "min, max and Cohen's dz = mean(diff) / sd(diff). dz is an effect size, not a",
        "significance test; with synthetic, dependent scenarios no p-value is given.",
        "",
        "Limitations",
        "-----------",
        "* Scenarios are synthetic (Gaussian noise, AR(1) demand, four documented",
        "  stress families) and are NOT a real-world forecast.",
        "* Intermediate branching is not modelled: scenarios share first-stage",
        "  decisions up to lead_periods, then adapt; a fully adaptive policy could",
        "  do better.",
        "* On short horizons and small lead_periods the three policies often coincide",
        "  out-of-sample; near-zero differences are reported as such.",
        "",
    ]
    return "\n".join(lines)


def load_experiment_summary(path: str | Path) -> dict[str, Any]:
    """Load an ``aggregate_summary.json`` written by :func:`write_experiment`."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
