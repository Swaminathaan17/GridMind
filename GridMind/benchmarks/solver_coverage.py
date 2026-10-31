"""Multi-solver coverage evidence for Stage 5E.

The command exercises every solver that is actually available in the current
environment. Unavailable solvers, solvers that cannot solve the MIP formulation,
and genuine solver failures are reported as different states; they are never
silently treated as success.

Two formulations are used:

* a tiny single-period LP, compared across all available LP solvers;
* a tiny multi-period battery-exclusivity MIP, compared only across solvers that
  pass the empirical MIP probe.

LP and MIP objectives are never compared with each other: they solve different
mathematical problems. Each solver is run in a fresh child process with
``GRIDMIND_SOLVER`` forced, so one solver cannot contaminate another's cached
state.

Run from the ``GridMind`` directory::

    python -m benchmarks.solver_coverage run --out benchmarks/runs/solvers_<label>

Exit code 0 means every *available* solver passed its applicable probes.
Unavailable solvers and unsupported MIP capabilities are reported but do not make
an ordinary run fail.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks import schema
from benchmarks.qualification import (
    ROOT,
    QualificationError,
    _input_hashes,
    _load_json,
    _provenance,
    _solver_inventory,
    _utc_now,
    _write_json,
    collect_environment,
    peak_rss_mb,
)

COVERAGE_SCHEMA_VERSION = schema.ARTIFACT_SCHEMA_VERSION
COVERAGE_KIND = schema.COVERAGE_KIND
SOLVER_ORDER = ("glpk", "cbc", "appsi_highs")
OBJECTIVE_REL_TOL = 1e-6


def _tiny_network() -> Any:
    from gridmind.network import Generator, Line, Network, Node

    return Network(
        name="coverage-lp",
        nodes=(Node("A", "A"), Node("B", "B", 50.0)),
        generators=(Generator("cheap", "Cheap", "A", "gas", 100.0, 10.0),),
        lines=(Line("AB", "A-B", "A", "B", 30.0),),
        unmet_penalty_per_mwh=1000.0,
    )


def _tiny_mip_problem() -> Any:
    from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
    from gridmind.network import Generator, Network, Node

    network = Network(
        name="coverage-mip",
        nodes=(Node("bus", "bus", 10.0),),
        generators=(Generator("cheap", "Cheap", "bus", "gas", 20.0, 10.0),),
        lines=(),
        unmet_penalty_per_mwh=1000.0,
    )
    periods = 4
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


def probe_lp() -> dict[str, Any]:
    """Solve the tiny LP and return solver-independent evidence."""
    from gridmind.optimizer import solve_dispatch

    started = time.perf_counter()
    result = solve_dispatch(_tiny_network())
    wall = time.perf_counter() - started
    validation = result.validation
    return {
        "formulation": "lp",
        "status": result.status,
        "solver": result.solver,
        "termination": result.termination_condition,
        "objective_usd": result.objective,
        "validation_passed": None if validation is None else bool(validation.passed),
        "max_constraint_violation": None if validation is None else max(
            (float(c.max_violation) for c in validation.checks), default=0.0),
        "wall_seconds": round(wall, 6),
        "error": None if result.status == "optimal" else result.message,
    }


def probe_mip() -> dict[str, Any]:
    """Solve the tiny battery-exclusivity MIP and return evidence."""
    from gridmind.scheduling import solve_schedule

    started = time.perf_counter()
    result = solve_schedule(_tiny_mip_problem(), battery_mode="exclusive")
    wall = time.perf_counter() - started
    validation = result.validation
    return {
        "formulation": "mip",
        "status": result.status,
        "solver": result.solver,
        "termination": result.termination_condition,
        "objective_usd": result.objective,
        "validation_passed": None if validation is None else bool(validation.passed),
        "max_constraint_violation": None if validation is None else max(
            (float(c.max_violation) for c in validation.checks), default=0.0),
        "wall_seconds": round(wall, 6),
        "error": None if result.status == "optimal" else result.message,
    }


def _probe_result(probe: Mapping[str, Any], solver: str) -> dict[str, Any]:
    passed = (probe.get("status") == "optimal"
              and probe.get("validation_passed") is True
              and probe.get("objective_usd") is not None
              and probe.get("solver") == solver)
    return {
        **dict(probe),
        "solver_status": probe.get("status"),
        "status": "passed" if passed else "failed",
        "reason": None if passed else (
            probe.get("error") or
            (f"reported solver {probe.get('solver')!r} instead of {solver!r}"
             if probe.get("solver") != solver else
             "independent validation failed" if probe.get("validation_passed") is not True
             else f"unexpected solver status {probe.get('status')!r}")),
    }


def _run_child_probe(solver: str, kind: str, out_dir: Path,
                     timeout_seconds: float) -> dict[str, Any]:
    probe_dir = out_dir / f"{solver}-{kind}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GRIDMIND_SOLVER"] = solver
    env.pop("GRIDMIND_SOLVER_PATH", None)
    command = [sys.executable, "-m", "benchmarks.solver_coverage", "_probe",
               "--solver", solver, "--kind", kind, "--out", str(probe_dir)]
    try:
        proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"status": "failed", "formulation": kind, "error":
                f"probe timed out after {timeout_seconds}s: {exc}", "solver": solver}
    result_path = probe_dir / "probe_result.json"
    if proc.returncode == 0 and result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    return {
        "status": "failed",
        "formulation": kind,
        "solver": solver,
        "error": (proc.stderr.strip() or proc.stdout.strip()
                  or f"child exited with {proc.returncode}"),
    }


def _objective_equivalent(actual: Any, reference: Any) -> bool:
    if actual is None or reference is None:
        return False
    try:
        a, r = float(actual), float(reference)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(r)):
        return False
    return abs(a - r) <= max(1e-9, abs(r) * OBJECTIVE_REL_TOL)


def _comparisons(records: Mapping[str, Mapping[str, Any]], formulation: str
                 ) -> list[dict[str, Any]]:
    passed = {name: rec[formulation] for name, rec in records.items()
              if rec.get(formulation, {}).get("status") == "passed"}
    if not passed:
        return []
    reference_name = next(iter(passed))
    reference_objective = passed[reference_name].get("objective_usd")
    comparisons = []
    for name, result in passed.items():
        objective = result.get("objective_usd")
        comparisons.append({
            "formulation": formulation,
            "solver": name,
            "reference_solver": reference_name,
            "objective_usd": objective,
            "reference_objective_usd": reference_objective,
            "delta_usd": (None if objective is None or reference_objective is None
                          else float(objective) - float(reference_objective)),
            "equivalent": _objective_equivalent(objective, reference_objective),
        })
    return comparisons


def _classify_coverage(records: Mapping[str, Mapping[str, Any]],
                       include_mip: bool) -> dict[str, Any]:
    available = [name for name, rec in records.items() if rec.get("available")]
    unavailable = [name for name, rec in records.items() if not rec.get("available")]
    lp_passed = [name for name in available if records[name]["lp"]["status"] == "passed"]
    lp_failed = [name for name in available if records[name]["lp"]["status"] == "failed"]
    mip_passed = [name for name in available if records[name]["mip"]["status"] == "passed"]
    mip_failed = [name for name in available if records[name]["mip"]["status"] == "failed"]
    mip_unsupported = [name for name in available
                       if records[name]["mip"]["status"] == "unsupported"]
    expected_mip = [name for name in available
                    if include_mip and records[name].get("supports_mip") is True]
    complete = bool(available) and not lp_failed and all(
        records[name]["mip"]["status"] == "passed" for name in expected_mip)
    return {
        "available_solvers": available,
        "unavailable_solvers": unavailable,
        "lp_passed": lp_passed,
        "lp_failed": lp_failed,
        "mip_passed": mip_passed,
        "mip_failed": mip_failed,
        "mip_unsupported": mip_unsupported,
        "mip_expected": expected_mip,
        "coverage_complete": complete,
    }


def run_solver_coverage(out_dir: str | Path, solvers: Sequence[str] | None = None,
                        include_mip: bool = True,
                        timeout_seconds: float = 300.0) -> dict[str, Any]:
    """Run coverage probes into a fresh, non-overwritten evidence directory."""
    out = Path(out_dir)
    if out.exists():
        raise QualificationError(f"refusing to overwrite existing evidence directory: {out}")
    inventory, inventory_error = _solver_inventory()
    by_name = {str(row.get("solver")): row for row in inventory}
    if solvers:
        unknown = sorted(set(solvers) - set(by_name) - set(SOLVER_ORDER))
        if unknown:
            raise QualificationError(f"unknown solvers: {unknown}; choose from {sorted(by_name)}")
        names = [name for name in SOLVER_ORDER if name in set(solvers)]
        names.extend(name for name in solvers if name not in names)
    else:
        names = [name for name in SOLVER_ORDER if name in by_name]
        names.extend(name for name in by_name if name not in names)
    if not names:
        raise QualificationError("no solver candidates were discovered")

    out.mkdir(parents=True)
    started_at_utc = _utc_now()
    environment = collect_environment()
    records: dict[str, Any] = {}
    for name in names:
        row = by_name.get(name, {})
        available = bool(row.get("available"))
        record: dict[str, Any] = {
            "solver": name,
            "available": available,
            "supports_mip": row.get("supports_mip"),
            "version": row.get("version"),
            "detail": row.get("detail") if not available else "usable",
            "lp": {"status": "unavailable", "reason": row.get("detail", "not discovered")},
            "mip": {"status": "unavailable", "reason": row.get("detail", "not discovered")},
        }
        if available:
            lp = _probe_result(_run_child_probe(name, "lp", out, timeout_seconds), name)
            record["lp"] = lp
            if not include_mip:
                record["mip"] = {"status": "not_run", "reason": "MIP probes disabled"}
            elif row.get("supports_mip") is not True:
                record["mip"] = {
                    "status": "unsupported",
                    "reason": row.get("mip_detail") or "solver did not pass the MIP probe",
                }
            else:
                record["mip"] = _probe_result(
                    _run_child_probe(name, "mip", out, timeout_seconds), name)
        records[name] = record

    summary = _classify_coverage(records, include_mip)
    comparisons = _comparisons(records, "lp") + (
        _comparisons(records, "mip") if include_mip else [])
    finished_at_utc = _utc_now()
    payload = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "kind": COVERAGE_KIND,
        "generated_at_utc": finished_at_utc,
        "environment": environment,
        "provenance": _provenance(environment, started_at_utc, finished_at_utc),
        "inventory_error": inventory_error,
        "include_mip": include_mip,
        "solvers": records,
        "comparisons": comparisons,
        "summary": summary,
    }
    errors = schema.validate_coverage_artifact(payload)
    if errors:
        raise QualificationError("invalid coverage artifact:\n- " + "\n- ".join(errors))
    _write_json(out / "coverage.json", payload)
    return payload


def _cmd_run(args: argparse.Namespace) -> int:
    names = [part.strip() for part in args.solvers.split(",") if part.strip()] \
        if args.solvers else None
    payload = run_solver_coverage(args.out, names, include_mip=not args.skip_mip,
                                  timeout_seconds=args.timeout)
    summary = payload["summary"]
    print(f"wrote {Path(args.out) / 'coverage.json'}")
    for name, record in payload["solvers"].items():
        print(f"  {name:<14} available={str(record['available']):<5} "
              f"lp={record['lp']['status']:<11} mip={record['mip']['status']}")
    print(f"coverage_complete={summary['coverage_complete']} "
          f"available={summary['available_solvers']} "
          f"unavailable={summary['unavailable_solvers']}")
    return 0 if summary["coverage_complete"] else 1


def _cmd_probe(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started_at_utc = _utc_now()
    try:
        probe = probe_lp() if args.kind == "lp" else probe_mip()
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - recorded evidence
        probe = {"formulation": args.kind, "status": "error", "solver": args.solver,
                 "error": f"{type(exc).__name__}: {exc}"}
        status = "error"
    payload = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "kind": "gridmind_solver_probe",
        "requested_solver": args.solver,
        "started_at_utc": started_at_utc,
        "finished_at_utc": _utc_now(),
        "status": status,
        "input_hashes": _input_hashes(),
        "peak_rss_mb": peak_rss_mb(),
        **probe,
    }
    _write_json(out / "probe_result.json", payload)
    return 0 if status == "ok" else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="probe every available solver")
    run_parser.add_argument("--out", required=True, help="new output directory")
    run_parser.add_argument("--solvers", help="comma-separated solver names")
    run_parser.add_argument("--skip-mip", action="store_true",
                            help="skip MIP probes and report them as not_run")
    run_parser.add_argument("--timeout", type=float, default=300.0)
    run_parser.set_defaults(func=_cmd_run)

    probe_parser = sub.add_parser("_probe", help=argparse.SUPPRESS)
    probe_parser.add_argument("--solver", required=True)
    probe_parser.add_argument("--kind", choices=("lp", "mip"), required=True)
    probe_parser.add_argument("--out", required=True)
    probe_parser.set_defaults(func=_cmd_probe)
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
