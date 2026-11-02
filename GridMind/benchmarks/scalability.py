"""Opt-in scalability and memory evidence for Stage 5E.

The harness runs representative LP and MIP workload sizes in fresh child
processes and records wall time, solver time and peak RSS for each size. A
repeated-run probe on one fixed workload looks for monotonic RSS growth that
would indicate a leak.

The checks are guardrails, not proofs of asymptotic complexity:

* every run must solve optimally and pass independent validation;
* time growth is bounded by a generous empirical exponent (default 3.0);
* RSS growth between the smallest and largest size is bounded;
* repeated identical runs must not grow RSS beyond a configurable tolerance.

Run from the ``GridMind`` directory::

    python -m benchmarks.scalability run --out benchmarks/runs/scalability_<label>

This is intentionally not part of the default test suite.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks import schema, stats as robust_stats
from benchmarks.qualification import (
    ROOT,
    QualificationError,
    _load_json,
    _provenance,
    _utc_now,
    _write_json,
    collect_environment,
    peak_rss_mb,
)

SCALABILITY_SCHEMA_VERSION = schema.ARTIFACT_SCHEMA_VERSION
SCALABILITY_KIND = schema.SCALABILITY_KIND
DEFAULT_LP_SIZES = (10, 40, 160)
DEFAULT_MIP_SIZES = (8, 24, 48)
DEFAULT_LEAK_REPETITIONS = 5
DEFAULT_MAX_EXPONENT = 3.0
DEFAULT_MAX_RSS_GROWTH_MB = 512.0
DEFAULT_MAX_RSS_GROWTH_FACTOR = 4.0
DEFAULT_LEAK_TOLERANCE_MB = 64.0
DEFAULT_LEAK_TOLERANCE_FRACTION = 0.10


def _dispatch_network(nodes: int) -> Any:
    from gridmind.network import Generator, Line, Network, Node

    if nodes < 2:
        raise ValueError("dispatch network needs at least two nodes")
    node_list = tuple(
        Node(f"N{i}", f"N{i}", 5.0 if i else 0.0) for i in range(nodes))
    lines = tuple(
        Line(f"L{i}", f"L{i}", f"N{i}", f"N{i + 1}", capacity_mw=float(nodes * 20))
        for i in range(nodes - 1))
    generator = Generator("G0", "G0", "N0", "gas", float(nodes * 20), 10.0)
    return Network(name=f"scalability-lp-{nodes}", nodes=node_list,
                   generators=(generator,), lines=lines,
                   unmet_penalty_per_mwh=1000.0)


def _mip_problem(periods: int) -> Any:
    from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
    from gridmind.network import Generator, Network, Node

    network = Network(
        name=f"scalability-mip-{periods}",
        nodes=(Node("bus", "bus", 10.0),),
        generators=(Generator("cheap", "Cheap", "bus", "gas", 20.0, 10.0),),
        lines=(),
        unmet_penalty_per_mwh=1000.0,
    )
    horizon = Horizon(
        periods=periods,
        step_hours=1.0,
        demand_mw=tuple({"bus": 10.0} for _ in range(periods)),
        availability_mw=tuple({"cheap": 20.0} for _ in range(periods)),
        generator_online=tuple({"cheap": True} for _ in range(periods)),
        line_in_service=tuple({} for _ in range(periods)),
    )
    battery = Battery("b1", "bus", capacity_mwh=20.0, max_charge_mw=5.0,
                      max_discharge_mw=5.0, eta_charge=0.95, eta_discharge=0.95,
                      initial_soc_mwh=10.0, min_soc_mwh=0.0, final_soc_mwh=10.0)
    problem = ScheduleProblem(network=network, horizon=horizon, batteries=(battery,))
    problem.validate()
    return problem


def _solve_once(kind: str, size: int) -> tuple[Any, float]:
    started = time.perf_counter()
    if kind == "dispatch_lp":
        from gridmind.optimizer import solve_dispatch
        result = solve_dispatch(_dispatch_network(size))
    elif kind == "schedule_mip":
        from gridmind.scheduling import solve_schedule
        result = solve_schedule(_mip_problem(size), battery_mode="exclusive")
    else:
        raise ValueError(f"unknown scalability workload: {kind}")
    return result, time.perf_counter() - started


def run_worker(kind: str, size: int, repetitions: int = 1) -> dict[str, Any]:
    """Run one workload size (optionally repeatedly) and sample RSS."""
    started_at_utc = _utc_now()
    rss_samples: list[float] = []
    wall_samples: list[float] = []
    last = None
    error = None
    for _ in range(max(1, repetitions)):
        try:
            last, wall = _solve_once(kind, size)
        except Exception as exc:  # noqa: BLE001 - recorded evidence
            error = f"{type(exc).__name__}: {exc}"
            break
        wall_samples.append(round(wall, 6))
        sample = peak_rss_mb()
        if sample is not None:
            rss_samples.append(float(sample))
    validation = None if last is None else last.validation
    status = "error" if error else ("ok" if last is not None else "error")
    return {
        "schema_version": SCALABILITY_SCHEMA_VERSION,
        "kind": "gridmind_scalability_worker",
        "workload": kind,
        "size": int(size),
        "repetitions": max(1, repetitions),
        "status": status,
        "started_at_utc": started_at_utc,
        "finished_at_utc": _utc_now(),
        "solver": None if last is None else last.solver,
        "solver_status": None if last is None else last.status,
        "solver_seconds": None if last is None else round(float(last.runtime_seconds), 6),
        "objective_usd": None if last is None else last.objective,
        "validation_passed": None if validation is None else bool(validation.passed),
        "wall_seconds": round(sum(wall_samples), 6) if wall_samples else None,
        "wall_samples_seconds": wall_samples,
        "rss_samples_mb": rss_samples,
        "peak_rss_mb": max(rss_samples) if rss_samples else None,
        "error": error,
    }


def _run_child(kind: str, size: int, repetitions: int, out_dir: Path,
               timeout_seconds: float, probe_name: str | None = None) -> dict[str, Any]:
    probe_dir = out_dir / (probe_name or f"{kind}-{size}")
    probe_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("GRIDMIND_SOLVER_PATH", None)
    command = [sys.executable, "-m", "benchmarks.scalability", "_worker",
               "--kind", kind, "--size", str(size),
               "--repetitions", str(repetitions), "--out", str(probe_dir)]
    try:
        proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"status": "error", "workload": kind, "size": size,
                "error": f"worker timed out after {timeout_seconds}s: {exc}"}
    result_path = probe_dir / "worker_result.json"
    if proc.returncode == 0 and result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    return {"status": "error", "workload": kind, "size": size,
            "error": (proc.stderr.strip() or proc.stdout.strip()
                      or f"worker exited with {proc.returncode}")}


def combine_replicate_runs(kind: str, size: int,
                           replicates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine independent child runs into one size-level record."""
    if len(replicates) == 1:
        return dict(replicates[0])
    ok = [run for run in replicates if run.get("status") == "ok"]
    wall = [value for run in ok for value in (run.get("wall_samples_seconds")
                                              or [run.get("wall_seconds")])]
    solver = [value for run in ok for value in (run.get("solver_samples_seconds")
                                                or [run.get("solver_seconds")])]
    rss = [value for run in ok for value in (run.get("rss_samples_mb")
                                             or [run.get("peak_rss_mb")])]
    objectives = sorted({round(float(run["objective_usd"]), 6) for run in ok
                         if _finite(run.get("objective_usd"))})
    solvers = sorted({str(run.get("solver")) for run in ok if run.get("solver")})
    wall_stats = robust_stats.summarize(wall)
    solver_stats = robust_stats.summarize(solver)
    rss_stats = robust_stats.summarize(rss)
    all_ok = len(ok) == len(replicates) and len(objectives) == 1 and len(solvers) == 1
    errors = [str(run.get("error")) for run in replicates if run.get("status") != "ok"]
    if len(objectives) > 1:
        errors.append(f"objective drift across replicates: {objectives}")
    if len(solvers) > 1:
        errors.append(f"solver drift across replicates: {solvers}")
    return {
        "schema_version": SCALABILITY_SCHEMA_VERSION,
        "kind": "gridmind_scalability_worker",
        "workload": kind,
        "size": int(size),
        "repetitions": len(replicates),
        "status": "ok" if all_ok else "error",
        "started_at_utc": replicates[0].get("started_at_utc"),
        "finished_at_utc": replicates[-1].get("finished_at_utc"),
        "solver": solvers[0] if len(solvers) == 1 else None,
        "solver_status": "optimal" if ok and all(
            run.get("solver_status") == "optimal" for run in ok) else None,
        "solver_seconds": solver_stats["median"] if solver_stats else None,
        "solver_samples_seconds": solver,
        "solver_stats": solver_stats,
        "objective_usd": objectives[0] if len(objectives) == 1 else None,
        "objectives_usd": objectives,
        "validation_passed": bool(ok) and all(run.get("validation_passed") is True for run in ok),
        "wall_seconds": wall_stats["median"] if wall_stats else None,
        "wall_samples_seconds": wall,
        "wall_stats": wall_stats,
        "rss_samples_mb": rss,
        "rss_stats": rss_stats,
        "peak_rss_mb": rss_stats["max"] if rss_stats else None,
        "error": "; ".join(errors) if errors else None,
        "replicates": list(replicates),
    }


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def growth_exponent(small_size: int, small_value: float,
                    large_size: int, large_value: float) -> float | None:
    """Empirical exponent p in value ~ size**p, or None when undefined."""
    if not (_finite(small_size) and _finite(large_size)
            and _finite(small_value) and _finite(large_value)):
        return None
    if small_size <= 0 or large_size <= small_size or small_value <= 0 or large_value <= 0:
        return None
    return math.log(large_value / small_value) / math.log(large_size / small_size)


