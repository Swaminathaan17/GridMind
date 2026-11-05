"""Stage 5E E9 statistical reliability evidence.

This harness records independent batches of existing qualification workloads and
independent scalability batches. It reports robust distributions, outliers,
distribution-free median intervals and a resampling interval for the empirical
scalability exponent. It does not infer asymptotic complexity or turn
performance thresholds into functional correctness conditions.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks import qualification as q
from benchmarks import scalability as sc
from benchmarks import schema, stats as robust_stats
from benchmarks.progress import ProgressReporter

STATISTICAL_KIND = schema.STATISTICAL_KIND
STATISTICAL_SCHEMA_VERSION = schema.ARTIFACT_SCHEMA_VERSION
DEFAULT_PERFORMANCE_WORKLOADS = ("smoke", "benchmark", "control_default", "mp_benchmark")
DEFAULT_BATCHES = 3
DEFAULT_REPETITIONS = 5
DEFAULT_WARMUPS = 1
DEFAULT_SCALABILITY_REPETITIONS = 3
DEFAULT_BOOTSTRAP_RESAMPLES = robust_stats.DEFAULT_BOOTSTRAP_RESAMPLES
DEFAULT_BOOTSTRAP_SEED = robust_stats.DEFAULT_BOOTSTRAP_SEED


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(q.ROOT).as_posix()
    except ValueError:
        return str(path)


def _check(category: str, name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"category": category, "name": name, "passed": bool(passed), "detail": detail}


def _all(passed: Sequence[bool]) -> bool:
    return bool(passed) and all(passed)


def _stats_summary(values: Sequence[float]) -> dict[str, Any] | None:
    return robust_stats.summarize(values)


def _run_samples(run: Mapping[str, Any], samples_key: str, value_key: str) -> list[float]:
    samples = run.get(samples_key)
    if samples:
        return [float(value) for value in samples if value is not None]
    value = run.get(value_key)
    return [] if value is None else [float(value)]


def collect_performance(out: Path, workloads: Sequence[str], batches: int,
                        repetitions: int, warmups: int,
                        timeout_seconds: float,
                        progress: ProgressReporter | None = None
                        ) -> list[dict[str, Any]]:
    if progress is None:
        progress = ProgressReporter()
    records = []
    for batch in range(1, batches + 1):
        directory = out / "performance" / f"batch-{batch:02d}"
        progress(f"performance batch {batch}/{batches} starting: {directory}")
        started = time.perf_counter()
        try:
            payload = q.measure(directory, workloads, repetitions, timeout_seconds,
                                warmup_repetitions=warmups, progress=progress)
        except KeyboardInterrupt:
            progress(f"interrupted during performance batch {batch}/{batches}")
            raise
        progress(f"performance batch {batch}/{batches} completed in "
                 f"{time.perf_counter() - started:.3f}s")
        records.append({"batch": batch, "path": _relative(directory), "payload": payload})
    return records


def collect_scalability(out: Path, batches: int, repetitions: int,
                        timeout_seconds: float,
                        progress: ProgressReporter | None = None
                        ) -> list[dict[str, Any]]:
    if progress is None:
        progress = ProgressReporter()
    records = []
    for batch in range(1, batches + 1):
        directory = out / "scalability" / f"batch-{batch:02d}"
        progress(f"scalability batch {batch}/{batches} starting: {directory}")
        started = time.perf_counter()
        try:
            payload = sc.run_scalability(directory, repetitions=repetitions,
                                         timeout_seconds=timeout_seconds,
                                         progress=progress)
        except KeyboardInterrupt:
            progress(f"interrupted during scalability batch {batch}/{batches}")
            raise
        progress(f"scalability batch {batch}/{batches} completed in "
                 f"{time.perf_counter() - started:.3f}s")
        records.append({"batch": batch, "path": _relative(directory), "payload": payload})
    return records


def analyze_performance(records: Sequence[Mapping[str, Any]],
                        baseline: Mapping[str, Any],
                        expected_repetitions: int) -> dict[str, Any]:
    names = list(records[0]["payload"]["harness"]["workloads"])
    workloads: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    for name in names:
        reps = [rep for record in records
                for rep in record["payload"]["workloads"][name]["results"]]
        warmups = [rep for record in records
                   for rep in record["payload"]["workloads"][name].get("warmups", [])]
        ok = [rep for rep in reps if rep.get("status") == "ok"]
        wall = [float(rep["wall_seconds"]) for rep in ok if rep.get("wall_seconds") is not None]
        solver = [float(rep["solver_seconds"]) for rep in ok
                  if rep.get("solver_seconds") is not None]
        rss = [float(rep["peak_rss_mb"]) for rep in ok if rep.get("peak_rss_mb") is not None]
        if not ok or not wall:
            raise q.QualificationError(
                f"workload {name}: no successful measured repetitions; "
                "cannot compute reliability statistics")
        batch_counts = [len(record["payload"]["workloads"][name]["results"])
                        for record in records]
        expected_per_batch = expected_repetitions // len(records)
        warmup_counts = [len(record["payload"]["workloads"][name].get("warmups", []))
                         for record in records]
        hashes = sorted({str(rep.get("decision_sha256")) for rep in ok
                         if rep.get("decision_sha256")})
        solvers = sorted({str(rep.get("solver")) for rep in ok if rep.get("solver")})
        summary = {
            "wall_seconds": _stats_summary(wall),
            "solver_seconds": _stats_summary(solver),
            "peak_rss_mb": _stats_summary(rss),
        }
        batch_medians = [record["payload"]["workloads"][name]["wall_seconds"]["median"]
                         for record in records]
        limits = baseline["workloads"][name]["thresholds"]
        wall_ci = summary["wall_seconds"]["median_interval"] if summary["wall_seconds"] else None
        solver_ci = (summary["solver_seconds"]["median_interval"]
                     if summary["solver_seconds"] else None)
        all_ok = len(ok) == len(reps) == expected_repetitions
        wall_median = summary["wall_seconds"]["median"] if summary["wall_seconds"] else None
        solver_median = (summary["solver_seconds"]["median"]
                         if summary["solver_seconds"] else None)
        checks.extend([
            _check("functional", f"{name}:repetitions_complete", all_ok,
                   f"ok={len(ok)} measured={len(reps)} expected={expected_repetitions}"),
            _check("functional", f"{name}:decision_hash_stable", len(hashes) == 1,
                   f"hashes={hashes}"),
            _check("functional", f"{name}:solver_stable", len(solvers) == 1,
                   f"solvers={solvers}"),
            _check("statistical", f"{name}:batches_complete",
                   len(records) >= 2
                   and all(count == expected_per_batch for count in batch_counts),
                   f"batches={len(records)} counts={batch_counts} "
                   f"expected_per_batch={expected_per_batch}"),
            _check("statistical", f"{name}:warmups_recorded",
                   all(count >= 1 for count in warmup_counts),
                   f"warmups={warmup_counts}"),
            _check("statistical", f"{name}:wall_samples", len(wall) == expected_repetitions,
                   f"count={len(wall)} expected={expected_repetitions}"),
            _check("statistical", f"{name}:wall_median_interval", wall_ci is not None,
                   f"interval={wall_ci}"),
            _check("performance", f"{name}:wall_median_point",
                   wall_median is not None
                   and float(wall_median) <= float(limits["wall_seconds_max"]),
                   f"median={wall_median} limit={limits['wall_seconds_max']}"),
            _check("performance", f"{name}:wall_median_upper_bound",
                   wall_ci is not None and wall_ci["upper"] <= float(limits["wall_seconds_max"]),
                   f"upper={None if wall_ci is None else wall_ci['upper']} "
                   f"limit={limits['wall_seconds_max']}"),
            _check("performance", f"{name}:solver_median_point",
                   solver_median is not None
                   and float(solver_median) <= float(limits["solver_seconds_max"]),
                   f"median={solver_median} limit={limits['solver_seconds_max']}"),
            _check("performance", f"{name}:solver_median_upper_bound",
                   solver_ci is not None
                   and solver_ci["upper"] <= float(limits["solver_seconds_max"]),
                   f"upper={None if solver_ci is None else solver_ci['upper']} "
                   f"limit={limits['solver_seconds_max']}"),
            _check("performance", f"{name}:rss_max",
                   bool(rss) and max(rss) <= float(limits["peak_rss_mb_max"]),
                   f"max={max(rss) if rss else None} limit={limits['peak_rss_mb_max']}"),
        ])
        workloads[name] = {
            "repetitions_total": len(reps),
            "warmup_repetitions_total": len(warmups),
            "samples": {"wall_seconds": wall, "solver_seconds": solver,
                        "peak_rss_mb": rss},
            "summary": summary,
            "batch_wall_medians": batch_medians,
            "batch_median_span": robust_stats.median_span(batch_medians),
            "decision_hashes": hashes,
            "solvers": solvers,
            "baseline_limits": limits,
        }
    return {"workloads": workloads, "checks": checks}


def _per_size_samples(records: Sequence[Mapping[str, Any]], kind: str,
                      samples_key: str, value_key: str) -> dict[int, list[float]]:
    groups: dict[int, list[float]] = {}
    for record in records:
        for run in record["payload"]["families"][kind]["runs"]:
            samples = _run_samples(run, samples_key, value_key)
            if not samples:
                continue
            groups.setdefault(int(run["size"]), []).extend(samples)
    return groups


def analyze_scalability(records: Sequence[Mapping[str, Any]],
                        expected_repetitions: int) -> dict[str, Any]:
    families: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    threshold_shapes = {json.dumps(record["payload"]["thresholds"], sort_keys=True)
                        for record in records}
    thresholds = records[0]["payload"]["thresholds"]
    for kind in ("dispatch_lp", "schedule_mip"):
        wall_groups = _per_size_samples(records, kind, "wall_samples_seconds", "wall_seconds")
        solver_groups = _per_size_samples(records, kind, "solver_samples_seconds",
                                          "solver_seconds")
        rss_groups = _per_size_samples(records, kind, "rss_samples_mb", "peak_rss_mb")
        sizes = sorted(wall_groups)
        if len(sizes) < 2:
            raise q.QualificationError(
                f"{kind}: fewer than two successful scalability sizes; "
                "cannot compute growth evidence")
        runs = [run for record in records
                for run in record["payload"]["families"][kind]["runs"]]
        ok = [run for run in runs if run.get("status") == "ok"]
        solvers = sorted({str(run.get("solver")) for run in ok if run.get("solver")})
        objectives: dict[int, set[float]] = {}
        for run in ok:
            values = run.get("objectives_usd") or [run.get("objective_usd")]
            for value in values:
                if value is not None and math.isfinite(float(value)):
                    objectives.setdefault(int(run["size"]), set()).add(round(float(value), 6))
        objectives_stable = bool(objectives) and all(len(values) == 1
                                                     for values in objectives.values())
        all_ok = (len(ok) == len(runs)
                  and all(run.get("solver_status") == "optimal" for run in ok)
                  and all(run.get("validation_passed") is True for run in ok))
        replicate_counts = {size: len(values) for size, values in wall_groups.items()}
        expected_per_size = expected_repetitions * len(records)
        medians = {size: robust_stats.percentile(values, 0.5)
                   for size, values in wall_groups.items()}
        rss_medians = {size: robust_stats.percentile(values, 0.5)
                       for size, values in rss_groups.items()}
        first, last = sizes[0], sizes[-1]
        exponent = sc.growth_exponent(first, medians[first], last, medians[last])

        def exponent_for(group_medians: Mapping[int, float]) -> float:
            value = sc.growth_exponent(first, group_medians[first],
                                       last, group_medians[last])
            return math.nan if value is None else value

        exponent_ci = robust_stats.bootstrap_groups_interval(
            wall_groups, exponent_for, seed=robust_stats.DEFAULT_BOOTSTRAP_SEED)
        rss_growth = rss_medians[last] - rss_medians[first]
        rss_factor = rss_medians[last] / rss_medians[first]
        per_size = {
            size: {
                "wall_seconds": _stats_summary(wall_groups[size]),
                "solver_seconds": _stats_summary(solver_groups[size]),
                "peak_rss_mb": _stats_summary(rss_groups[size]),
                "objectives_usd": sorted(objectives.get(size, set())),
            }
            for size in sizes
        }
        checks.extend([
            _check("functional", f"{kind}:all_runs_ok", all_ok,
                   f"ok={len(ok)} total={len(runs)}"),
            _check("functional", f"{kind}:objectives_stable", objectives_stable,
                   f"objectives={ {size: sorted(values) for size, values in objectives.items()} }"),
            _check("functional", f"{kind}:solver_stable", len(solvers) == 1,
                   f"solvers={solvers}"),
            _check("statistical", f"{kind}:size_count", len(sizes) >= 2,
                   f"sizes={sizes}"),
            _check("statistical", f"{kind}:replicates_per_size",
                   all(count >= expected_per_size for count in replicate_counts.values()),
                   f"counts={replicate_counts} expected>={expected_per_size}"),
            _check("statistical", f"{kind}:exponent_interval",
                   exponent_ci is not None, f"interval={exponent_ci}"),
            _check("scalability", f"{kind}:time_exponent_point",
                   exponent is not None
                   and exponent <= float(thresholds["max_time_exponent"]),
                   f"exponent={exponent} limit={thresholds['max_time_exponent']}"),
            _check("scalability", f"{kind}:time_exponent_interval_upper",
                   exponent_ci is not None
                   and exponent_ci["upper"] <= float(thresholds["max_time_exponent"]),
                   f"upper={None if exponent_ci is None else exponent_ci['upper']} "
                   f"limit={thresholds['max_time_exponent']}"),
            _check("scalability", f"{kind}:rss_growth_mb",
                   rss_growth <= float(thresholds["max_rss_growth_mb"]),
                   f"growth={rss_growth:.3f} limit={thresholds['max_rss_growth_mb']}"),
            _check("scalability", f"{kind}:rss_growth_factor",
                   rss_factor <= float(thresholds["max_rss_growth_factor"]),
                   f"factor={rss_factor:.3f} limit={thresholds['max_rss_growth_factor']}"),
        ])
        families[kind] = {
            "sizes": sizes,
            "per_size": per_size,
            "growth": {
                "time_exponent": exponent,
                "time_exponent_bootstrap_interval": exponent_ci,
                "rss_growth_mb": rss_growth,
                "rss_growth_factor": rss_factor,
                "size_range": [first, last],
            },
            "replicates_per_size": replicate_counts,
        }

    leaks = [record["payload"]["leak"] for record in records]
    leak_suspected = any(bool(leak.get("leak_suspected")) for leak in leaks)
    leak_growths = [float(leak["rss_growth_mb"]) for leak in leaks
                    if leak.get("rss_growth_mb") is not None]
    checks.append(_check("functional", "leak:not_suspected", not leak_suspected,
                         f"suspected={leak_suspected} batches={len(leaks)}"))
    return {
        "families": families,
        "thresholds": thresholds,
        "thresholds_consistent": len(threshold_shapes) == 1,
        "leak": {
            "leak_suspected": leak_suspected,
            "rss_growth_mb": leak_growths,
            "batch_values": [{"batch": record["batch"], **{
                key: value for key, value in record["payload"]["leak"].items()
                if key != "worker"}} for record in records],
        },
        "checks": checks,
    }


def run_reliability(out_dir: str | Path, *, baseline_path: str | Path | None = None,
                    performance_workloads: Sequence[str] = DEFAULT_PERFORMANCE_WORKLOADS,
                    batches: int = DEFAULT_BATCHES,
                    repetitions: int = DEFAULT_REPETITIONS,
                    warmups: int = DEFAULT_WARMUPS,
                    scalability_repetitions: int = DEFAULT_SCALABILITY_REPETITIONS,
                    timeout_seconds: float = 1800.0,
                    progress: ProgressReporter | None = None) -> dict[str, Any]:
    out = Path(out_dir)
    if progress is None:
        progress = ProgressReporter()
    if out.exists():
        raise q.QualificationError(f"refusing to overwrite existing evidence directory: {out}")
    if batches < 2:
        raise q.QualificationError("batches must be at least 2 for reliability evidence")
    if repetitions < 2 or scalability_repetitions < 2:
        raise q.QualificationError("repetitions must be at least 2")
    if warmups < 1:
        raise q.QualificationError("warmups must be at least 1 for reliability evidence")
    baseline_file = Path(baseline_path) if baseline_path else q.DEFAULT_BASELINE
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    baseline_errors = schema.validate_baseline_artifact(baseline)
    if baseline_errors:
        raise q.QualificationError("invalid baseline:\n- " + "\n- ".join(baseline_errors))
    unknown = sorted(set(performance_workloads) - set(baseline["workloads"]))
    if unknown:
        raise q.QualificationError(f"unknown baseline workloads: {unknown}")
    out.mkdir(parents=True)
    progress(f"E9 reliability run starting: out={out} batches={batches} "
             f"workloads={list(performance_workloads)} repetitions={repetitions} "
             f"warmups={warmups} scalability_repetitions={scalability_repetitions}")
    started_at_utc = q._utc_now()
    environment = q.collect_environment()
    try:
        performance_records = collect_performance(
            out, list(performance_workloads), batches, repetitions, warmups,
            timeout_seconds, progress=progress)
        scalability_records = collect_scalability(
            out, batches, scalability_repetitions, timeout_seconds, progress=progress)
        progress("statistical analysis starting: performance")
        analysis_started = time.perf_counter()
        performance = analyze_performance(
            performance_records, baseline, batches * repetitions)
        progress("statistical analysis completed: performance in "
                 f"{time.perf_counter() - analysis_started:.3f}s")
        progress("statistical analysis starting: scalability")
        analysis_started = time.perf_counter()
        scalability = analyze_scalability(
            scalability_records, scalability_repetitions)
        progress("statistical analysis completed: scalability in "
                 f"{time.perf_counter() - analysis_started:.3f}s")
    except KeyboardInterrupt:
        progress("E9 reliability run interrupted; no reliability.json artifact written")
        raise
    checks = performance["checks"] + scalability["checks"]
    categories = {
        category: _all([check["passed"] for check in checks
                        if check["category"] == category])
        for category in ("functional", "statistical", "performance", "scalability")
    }
    passed = _all([check["passed"] for check in checks]) and scalability["thresholds_consistent"]
    finished_at_utc = q._utc_now()
    payload = {
        "schema_version": STATISTICAL_SCHEMA_VERSION,
        "kind": STATISTICAL_KIND,
        "generated_at_utc": finished_at_utc,
        "environment": environment,
        "provenance": q._provenance(environment, started_at_utc, finished_at_utc),
        "methodology": {
            "batches": batches,
            "repetitions_per_batch": repetitions,
            "warmup_repetitions_per_batch": warmups,
            "measured_repetitions_per_workload": batches * repetitions,
            "scalability_batches": batches,
            "scalability_repetitions_per_size": scalability_repetitions,
            "primary_location": "median",
            "median_interval": "distribution-free binomial order statistic, nominal 0.95",
            "median_interval_method": (
                "order statistics of a two-sided binomial sign test; achieved coverage is "
                "recorded and no interval is reported when the sample is too small"),
            "minimum_samples_for_median_interval": (
                robust_stats.minimum_median_interval_samples()),
            "sample_requirement": (
                "at least two independent batches and two measured repetitions per batch; "
                "each performance workload uses all batch repetitions pooled for its interval"),
            "dispersion": ["IQR", "MAD", "sample standard deviation", "CV"],
            "outliers": "Tukey 1.5*IQR flagged and retained; raw samples are never removed",
            "warmup_policy": (
                "at least one warm-up child per workload and batch is recorded separately and "
                "excluded from all measured statistics"),
            "decision_reproducibility": "all canonical decision hashes must match",
            "performance_decision": (
                "the median point estimate and the conservative upper bound of the 95% median "
                "interval must both stay within the baseline limit; the upper-bound check "
                "exposes statistical uncertainty separately from a pure regression"),
            "scalability_decision": (
                "point exponent and bootstrap 95% upper bound must not exceed the configured "
                "limit; reported as uncertainty over observed sizes, not an asymptotic proof"),
            "bootstrap": {
                "method": "percentile bootstrap over independent per-size samples and medians",
                "resamples": DEFAULT_BOOTSTRAP_RESAMPLES,
                "seed": DEFAULT_BOOTSTRAP_SEED,
            },
        },
        "acceptance": {
            "passed": passed,
            "categories": categories,
            "checks": checks,
            "baseline": _relative(baseline_file),
        },
        "performance": {
            "batches": [{"batch": record["batch"], "path": record["path"],
                         "repetitions": record["payload"]["harness"]["repetitions"],
                         "warmup_repetitions": record["payload"]["harness"]
                         ["warmup_repetitions"]} for record in performance_records],
            **performance,
        },
        "scalability": {
            "batches": [{"batch": record["batch"], "path": record["path"],
                         "summary": record["payload"]["summary"]}
                        for record in scalability_records],
            **scalability,
        },
    }
    errors = schema.validate_statistical_artifact(payload)
    if errors:
        raise q.QualificationError("invalid statistical artifact:\n- " + "\n- ".join(errors))
    artifact = out / "reliability.json"
    temporary = out / ".reliability.json.tmp"
    try:
        q._write_json(temporary, payload)
        temporary.replace(artifact)
    except KeyboardInterrupt:
        temporary.unlink(missing_ok=True)
        progress("E9 reliability run interrupted while writing artifact; "
                 "no completed reliability.json was installed")
        raise
    progress(f"artifact written: {artifact}")
    progress(f"acceptance passed={payload['acceptance']['passed']} "
             f"categories={payload['acceptance']['categories']}")
    return payload


def _cmd_run(args: argparse.Namespace) -> int:
    progress = ProgressReporter(enabled=not args.quiet)
    payload = run_reliability(
        args.out, baseline_path=args.baseline,
        performance_workloads=args.workloads, batches=args.batches,
        repetitions=args.repetitions, warmups=args.warmups,
        scalability_repetitions=args.scalability_repetitions,
        timeout_seconds=args.timeout, progress=progress)
    print(f"wrote {Path(args.out) / 'reliability.json'}")
    for name, workload in payload["performance"]["workloads"].items():
        wall = workload["summary"]["wall_seconds"]
        print(f"  {name:<16} n={wall['count']:<3} median={wall['median']:.6f} "
              f"ci_upper={None if wall['median_interval'] is None else wall['median_interval']['upper']}")
    for kind, family in payload["scalability"]["families"].items():
        growth = family["growth"]
        interval = growth["time_exponent_bootstrap_interval"]
        print(f"  {kind:<12} exponent={growth['time_exponent']} "
              f"ci_upper={None if interval is None else interval['upper']}")
    print(f"acceptance passed={payload['acceptance']['passed']} "
          f"categories={payload['acceptance']['categories']}")
    return 0 if payload["acceptance"]["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="collect E9 reliability evidence")
    run_parser.add_argument("--out", required=True, help="new output directory")
    run_parser.add_argument("--baseline", help="baseline JSON (default: benchmarks/baseline.json)")
    run_parser.add_argument("--workloads", nargs="+", default=list(DEFAULT_PERFORMANCE_WORKLOADS))
    run_parser.add_argument("--batches", type=int, default=DEFAULT_BATCHES)
    run_parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    run_parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    run_parser.add_argument("--scalability-repetitions", type=int,
                            default=DEFAULT_SCALABILITY_REPETITIONS)
    run_parser.add_argument("--timeout", type=float, default=1800.0)
    run_parser.add_argument("--quiet", action="store_true",
                            help="suppress timestamped progress on stderr")
    run_parser.set_defaults(func=_cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except q.QualificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
