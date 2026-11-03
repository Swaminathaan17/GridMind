"""Reproducible qualification measurements, baselines and performance gates.

This module is intentionally separate from the ``gridmind`` runtime package. It
runs existing public entry points, writes evidence into a fresh directory, and
never changes optimisation models, solver defaults or dispatch semantics.

Run from the ``GridMind`` directory (the one containing ``pyproject.toml``)::

    python -m benchmarks.qualification measure --out benchmarks/runs/<label>
    python -m benchmarks.qualification build-baseline \
        --measurements benchmarks/runs/<label>/measurements.json
    python -m benchmarks.qualification gate \
        --measurements benchmarks/runs/<label>/measurements.json \
        --baseline benchmarks/baseline.json

The default output directory is never overwritten. ``measure`` refuses to run if
the target already exists; create a new label for each evidence run.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from benchmarks import schema, stats as robust_stats
from benchmarks.progress import ProgressReporter
from benchmarks.process import run_captured

SCHEMA_VERSION = schema.ARTIFACT_SCHEMA_VERSION
PROVENANCE_SCHEMA_VERSION = schema.PROVENANCE_SCHEMA_VERSION
HARNESS_VERSION = schema.HARNESS_VERSION
BASELINE_KIND = schema.BASELINE_KIND
MEASUREMENT_KIND = schema.MEASUREMENT_KIND

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_BASELINE = ROOT / "benchmarks" / "baseline.json"

#: input data files whose hashes are recorded with every evidence run
INPUT_FILES = (
    ROOT / "data" / "sample_network.json",
    ROOT / "data" / "sample_horizon.json",
)

WALL_HEADROOM = 1.50
SOLVER_HEADROOM = 1.50
RSS_HEADROOM = 1.25
RSS_FLOOR_MB = 16.0

VOLATILE_KEYS = frozenset({
    "generated_at_utc",
    "started_at",
    "runtime_seconds",
    "runtime_s",
    "total_time_s",
    "solve_seconds",
    "mean_solve_seconds",
    "max_solve_seconds",
    "total_solver_runtime_s",
    "mean_solver_runtime_s",
    "wall_seconds",
    "process_wall_seconds",
    "peak_rss_mb",
})

PACKAGE_NAMES = (
    "pyomo",
    "highspy",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
    "matplotlib",
)

SOLVER_ORDER = ("glpk", "cbc", "appsi_highs")


class QualificationError(RuntimeError):
    """A qualification run or baseline is invalid."""


@dataclass
class WorkloadOutput:
    parameters: Mapping[str, Any]
    solver: str | None
    solver_seconds: float | None
    decision_payload: Any
    summary: Mapping[str, Any]
    artifacts: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class WorkloadSpec:
    name: str
    description: str
    run: Callable[[Path], WorkloadOutput]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _memory_total_mb() -> float | None:
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return round(status.ullTotalPhys / (1024 * 1024), 1)
        page = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        return round(page * pages / (1024 * 1024), 1)
    except (AttributeError, OSError, ValueError):
        return None


def peak_rss_mb() -> float | None:
    """Peak resident set size of this process in MiB, where available."""
    try:
        if os.name == "nt":
            import ctypes.wintypes as wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            if not ok:
                return None
            return round(counters.PeakWorkingSetSize / (1024 * 1024), 1)
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return round(usage / divisor, 1)
    except (AttributeError, OSError, ValueError, ImportError):
        return None


def _parse_solver_version(name: str, output: str) -> str | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    if name == "cbc":
        for line in lines:
            if line.lower().startswith("version:"):
                return line
    return lines[0]


def _solver_version(name: str) -> str | None:
    """Best-effort version string for a solver plugin or executable."""
    if name == "appsi_highs":
        return _package_version("highspy")
    try:
        import shutil
        executable = shutil.which("glpsol" if name == "glpk" else name)
        if not executable:
            return None
        proc = subprocess.run([executable, "--version"], capture_output=True, text=True,
                              timeout=5, check=False)
        return _parse_solver_version(name, proc.stdout or proc.stderr)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _solver_identity(name: str, inventory: Sequence[Mapping[str, Any]] | None = None
                     ) -> dict[str, Any]:
    row = next((r for r in (inventory or []) if r.get("solver") == name), {})
    return {
        "solver": name,
        "version": row.get("version", _solver_version(name)),
        "available": bool(row.get("available", True)),
        "supports_mip": row.get("supports_mip"),
    }


def _input_hashes() -> dict[str, str]:
    return {path.relative_to(ROOT).as_posix(): schema.sha256_file(path)
            for path in INPUT_FILES if path.exists()}


def _solver_inventory() -> tuple[list[dict[str, Any]], str | None]:
    try:
        from gridmind.solver import solver_inventory
        rows = solver_inventory()
        by_name = {row.get("solver"): row for row in rows}
        ordered = [dict(by_name[name]) for name in SOLVER_ORDER if name in by_name]
        ordered.extend(dict(row) for row in rows if row.get("solver") not in SOLVER_ORDER)
        for row in ordered:
            row["version"] = _solver_version(str(row.get("solver")))
        return ordered, None
    except Exception as exc:  # pragma: no cover - defensive environment report
        return [], f"{type(exc).__name__}: {exc}"


def collect_environment() -> dict[str, Any]:
    """Machine, Python, package, solver and git provenance for one run."""
    inventory, inventory_error = _solver_inventory()
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at_utc": _utc_now(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "version_full": sys.version,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "memory_total_mb": _memory_total_mb(),
        "packages": {name: _package_version(name) for name in PACKAGE_NAMES},
        "solvers": inventory,
        "solver_inventory_error": inventory_error,
        "git": {
            "root": _git("rev-parse", "--show-toplevel"),
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        },
    }


def _provenance(environment: Mapping[str, Any], started_at_utc: str,
                finished_at_utc: str, *, legacy: bool = False) -> dict[str, Any]:
    """Provenance block shared by measurements and derived baselines."""
    inventory = environment.get("solvers") or []
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "legacy": legacy,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "git": dict(environment.get("git") or {}),
        "environment_sha256": schema.canonical_sha256(environment),
        "input_hashes": _input_hashes(),
        "solver_identity": [
            {"solver": row.get("solver"), "version": row.get("version"),
             "available": bool(row.get("available")), "supports_mip": row.get("supports_mip")}
            for row in inventory
        ],
    }


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical_value(v) for k, v in sorted(value.items())
                if str(k) not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not valid qualification evidence")
        rounded = round(value, 6)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return _canonical_value(item())
    raise TypeError(f"unsupported value in qualification evidence: {type(value).__name__}")


def canonical_payload(payload: Any) -> Any:
    """Timing-free, float-rounded, key-sorted representation for hashing."""
    return _canonical_value(payload)


def canonical_json(payload: Any) -> str:
    return json.dumps(canonical_payload(payload), sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def sha256_canonical(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_solver() -> str | None:
    try:
        from gridmind.solver import resolve_solver
        name, _ = resolve_solver()
        return name
    except Exception:
        return None


def _sum_solver_seconds(record: Mapping[str, Any]) -> float | None:
    values: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in ("runtime_seconds", "solve_seconds", "runtime_s"):
                    try:
                        number = float(item)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(number):
                        values.append(number)
                else:
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(record)
    return round(sum(values), 9) if values else None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
                    encoding="utf-8")


def _single_period(out: Path, network_path: Path, scenarios: Sequence[Any],
                   parameters: Mapping[str, Any]) -> WorkloadOutput:
    from gridmind.benchmark import aggregate_benchmark_summary, export_csv, run_benchmark
    from gridmind.network import load_network

    network = load_network(network_path)
    frame = run_benchmark(network, scenarios)
    artifact = export_csv(frame, out / "benchmark.csv")
    summary = aggregate_benchmark_summary(frame)
    return WorkloadOutput(
        parameters=parameters,
        solver=_resolved_solver(),
        solver_seconds=round(float(frame["runtime_s"].sum()), 9),
        decision_payload=frame.to_dict(orient="records"),
        summary=summary,
        artifacts=[artifact],
    )


def _run_smoke(out: Path) -> WorkloadOutput:
    from gridmind.scenarios import CORE_SCENARIOS, generate_random_scenarios

    network_path = ROOT / "data" / "sample_network.json"
    from gridmind.network import load_network
    network = load_network(network_path)
    scenarios = list(CORE_SCENARIOS[:1]) + generate_random_scenarios(network, 1, 42)
    return _single_period(
        out, network_path, scenarios,
        {"network": "data/sample_network.json", "core_scenarios": 1,
         "n_random": 1, "seed": 42, "n_scenarios": len(scenarios),
         "scenario_ids": [s.id for s in scenarios]})


def _run_benchmark(out: Path) -> WorkloadOutput:
    from gridmind.network import load_network
    from gridmind.scenarios import CORE_SCENARIOS, generate_random_scenarios

    network_path = ROOT / "data" / "sample_network.json"
    network = load_network(network_path)
    scenarios = list(CORE_SCENARIOS) + generate_random_scenarios(network, 20, 42)
    return _single_period(
        out, network_path, scenarios,
        {"network": "data/sample_network.json",
         "core_scenarios": len(CORE_SCENARIOS), "n_random": 20, "seed": 42,
         "n_scenarios": len(scenarios), "scenario_ids": [s.id for s in scenarios]})


def _run_mp_benchmark(out: Path) -> WorkloadOutput:
    from gridmind.benchmark import aggregate_benchmark_summary, export_csv, run_mp_benchmark
    from gridmind.multiperiod import load_schedule_problem
    from gridmind.scenarios import CORE_HORIZON_SCENARIOS, generate_random_horizon_scenarios

    problem = load_schedule_problem(ROOT / "data" / "sample_horizon.json")
    scenarios = list(CORE_HORIZON_SCENARIOS) + generate_random_horizon_scenarios(problem, 10, 42)
    frame = run_mp_benchmark(problem, scenarios, battery_mode="lp")
    artifact = export_csv(frame, out / "mp_benchmark.csv")
    solver = frame["solver"].dropna().iloc[0] if "solver" in frame and len(frame) else None
    return WorkloadOutput(
        parameters={"horizon": "data/sample_horizon.json",
                    "core_scenarios": len(CORE_HORIZON_SCENARIOS), "n_random": 10,
                    "seed": 42, "n_scenarios": len(frame), "battery_mode": "lp",
                    "scenario_ids": [s.id for s in scenarios]},
        solver=str(solver) if solver is not None else _resolved_solver(),
        solver_seconds=round(float(frame["runtime_s"].sum()), 9)
        if "runtime_s" in frame else None,
        decision_payload=frame.to_dict(orient="records"),
        summary=aggregate_benchmark_summary(frame),
        artifacts=[artifact],
    )


def _run_stochastic(out: Path) -> WorkloadOutput:
    from gridmind.multiperiod import load_schedule_problem
    from gridmind.risk_eval import compare_policies
    from gridmind.uncertainty import UncertaintyConfig, generate_scenarios, split_scenarios

    problem = load_schedule_problem(ROOT / "data" / "sample_horizon.json")
    config = UncertaintyConfig(n_scenarios=20, seed=42)
    scenario_set = generate_scenarios(problem, config)
    in_sample, holdout = split_scenarios(scenario_set, holdout_fraction=0.5)
    record = compare_policies(problem, in_sample, holdout, risk_weight=0.5,
                              cvar_alpha=0.95, lead_periods=1, battery_mode="lp")
    record["uncertainty"] = config.to_dict()
    record["seed"] = 42
    record["horizon"] = "data/sample_horizon.json"
    artifact = out / "stochastic.json"
    _write_json(artifact, record)
    summary = {
        "in_sample_scenarios": record["in_sample_scenarios"],
        "holdout_scenarios": record["holdout_scenarios"],
        "fit_status": {k: v.get("status") for k, v in record["fit"].items()},
        "evaluation_status": {k: v.get("status") for k, v in record["evaluations"].items()},
        "expected_cost_usd": {k: v.get("expected_cost_usd")
                              for k, v in record["evaluations"].items()},
        "cvar_usd": {k: v.get("cvar_usd") for k, v in record["evaluations"].items()},
        "validation_all_passed": {k: v.get("validation_all_passed")
                                  for k, v in record["evaluations"].items()},
    }
    return WorkloadOutput(
        parameters={"horizon": "data/sample_horizon.json", "n_scenarios": 20,
                    "seed": 42, "holdout_fraction": 0.5, "risk_weight": 0.5,
                    "cvar_alpha": 0.95, "lead_periods": 1, "battery_mode": "lp"},
        solver=record.get("solver"),
        solver_seconds=_sum_solver_seconds(record),
        decision_payload=record,
        summary=summary,
        artifacts=[artifact],
    )


def _experiment(problem: Any, config: Any, out: Path) -> WorkloadOutput:
    from gridmind.experiment import run_comparative_benchmark, write_experiment

    record = run_comparative_benchmark(problem, config)
    paths = write_experiment(record, out / "experiment")
    summary = {
        "n_seeds": len(config.seeds),
        "n_scenarios": config.n_scenarios,
        "feasible_runs": {k: v.get("feasible_runs")
                          for k, v in record.aggregate.get("policies", {}).items()},
        "mean_expected_cost_usd": {
            k: v.get("metrics", {}).get("expected_cost_usd", {}).get("mean")
            for k, v in record.aggregate.get("policies", {}).items()},
        "mean_cvar_usd": {
            k: v.get("metrics", {}).get("cvar_usd", {}).get("mean")
            for k, v in record.aggregate.get("policies", {}).items()},
    }
    return WorkloadOutput(
        parameters={"horizon": "data/sample_horizon.json", "seeds": list(config.seeds),
                    "n_scenarios": config.n_scenarios, "risk_weight": config.risk_weight,
                    "cvar_alpha": config.cvar_alpha, "lead_periods": config.lead_periods,
                    "holdout_fraction": config.holdout_fraction,
                    "battery_mode": config.battery_mode},
        solver=_resolved_solver(),
        solver_seconds=_sum_solver_seconds(record.to_dict()),
        decision_payload=record.to_dict(),
        summary=summary,
        artifacts=list(paths.values()),
    )


def _run_experiment_small(out: Path) -> WorkloadOutput:
    from gridmind.experiment import ExperimentConfig
    from gridmind.multiperiod import load_schedule_problem

    config = ExperimentConfig(seeds=(42, 43), n_scenarios=20)
    return _experiment(load_schedule_problem(ROOT / "data" / "sample_horizon.json"),
                       config, out)


def _run_experiment_default(out: Path) -> WorkloadOutput:
    from gridmind.experiment import default_experiment_config
    from gridmind.multiperiod import load_schedule_problem

    return _experiment(load_schedule_problem(ROOT / "data" / "sample_horizon.json"),
                       default_experiment_config(), out)


def _run_control(out: Path) -> WorkloadOutput:
    from gridmind.control import ControlConfig, compare_control, write_control_result
    from gridmind.multiperiod import load_schedule_problem

    problem = load_schedule_problem(ROOT / "data" / "sample_horizon.json")
    config = ControlConfig(lookahead=6, seed=20260201)
    comparison = compare_control(problem, config)
    payload = comparison.to_dict()
    paths = write_control_result(payload, out, name="control")
    controller = payload["controller"]
    summary = {
        "status": controller.get("status"),
        "periods_executed": controller.get("periods_executed"),
        "realized_operating_cost_usd": controller.get("realized_operating_cost_usd"),
        "realized_unmet_energy_mwh": controller.get("realized_unmet_energy_mwh"),
        "realized_battery_throughput_mwh": controller.get("realized_battery_throughput_mwh"),
        "cost_difference_usd": payload.get("cost_difference_usd"),
        "realized_physical_feasible": controller.get("realized_physical_feasible"),
        "mean_solve_seconds": controller.get("mean_solve_seconds"),
        "max_solve_seconds": controller.get("max_solve_seconds"),
    }
    steps = controller.get("steps") or []
    solver_seconds = round(sum(float(s.get("runtime_seconds") or 0.0) for s in steps), 9)
    return WorkloadOutput(
        parameters={"horizon": "data/sample_horizon.json", "lookahead": 6,
                    "seed": 20260201, "mode": "deterministic", "battery_mode": "lp"},
        solver=controller.get("solver"),
        solver_seconds=solver_seconds,
        decision_payload=payload,
        summary=summary,
        artifacts=list(paths.values()),
    )


def _run_stress_default(out: Path) -> WorkloadOutput:
    from gridmind.stress_bench import run_stress_benchmark, write_stress_benchmark

    record = run_stress_benchmark()
    paths = write_stress_benchmark(record, out / "stress")
    payload = record.to_dict()
    results = payload.get("results", {})
    solver_seconds = round(sum(float(v.get("solve_seconds") or 0.0)
                               for v in results.values()), 9)
    solvers = sorted({str(v.get("solver")) for v in results.values() if v.get("solver")})
    summary = {
        "included_methods": payload.get("included_methods"),
        "excluded_methods": payload.get("excluded_methods"),
        "failures": payload.get("failures"),
        "exclusions": payload.get("exclusions"),
        "realized_operating_cost_usd": {
            k: v.get("realized_operating_cost_usd") for k, v in results.items()},
        "trace_metrics_match": {k: v.get("trace_metrics_match")
                                for k, v in results.items()},
    }
    return WorkloadOutput(
        parameters={"workload": "stress_default", "methods": list(results),
                    "battery_mode": "lp", "n_scenarios_stochastic": 20,
                    "lookahead": 6, "lead_periods": 1, "seed": 20260301},
        solver=solvers[0] if len(solvers) == 1 else None,
        solver_seconds=solver_seconds,
        decision_payload=payload,
        summary=summary,
        artifacts=list(paths.values()),
    )


WORKLOAD_SPECS: dict[str, WorkloadSpec] = {
    "smoke": WorkloadSpec("smoke", "Fast single-period smoke workload for CI gates.",
                          _run_smoke),
    "benchmark": WorkloadSpec("benchmark", "Single-period batch: core scenarios + 20 random (seed 42).",
                              _run_benchmark),
    "mp_benchmark": WorkloadSpec("mp_benchmark", "24-period batch: core scenarios + 10 random (seed 42).",
                                 _run_mp_benchmark),
    "stochastic_20": WorkloadSpec("stochastic_20", "20-scenario two-stage comparison (seed 42).",
                                  _run_stochastic),
    "experiment_small": WorkloadSpec("experiment_small", "2 seeds x 20 scenarios comparative experiment.",
                                     _run_experiment_small),
    "experiment_default": WorkloadSpec("experiment_default", "Default 5 seeds x 40 scenarios experiment.",
                                       _run_experiment_default),
    "control_default": WorkloadSpec("control_default", "Default lookahead-6 rolling-horizon control.",
                                    _run_control),
    "stress_default": WorkloadSpec("stress_default", "Default six-method Stage 5C stress benchmark.",
                                   _run_stress_default),
}


def _prime_imports() -> None:
    import gridmind.analysis
    import gridmind.benchmark
    import gridmind.control
    import gridmind.experiment
    import gridmind.multiperiod
    import gridmind.network
    import gridmind.optimizer
    import gridmind.risk_eval
    import gridmind.scenarios
    import gridmind.scheduling
    import gridmind.solver
    import gridmind.stochastic
    import gridmind.stress
    import gridmind.stress_bench
    import gridmind.uncertainty


def run_workload(name: str, out_dir: str | Path) -> dict[str, Any]:
    """Run one workload in this process and write ``workload_result.json``."""
    spec = WORKLOAD_SPECS.get(name)
    if spec is None:
        raise QualificationError(f"unknown workload {name!r}; choose from {sorted(WORKLOAD_SPECS)}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    inventory, _ = _solver_inventory()
    started_at_utc = _utc_now()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": schema.WORKLOAD_KIND,
        "workload": name,
        "description": spec.description,
        "status": "error",
        "parameters": {},
        "solver": None,
        "solver_identity": None,
        "solver_seconds": None,
        "wall_seconds": None,
        "peak_rss_mb": None,
        "decision_sha256": None,
        "config_sha256": schema.canonical_sha256({}),
        "input_hashes": _input_hashes(),
        "started_at_utc": started_at_utc,
        "finished_at_utc": None,
        "artifacts": {},
        "summary": {},
        "error": None,
    }
    try:
        from gridmind.solver import solver_available
        ok, message = solver_available()
        if not ok:
            raise QualificationError(f"solver unavailable: {message}")
        _prime_imports()
        started = time.perf_counter()
        output = spec.run(out)
        wall = time.perf_counter() - started
        artifacts = {}
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            if path.name == "workload_result.json":
                continue
            artifacts[path.relative_to(out).as_posix()] = sha256_file(path)
        result.update({
            "status": "ok",
            "parameters": dict(output.parameters),
            "solver": output.solver,
            "solver_identity": _solver_identity(str(output.solver), inventory)
            if output.solver else None,
            "solver_seconds": output.solver_seconds,
            "wall_seconds": round(wall, 6),
            "peak_rss_mb": peak_rss_mb(),
            "decision_sha256": sha256_canonical(output.decision_payload),
            "config_sha256": schema.canonical_sha256(dict(output.parameters)),
            "artifacts": artifacts,
            "summary": dict(output.summary),
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["finished_at_utc"] = _utc_now()
    _write_json(out / "workload_result.json", result)
    return result


def _stats(values: Iterable[float | None]) -> dict[str, Any] | None:
    return robust_stats.summarize(values)


def aggregate_repetitions(name: str, results: Sequence[Mapping[str, Any]],
                          warmups: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    ok = [r for r in results if r.get("status") == "ok"]
    hashes = sorted({str(r.get("decision_sha256")) for r in ok if r.get("decision_sha256")})
    solvers = sorted({str(r.get("solver")) for r in ok if r.get("solver")})
    parameters = ok[0].get("parameters") if ok else {}
    first = ok[0] if ok else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "gridmind_workload_aggregate",
        "workload": name,
        "description": first.get("description") if ok else "",
        "status": "ok" if len(ok) == len(results) and results else "error",
        "parameters": parameters,
        "config_sha256": first.get("config_sha256"),
        "input_hashes": first.get("input_hashes") or _input_hashes(),
        "solver": solvers[0] if len(solvers) == 1 else (solvers or None),
        "solver_identity": first.get("solver_identity"),
        "solvers": solvers,
        "deterministic": len(hashes) == 1,
        "decision_sha256": hashes[0] if len(hashes) == 1 else None,
        "decision_hashes": hashes,
        "repetitions": len(results),
        "warmup_repetitions": len(warmups or ()),
        "warmups": list(warmups or ()),
        "wall_seconds": _stats(r.get("wall_seconds") for r in ok),
        "solver_seconds": _stats(r.get("solver_seconds") for r in ok),
        "peak_rss_mb": _stats(r.get("peak_rss_mb") for r in ok),
        "summary": first.get("summary") if ok else {},
        "artifacts": first.get("artifacts") if ok else {},
        "errors": [r.get("error") for r in results if r.get("status") != "ok"],
        "results": list(results),
    }


def measure(out_dir: str | Path, workload_names: Sequence[str] | None = None,
            repetitions: int = 3, timeout_seconds: float = 1800.0,
            warmup_repetitions: int = 0,
            progress: ProgressReporter | None = None) -> dict[str, Any]:
    """Run each workload in a fresh child process; refuse to overwrite evidence."""
    out = Path(out_dir)
    if progress is None:
        progress = ProgressReporter()
    if out.exists():
        raise QualificationError(f"refusing to overwrite existing evidence directory: {out}")
    if repetitions < 1:
        raise QualificationError("repetitions must be >= 1")
    if warmup_repetitions < 0:
        raise QualificationError("warmup_repetitions must be >= 0")
    names = list(workload_names) if workload_names else list(WORKLOAD_SPECS)
    unknown = sorted(set(names) - set(WORKLOAD_SPECS))
    if unknown:
        raise QualificationError(f"unknown workloads: {unknown}; choose from {sorted(WORKLOAD_SPECS)}")
    out.mkdir(parents=True)
    progress(f"qualification measure starting: out={out} workloads={len(names)} "
             f"repetitions={repetitions} warmups={warmup_repetitions}")
    started_at_utc = _utc_now()
    environment = collect_environment()

    def run_repetition(name: str, directory: Path, label: str) -> dict[str, Any]:
        directory.mkdir(parents=True)
        command = [sys.executable, "-m", "benchmarks.qualification", "_workload",
                   "--name", name, "--out", str(directory)]
        started = time.perf_counter()
        try:
            proc = run_captured(command, cwd=ROOT, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            progress(f"child {name} {label} ended after {elapsed:.3f}s status=timeout")
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": "gridmind_workload_result",
                "workload": name,
                "status": "error",
                "error": f"child timed out after {timeout_seconds}s: {exc}",
            }
        except OSError as exc:
            elapsed = time.perf_counter() - started
            progress(f"child {name} {label} ended after {elapsed:.3f}s status=error")
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": "gridmind_workload_result",
                "workload": name,
                "status": "error",
                "error": f"child could not be started: {type(exc).__name__}: {exc}",
            }
        elapsed = time.perf_counter() - started
        result_path = directory / "workload_result.json"
        if proc.returncode == 0 and result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["process_wall_seconds"] = None
        else:
            result = {
                "schema_version": SCHEMA_VERSION,
                "kind": "gridmind_workload_result",
                "workload": name,
                "status": "error",
                "error": (proc.stderr.strip() or proc.stdout.strip()
                          or f"child exited with {proc.returncode}"),
                "returncode": proc.returncode,
            }
        progress(f"child {name} {label} completed in {elapsed:.3f}s "
                 f"status={result.get('status')}")
        return result

    workloads: dict[str, Any] = {}
    for name_index, name in enumerate(names, start=1):
        progress(f"workload {name} ({name_index}/{len(names)}) starting")
        label = "setup"
        workload_started = time.perf_counter()
        try:
            warmups = []
            for index in range(1, warmup_repetitions + 1):
                label = f"warm-up {index}/{warmup_repetitions}"
                progress(f"workload {name}: {label} starting")
                warmups.append(run_repetition(
                    name, out / name / f"warmup-{index:02d}", label))
            reps = []
            for index in range(1, repetitions + 1):
                label = f"repetition {index}/{repetitions}"
                progress(f"workload {name}: {label} starting")
                reps.append(run_repetition(
                    name, out / name / f"rep-{index:02d}", label))
        except KeyboardInterrupt:
            progress(f"interrupted during workload {name}: {label}")
            raise
        workloads[name] = aggregate_repetitions(name, reps, warmups)
        progress(f"workload {name} ({name_index}/{len(names)}) completed in "
                 f"{time.perf_counter() - workload_started:.3f}s")
    finished_at_utc = _utc_now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": MEASUREMENT_KIND,
        "generated_at_utc": finished_at_utc,
        "environment": environment,
        "provenance": _provenance(environment, started_at_utc, finished_at_utc),
        "harness": {
            "repetitions": repetitions,
            "warmup_repetitions": warmup_repetitions,
            "workloads": names,
            "timeout_seconds": timeout_seconds,
            "command": "python -m benchmarks.qualification measure",
            "canonicalisation": ("timing keys removed; floats rounded to 6 decimals; "
                                 "keys sorted before SHA-256"),
        },
        "workloads": workloads,
    }
    _write_json(out / "measurements.json", payload)
    progress(f"artifact written: {out / 'measurements.json'}")
    return payload


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationError(f"{label} must be a JSON object")
    return value


def validate_measurement(payload: Mapping[str, Any]) -> list[str]:
    errors = schema.validate_measurement_artifact(payload)
    workloads = payload.get("workloads")
    if not isinstance(workloads, Mapping) or not workloads:
        return errors
    for name, record in workloads.items():
        if not isinstance(record, Mapping):
            continue
        if record.get("status") != "ok":
            errors.append(f"workload {name}: status is {record.get('status')!r}")
        if not record.get("deterministic"):
            errors.append(f"workload {name}: decision hash is not deterministic")
        for metric in ("wall_seconds", "peak_rss_mb"):
            if not isinstance(record.get(metric), Mapping):
                errors.append(f"workload {name}: missing {metric}")
    return errors


def validate_baseline(payload: Mapping[str, Any]) -> list[str]:
    return schema.validate_baseline_artifact(payload)


def _ceil(value: float, places: int = 3) -> float:
    factor = 10 ** places
    return math.ceil(value * factor) / factor


def build_baseline(measurement: Mapping[str, Any], out_path: str | Path) -> dict[str, Any]:
    """Derive absolute thresholds from measured baseline repetitions."""
    errors = validate_measurement(measurement)
    if errors:
        raise QualificationError("invalid measurements:\n- " + "\n- ".join(errors))
    out = Path(out_path)
    if out.exists():
        raise QualificationError(f"refusing to overwrite existing baseline: {out}")
    workloads: dict[str, Any] = {}
    for name, record in measurement["workloads"].items():
        wall = record.get("wall_seconds")
        solver = record.get("solver_seconds")
        rss = record.get("peak_rss_mb")
        if not isinstance(wall, Mapping) or not isinstance(rss, Mapping):
            raise QualificationError(f"workload {name}: missing wall/RSS statistics")
        thresholds = {
            "wall_seconds_max": _ceil(float(wall["median"]) * WALL_HEADROOM),
            "peak_rss_mb_max": _ceil(float(rss["max"]) * RSS_HEADROOM + RSS_FLOOR_MB, 1),
        }
        if isinstance(solver, Mapping):
            thresholds["solver_seconds_max"] = _ceil(
                float(solver["median"]) * SOLVER_HEADROOM)
        workloads[name] = {
            "description": record.get("description", ""),
            "parameters": record.get("parameters", {}),
            "parameters_sha256": schema.canonical_sha256(record.get("parameters", {})),
            "solver": record.get("solver"),
            "solver_identity": record.get("solver_identity"),
            "decision_sha256": record.get("decision_sha256"),
            "measured": {
                "wall_seconds": wall,
                "solver_seconds": solver,
                "peak_rss_mb": rss,
                "repetitions": record.get("repetitions"),
            },
            "thresholds": thresholds,
        }
    baseline = {
        "schema_version": SCHEMA_VERSION,
        "kind": BASELINE_KIND,
        "generated_at_utc": _utc_now(),
        "reference_environment": measurement["environment"],
        "provenance": measurement.get("provenance"),
        "harness": measurement.get("harness", {}),
        "threshold_policy": {
            "wall_headroom_factor": WALL_HEADROOM,
            "solver_headroom_factor": SOLVER_HEADROOM,
            "rss_headroom_factor": RSS_HEADROOM,
            "rss_floor_mb": RSS_FLOOR_MB,
            "formula": ("wall/solver limit = ceil(median observed x headroom, 3 dp); "
                        "RSS limit = ceil(max observed x headroom + floor, 1 dp)"),
            "rationale": ("Time limits use the median of the committed baseline "
                          "repetitions so a single scheduler or I/O outlier cannot inflate "
                          "the gate; RSS uses the maximum observed value. The headroom is "
                          "fixed in this file so every threshold can be recomputed from the "
                          "measured samples."),
            "reference_machine_only": True,
        },
        "workloads": workloads,
    }
    errors = validate_baseline(baseline)
    if errors:
        raise QualificationError("generated baseline is invalid:\n- " + "\n- ".join(errors))
    _write_json(out, baseline)
    return baseline


def _load_json(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read {path}: {exc}") from exc
    return _require_mapping(payload, str(path))


def _check(workload: str, metric: str, kind: str, actual: Any, limit: Any,
           passed: bool) -> dict[str, Any]:
    return {"workload": workload, "metric": metric, "kind": kind,
            "actual": actual, "limit": limit, "passed": bool(passed)}


def evaluate_gate(measurement: Mapping[str, Any], baseline: Mapping[str, Any],
                  workload_names: Sequence[str] | None = None,
                  require_deterministic: bool = True,
                  require_same_solver: bool = True,
                  require_performance: bool = True) -> dict[str, Any]:
    """Compare measurements to a committed baseline; return a structured decision.

    ``require_performance=False`` keeps the portable checks (schema, determinism,
    decision hash and solver identity) while omitting wall, solver-time and RSS
    thresholds. It is intended for shared CI runners whose timing is not
    comparable with the reference machine.
    """
    errors = validate_baseline(baseline)
    if errors:
        raise QualificationError("invalid baseline:\n- " + "\n- ".join(errors))
    errors = validate_measurement(measurement)
    if errors:
        raise QualificationError("invalid measurements:\n- " + "\n- ".join(errors))
    baseline_workloads = baseline["workloads"]
    names = list(workload_names) if workload_names else list(baseline_workloads)
    checks: list[dict[str, Any]] = []
    for name in names:
        expected = baseline_workloads.get(name)
        actual = measurement["workloads"].get(name)
        if expected is None:
            checks.append(_check(name, "baseline_entry", "contract", False, True, False))
            continue
        if actual is None:
            checks.append(_check(name, "present", "contract", False, True, False))
            continue
        if actual.get("status") != "ok":
            checks.append(_check(name, "status", "contract", actual.get("status"), "ok", False))
            continue
        if require_deterministic:
            checks.append(_check(name, "deterministic", "reproducibility",
                                 bool(actual.get("deterministic")), True,
                                 bool(actual.get("deterministic"))))
        expected_hash = expected.get("decision_sha256")
        checks.append(_check(name, "decision_sha256", "reproducibility",
                             actual.get("decision_sha256"), expected_hash,
                             actual.get("decision_sha256") == expected_hash))
        expected_solver = expected.get("solver")
        if require_same_solver and expected_solver:
            actual_solver = actual.get("solver")
            checks.append(_check(name, "solver", "environment", actual_solver,
                                 expected_solver, actual_solver == expected_solver))
        if not require_performance:
            continue
        thresholds = expected.get("thresholds", {})
        wall = actual.get("wall_seconds") or {}
        if "wall_seconds_max" in thresholds:
            value = wall.get("median")
            limit = thresholds["wall_seconds_max"]
            checks.append(_check(name, "wall_seconds", "performance", value, limit,
                                 value is not None and float(value) <= float(limit)))
        solver_stats = actual.get("solver_seconds")
        if "solver_seconds_max" in thresholds and isinstance(solver_stats, Mapping):
            value = solver_stats.get("median")
            limit = thresholds["solver_seconds_max"]
            checks.append(_check(name, "solver_seconds", "performance", value, limit,
                                 value is not None and float(value) <= float(limit)))
        rss = actual.get("peak_rss_mb") or {}
        if "peak_rss_mb_max" in thresholds:
            value = rss.get("max")
            limit = thresholds["peak_rss_mb_max"]
            checks.append(_check(name, "peak_rss_mb", "performance", value, limit,
                                 value is not None and float(value) <= float(limit)))
    failures = [check for check in checks if not check["passed"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": not failures,
        "workloads": names,
        "checks": checks,
        "failures": failures,
    }


def _parse_names(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _cmd_measure(args: argparse.Namespace) -> int:
    progress = ProgressReporter(enabled=not args.quiet)
    payload = measure(args.out, _parse_names(args.workloads), args.repetitions,
                      args.timeout, args.warmup, progress=progress)
    print(f"wrote {Path(args.out) / 'measurements.json'}")
    for name, record in payload["workloads"].items():
        wall = record.get("wall_seconds") or {}
        print(f"  {name:<20} status={record['status']:<5} "
              f"median_wall={wall.get('median')}s deterministic={record['deterministic']}")
    ok = all(r["status"] == "ok" and r["deterministic"]
             and isinstance(r.get("peak_rss_mb"), Mapping)
             for r in payload["workloads"].values())
    return 0 if ok else 1


def _cmd_workload(args: argparse.Namespace) -> int:
    result = run_workload(args.name, args.out)
    if result.get("status") != "ok":
        print(result.get("error") or "workload failed", file=sys.stderr)
        return 3
    print(f"{args.name}: wall={result['wall_seconds']}s "
          f"hash={result['decision_sha256']}")
    return 0


def _cmd_build_baseline(args: argparse.Namespace) -> int:
    payload = build_baseline(_load_json(args.measurements), args.out)
    print(f"wrote {args.out} ({len(payload['workloads'])} workloads)")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    errors = validate_baseline(_load_json(args.baseline))
    if errors:
        print("INVALID")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("VALID")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    result = evaluate_gate(_load_json(args.measurements), _load_json(args.baseline),
                           _parse_names(args.workloads),
                           require_deterministic=not args.allow_nondeterministic,
                           require_same_solver=not args.allow_solver_change,
                           require_performance=not args.reproducibility_only)
    print(f"{'PASS' if result['passed'] else 'FAIL'} "
          f"({len(result['checks'])} checks, {len(result['failures'])} failures)")
    for check in result["checks"]:
        status = "ok" if check["passed"] else "FAIL"
        print(f"  [{status:<4}] {check['workload']:<20} {check['metric']:<18} "
              f"actual={check['actual']!s:<22} limit={check['limit']!s}")
    return 0 if result["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    measure_parser = sub.add_parser("measure", help="run workloads into a new evidence directory")
    measure_parser.add_argument("--out", required=True, help="new output directory")
    measure_parser.add_argument("--workloads", help="comma-separated workload names")
    measure_parser.add_argument("--repetitions", type=int, default=3)
    measure_parser.add_argument("--warmup", type=int, default=0,
                                help="recorded warm-up children excluded from statistics")
    measure_parser.add_argument("--timeout", type=float, default=1800.0,
                                help="per-child timeout in seconds")
    measure_parser.add_argument("--quiet", action="store_true",
                                help="suppress timestamped progress on stderr")
    measure_parser.set_defaults(func=_cmd_measure)

    baseline_parser = sub.add_parser("build-baseline", help="derive thresholds from measurements")
    baseline_parser.add_argument("--measurements", required=True)
    baseline_parser.add_argument("--out", default=str(DEFAULT_BASELINE))
    baseline_parser.set_defaults(func=_cmd_build_baseline)

    validate_parser = sub.add_parser("validate-baseline", help="validate baseline structure")
    validate_parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    validate_parser.set_defaults(func=_cmd_validate)

    gate_parser = sub.add_parser("gate", help="compare measurements to a baseline")
    gate_parser.add_argument("--measurements", required=True)
    gate_parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    gate_parser.add_argument("--workloads", help="comma-separated subset to gate")
    gate_parser.add_argument("--allow-nondeterministic", action="store_true")
    gate_parser.add_argument("--allow-solver-change", action="store_true")
    gate_parser.add_argument("--reproducibility-only", action="store_true",
                             help="check schema, determinism, decision hash and solver; "
                                  "skip machine-specific performance thresholds")
    gate_parser.set_defaults(func=_cmd_gate)

    workload_parser = sub.add_parser("_workload", help=argparse.SUPPRESS)
    workload_parser.add_argument("--name", required=True)
    workload_parser.add_argument("--out", required=True)
    workload_parser.set_defaults(func=_cmd_workload)
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