def assess_growth(runs: Sequence[Mapping[str, Any]], *,
                  max_exponent: float = DEFAULT_MAX_EXPONENT,
                  max_rss_growth_mb: float = DEFAULT_MAX_RSS_GROWTH_MB,
                  max_rss_growth_factor: float = DEFAULT_MAX_RSS_GROWTH_FACTOR
                  ) -> dict[str, Any]:
    usable = [r for r in runs if r.get("status") == "ok"
              and _finite(r.get("wall_seconds")) and _finite(r.get("peak_rss_mb"))]
    usable.sort(key=lambda r: int(r["size"]))
    checks: list[dict[str, Any]] = []
    if len(usable) < 2:
        checks.append({"name": "growth_data", "passed": False,
                       "detail": "at least two successful sizes are required"})
        return {"checks": checks, "time_exponent": None, "rss_growth_mb": None,
                "rss_growth_factor": None}
    first, last = usable[0], usable[-1]
    exponent = growth_exponent(int(first["size"]), float(first["wall_seconds"]),
                               int(last["size"]), float(last["wall_seconds"]))
    rss_growth = float(last["peak_rss_mb"]) - float(first["peak_rss_mb"])
    rss_factor = (float(last["peak_rss_mb"]) / float(first["peak_rss_mb"])
                  if float(first["peak_rss_mb"]) > 0 else None)
    checks.append({"name": "time_exponent", "passed":
                   exponent is not None and exponent <= max_exponent,
                   "detail": f"exponent={exponent!r} limit={max_exponent}"})
    checks.append({"name": "rss_growth_mb", "passed": rss_growth <= max_rss_growth_mb,
                   "detail": f"growth={rss_growth:.1f}MB limit={max_rss_growth_mb}MB"})
    checks.append({"name": "rss_growth_factor", "passed":
                   rss_factor is not None and rss_factor <= max_rss_growth_factor,
                   "detail": f"factor={rss_factor!r} limit={max_rss_growth_factor}"})
    return {
        "checks": checks,
        "time_exponent": exponent,
        "rss_growth_mb": rss_growth,
        "rss_growth_factor": rss_factor,
    }


def assess_leak(worker: Mapping[str, Any], *,
                tolerance_mb: float = DEFAULT_LEAK_TOLERANCE_MB,
                tolerance_fraction: float = DEFAULT_LEAK_TOLERANCE_FRACTION
                ) -> dict[str, Any]:
    samples = [float(v) for v in worker.get("rss_samples_mb") or [] if _finite(v)]
    if len(samples) < 2:
        return {"checks": [{"name": "leak_samples", "passed": False,
                            "detail": "at least two RSS samples are required"}],
                "rss_growth_mb": None, "rss_growth_fraction": None,
                "leak_suspected": True}
    growth = samples[-1] - samples[0]
    fraction = growth / samples[0] if samples[0] > 0 else None
    leaked = growth > tolerance_mb or (
        fraction is not None and fraction > tolerance_fraction)
    return {
        "checks": [{
            "name": "repeated_run_rss_growth",
            "passed": not leaked,
            "detail": (f"growth={growth:.1f}MB fraction={fraction!r} "
                       f"limits={tolerance_mb}MB/{tolerance_fraction:.0%}"),
        }],
        "rss_growth_mb": growth,
        "rss_growth_fraction": fraction,
        "leak_suspected": leaked,
    }


def run_scalability(out_dir: str | Path, *,
                    lp_sizes: Sequence[int] = DEFAULT_LP_SIZES,
                    mip_sizes: Sequence[int] = DEFAULT_MIP_SIZES,
                    leak_repetitions: int = DEFAULT_LEAK_REPETITIONS,
                    leak_size: int | None = None,
                    timeout_seconds: float = 900.0,
                    repetitions: int = 1,
                    max_exponent: float = DEFAULT_MAX_EXPONENT,
                    max_rss_growth_mb: float = DEFAULT_MAX_RSS_GROWTH_MB,
                    max_rss_growth_factor: float = DEFAULT_MAX_RSS_GROWTH_FACTOR,
                    leak_tolerance_mb: float = DEFAULT_LEAK_TOLERANCE_MB,
                    leak_tolerance_fraction: float = DEFAULT_LEAK_TOLERANCE_FRACTION
                    ) -> dict[str, Any]:
    out = Path(out_dir)
    if out.exists():
        raise QualificationError(f"refusing to overwrite existing evidence directory: {out}")
    if not lp_sizes or not mip_sizes:
        raise QualificationError("both LP and MIP size lists must be non-empty")
    if leak_repetitions < 2:
        raise QualificationError("leak_repetitions must be at least 2")
    if repetitions < 1:
        raise QualificationError("repetitions must be at least 1")
    out.mkdir(parents=True)
    started_at_utc = _utc_now()
    environment = collect_environment()
    families: dict[str, Any] = {}
    all_checks: list[dict[str, Any]] = []
    for kind, sizes in (("dispatch_lp", lp_sizes), ("schedule_mip", mip_sizes)):
        runs = []
        for size in sizes:
            replicates = []
            for index in range(1, repetitions + 1):
                probe_name = (f"{kind}-{size}" if repetitions == 1
                              else f"{kind}-{size}-rep-{index:02d}")
                replicates.append(_run_child(kind, int(size), 1, out, timeout_seconds,
                                             probe_name=probe_name))
            runs.append(combine_replicate_runs(kind, int(size), replicates))
        growth = assess_growth(runs, max_exponent=max_exponent,
                               max_rss_growth_mb=max_rss_growth_mb,
                               max_rss_growth_factor=max_rss_growth_factor)
        families[kind] = {"sizes": [int(s) for s in sizes], "runs": runs, "growth": growth}
        all_checks.extend({"family": kind, **check} for check in growth["checks"])

    selected_leak_size = int(leak_size if leak_size is not None else mip_sizes[0])
    leak_worker = _run_child("schedule_mip", selected_leak_size, int(leak_repetitions),
                             out, timeout_seconds)
    leak = {"size": selected_leak_size, "repetitions": int(leak_repetitions),
            "worker": leak_worker, **assess_leak(
                leak_worker, tolerance_mb=leak_tolerance_mb,
                tolerance_fraction=leak_tolerance_fraction)}
    all_checks.extend({"family": "leak", **check} for check in leak["checks"])

    for kind, family in families.items():
        for run in family["runs"]:
            passed = (run.get("status") == "ok"
                      and run.get("solver_status") == "optimal"
                      and run.get("validation_passed") is True)
            all_checks.append({
                "family": kind,
                "name": f"solve_{run.get('size')}",
                "passed": passed,
                "detail": (f"status={run.get('status')} solver={run.get('solver_status')} "
                           f"validation={run.get('validation_passed')} "
                           f"error={run.get('error')}"),
            })
    passed = all(bool(check["passed"]) for check in all_checks)
    finished_at_utc = _utc_now()
    payload = {
        "schema_version": SCALABILITY_SCHEMA_VERSION,
        "kind": SCALABILITY_KIND,
        "generated_at_utc": finished_at_utc,
        "environment": environment,
        "provenance": _provenance(environment, started_at_utc, finished_at_utc),
        "thresholds": {
            "max_time_exponent": max_exponent,
            "max_rss_growth_mb": max_rss_growth_mb,
            "max_rss_growth_factor": max_rss_growth_factor,
            "leak_tolerance_mb": leak_tolerance_mb,
            "leak_tolerance_fraction": leak_tolerance_fraction,
        },
        "harness": {
            "repetitions_per_size": repetitions,
            "leak_repetitions": leak_repetitions,
            "timeout_seconds": timeout_seconds,
            "command": "python -m benchmarks.scalability",
        },
        "families": families,
        "leak": leak,
        "checks": all_checks,
        "summary": {"passed": passed, "checks": len(all_checks),
                    "failures": sum(1 for c in all_checks if not c["passed"])},
    }
    errors = schema.validate_scalability_artifact(payload)
    if errors:
        raise QualificationError("invalid scalability artifact:\n- " + "\n- ".join(errors))
    _write_json(out / "scalability.json", payload)
    return payload


def _cmd_run(args: argparse.Namespace) -> int:
    payload = run_scalability(
        args.out,
        lp_sizes=args.lp_sizes, mip_sizes=args.mip_sizes,
        leak_repetitions=args.leak_repetitions, leak_size=args.leak_size,
        timeout_seconds=args.timeout, repetitions=args.repetitions,
        max_exponent=args.max_exponent,
        max_rss_growth_mb=args.max_rss_growth_mb,
        max_rss_growth_factor=args.max_rss_growth_factor,
        leak_tolerance_mb=args.leak_tolerance_mb,
        leak_tolerance_fraction=args.leak_tolerance_fraction)
    print(f"wrote {Path(args.out) / 'scalability.json'}")
    for kind, family in payload["families"].items():
        for run in family["runs"]:
            print(f"  {kind:<12} size={run['size']:<4} status={run['status']:<5} "
                  f"wall={run.get('wall_seconds')} rss={run.get('peak_rss_mb')} "
                  f"objective={run.get('objective_usd')}")
        growth = family["growth"]
        print(f"  {kind:<12} time_exponent={growth['time_exponent']} "
              f"rss_growth={growth['rss_growth_mb']}")
    leak = payload["leak"]
    print(f"  leak         size={leak['size']} reps={leak['repetitions']} "
          f"growth={leak['rss_growth_mb']}MB suspected={leak['leak_suspected']}")
    print(f"summary passed={payload['summary']['passed']} "
          f"failures={payload['summary']['failures']}/{payload['summary']['checks']}")
    return 0 if payload["summary"]["passed"] else 1


def _cmd_worker(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = run_worker(args.kind, args.size, args.repetitions)
    _write_json(out / "worker_result.json", payload)
    return 0 if payload["status"] == "ok" else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="measure scalability and memory")
    run_parser.add_argument("--out", required=True, help="new output directory")
    run_parser.add_argument("--lp-sizes", type=int, nargs="+", default=list(DEFAULT_LP_SIZES))
    run_parser.add_argument("--mip-sizes", type=int, nargs="+", default=list(DEFAULT_MIP_SIZES))
    run_parser.add_argument("--leak-repetitions", type=int, default=DEFAULT_LEAK_REPETITIONS)
    run_parser.add_argument("--leak-size", type=int)
    run_parser.add_argument("--timeout", type=float, default=900.0)
    run_parser.add_argument("--repetitions", type=int, default=1,
                            help="independent child runs per size")
    run_parser.add_argument("--max-exponent", type=float, default=DEFAULT_MAX_EXPONENT)
    run_parser.add_argument("--max-rss-growth-mb", type=float, default=DEFAULT_MAX_RSS_GROWTH_MB)
    run_parser.add_argument("--max-rss-growth-factor", type=float,
                            default=DEFAULT_MAX_RSS_GROWTH_FACTOR)
    run_parser.add_argument("--leak-tolerance-mb", type=float, default=DEFAULT_LEAK_TOLERANCE_MB)
    run_parser.add_argument("--leak-tolerance-fraction", type=float,
                            default=DEFAULT_LEAK_TOLERANCE_FRACTION)
    run_parser.set_defaults(func=_cmd_run)

    worker_parser = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--kind", choices=("dispatch_lp", "schedule_mip"),
                               required=True)
    worker_parser.add_argument("--size", type=int, required=True)
    worker_parser.add_argument("--repetitions", type=int, default=1)
    worker_parser.add_argument("--out", required=True)
    worker_parser.set_defaults(func=_cmd_worker)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except QualificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
