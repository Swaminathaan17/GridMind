"""Solver discovery and configuration.

Separation of concerns: this module knows *how to find and create* a Pyomo LP
solver; ``gridmind.optimizer`` and ``gridmind.scheduling`` know how to build and
solve models.

Configuration (no machine-specific paths are hardcoded):
  GRIDMIND_SOLVER       - explicit solver plugin name (e.g. glpk, cbc, appsi_highs).
                          When set, ONLY that solver is used; if it is unavailable
                          the result is solver_unavailable (no silent fallback).
  GRIDMIND_SOLVER_PATH  - full path to the solver executable (glpsol.exe / cbc.exe)
                          or the folder containing it.

When GRIDMIND_SOLVER is not set, the candidates in SOLVER_CANDIDATES are tried in
order. GLPK is first because it is the project's reference solver; the HiGHS
``appsi`` plugin (in-process, ``pip install highspy``) is a portable fallback for
machines without a GLPK binary.
"""
from __future__ import annotations

import os
import shutil
from typing import Any

ENV_SOLVER = "GRIDMIND_SOLVER"            # explicit solver name (disables fallback)
ENV_SOLVER_PATH = "GRIDMIND_SOLVER_PATH"  # full path to executable OR its folder
DEFAULT_SOLVER = "glpk"

#: tried in order when GRIDMIND_SOLVER is unset
SOLVER_CANDIDATES: tuple[str, ...] = ("glpk", "cbc", "appsi_highs")

#: tried in order for MIP (battery exclusivity) mode. Membership here is only a
#: hint; actual MIP capability is decided by the empirical ``_probe_mip``.
MIP_SOLVER_CANDIDATES: tuple[str, ...] = ("glpk", "cbc", "appsi_highs")

#: executable names looked up when a folder is given in GRIDMIND_SOLVER_PATH
_FOLDER_EXECUTABLES = {"glpk": ("glpsol.exe", "glpsol"), "cbc": ("cbc.exe", "cbc")}

#: solvers that are external executables (may honour GRIDMIND_SOLVER_PATH)
_FILE_SOLVERS = frozenset({"glpk", "cbc"})

#: private per-process cache of (ok, reason) empirical MIP probe outcomes
_MIP_PROBE_CACHE: dict[object, tuple[bool, str]] = {}

class SolverUnavailableError(RuntimeError):
    """Pyomo missing, or the configured solver executable cannot be found."""


def _unknown_solver_types() -> tuple[type, ...]:
    """Pyomo fabricates UnknownSolver objects for unknown plugin names; they must
    never be treated as available (they shell out to whatever executable exists).
    Import is lazy so this module works without Pyomo installed."""
    try:
        from pyomo.opt.base.solvers import UnknownSolver
        return (UnknownSolver,)
    except ImportError:
        return ()


def explicit_solver_name() -> str | None:
    """The solver forced via GRIDMIND_SOLVER, or None to use the candidate chain."""
    return os.environ.get(ENV_SOLVER) or None


def solver_name() -> str:
    """Best-effort name of the solver that will be used (config or default)."""
    return explicit_solver_name() or DEFAULT_SOLVER


def _resolve_executable(path: str, name: str) -> str:
    if os.path.isdir(path):
        for candidate in _FOLDER_EXECUTABLES.get(name, ()):
            full = os.path.join(path, candidate)
            if os.path.isfile(full):
                return full
        raise SolverUnavailableError(
            f"{ENV_SOLVER_PATH}='{path}' is a folder without {'/'.join(_FOLDER_EXECUTABLES.get(name, (name,)))}")
    if os.path.isfile(path):
        return path
    found = shutil.which(path)
    if found:
        return found
    raise SolverUnavailableError(f"{ENV_SOLVER_PATH}='{path}' does not exist")


def _make(name: str) -> Any:
    try:
        import pyomo.environ as pyo
    except ImportError as exc:
        raise SolverUnavailableError("Pyomo is not installed (pip install -r requirements.txt)") from exc
    path = os.environ.get(ENV_SOLVER_PATH)
    try:
        if path and name in _FILE_SOLVERS:
            solver = pyo.SolverFactory(name, executable=_resolve_executable(path, name))
        else:
            solver = pyo.SolverFactory(name)
        if isinstance(solver, _unknown_solver_types()):
            raise SolverUnavailableError(
                f"unknown solver '{name}'; supported plugins include: {', '.join(SOLVER_CANDIDATES)}")
        if not solver.available(exception_flag=False):
            hint = (f". Install GLPK and add it to PATH or set {ENV_SOLVER_PATH}" if name == "glpk"
                    else f". Install it or set {ENV_SOLVER}")
            raise SolverUnavailableError(f"solver '{name}' not found{hint}")
    except SolverUnavailableError:
        raise
    except Exception as exc:  # factory errors, missing runtime libraries, ...
        raise SolverUnavailableError(f"could not create solver '{name}': {exc}") from exc
    return solver


def _mip_probe_cache_key(name: str) -> object:
    """Identify the solver implementation whose MIP capability was probed.

    File-based solvers can be redirected with GRIDMIND_SOLVER_PATH or by
    changing PATH while the process is running. Include the resolved
    executable and its basic file identity so a cached result is never reused
    for a different binary. Non-file plugins retain a simple name-based key.
    """
    if name not in _FILE_SOLVERS:
        return name

    configured_path = os.environ.get(ENV_SOLVER_PATH)
    try:
        if configured_path:
            executable = _resolve_executable(configured_path, name)
        else:
            executable = None
            for candidate in _FOLDER_EXECUTABLES[name]:
                executable = shutil.which(candidate)
                if executable:
                    break
    except SolverUnavailableError:
        executable = None

    if executable is None:
        unresolved_path = (
            os.path.normcase(os.path.abspath(configured_path))
            if configured_path else None
        )
        return name, "unresolved", unresolved_path, os.environ.get("PATH", "")

    resolved = os.path.normcase(os.path.realpath(executable))
    try:
        stat = os.stat(resolved)
        return (name, resolved, stat.st_dev, stat.st_ino, stat.st_mode,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return name, resolved, None, None


def _probe_mip(name: str) -> tuple[bool, str]:
    """Empirically test whether solver ``name`` can solve a tiny binary model.

    Model:  min -x - y  s.t.  2x + 2y <= 3,  x, y in {0, 1}.
    The continuous relaxation optimum is -1.5; the integer optimum is -1. A
    genuine MIP solver must return an integral point with objective -1. This
    detects plugins that are importable/available but do not actually enforce
    integrality or cannot load solutions (e.g. a missing ``highspy`` backend).

    Outcomes are cached (successes and failures) in ``_MIP_PROBE_CACHE``.
    Returns ``(ok, reason)``; never raises.
    """
    cache_key = _mip_probe_cache_key(name)
    if cache_key in _MIP_PROBE_CACHE:
        return _MIP_PROBE_CACHE[cache_key]
    try:
        import pyomo.environ as pyo
        from pyomo.opt import SolverStatus, TerminationCondition
    except ImportError as exc:
        result = (False, f"Pyomo is not installed: {exc}")
        _MIP_PROBE_CACHE[cache_key] = result
        return result
    try:
        solver = _make(name)
    except SolverUnavailableError as exc:
        result = (False, str(exc))
        _MIP_PROBE_CACHE[cache_key] = result
        return result

    try:
        m = pyo.ConcreteModel()
        m.x = pyo.Var(domain=pyo.Binary)
        m.y = pyo.Var(domain=pyo.Binary)
        m.cap = pyo.Constraint(expr=2 * m.x + 2 * m.y <= 3)
        m.obj = pyo.Objective(expr=-m.x - m.y, sense=pyo.minimize)
        results = solver.solve(m, load_solutions=False)
        status = results.solver.status
        term = results.solver.termination_condition
        if status not in (SolverStatus.ok, SolverStatus.warning):
            reason = f"solver status {status} (termination {term})"
        elif term != TerminationCondition.optimal:
            reason = f"termination condition {term} is not optimal"
        else:
            try:
                m.solutions.load_from(results)
                x = pyo.value(m.x, exception=False)
                y = pyo.value(m.y, exception=False)
            except Exception as exc:  # solution-loading failure
                x = y = None
                reason = f"could not load solution: {type(exc).__name__}: {exc}"
            else:
                reason = None
            if reason is None and (x is None or y is None):
                reason = "solver returned no values for the binary variables"
            elif reason is None:
                if abs(x - round(x)) > 1e-6 or abs(y - round(y)) > 1e-6:
                    reason = f"non-integral solution (x={x}, y={y})"
                else:
                    obj = pyo.value(m.obj, exception=False)
                    if obj is None or abs(obj - (-1.0)) > 1e-6:
                        reason = (f"unexpected objective {obj} for a binary model "
                                  f"(expected -1; got the LP value {obj!r}?)")
    except Exception as exc:  # noqa: BLE001 - probe must never propagate
        reason = f"{type(exc).__name__}: {exc}"

    result = (reason is None, reason or "ok: binary optimum -1 reached")
    _MIP_PROBE_CACHE[cache_key] = result
    return result


def make_solver(name: str | None = None, mip: bool = False) -> Any:
    """Create a configured Pyomo solver.

    ``name`` given           -> exactly that solver, or SolverUnavailableError.
    ``name`` None, env set   -> exactly the env solver (no fallback).
    ``name`` None, env unset -> first available candidate in SOLVER_CANDIDATES
                                (or MIP_SOLVER_CANDIDATES when ``mip=True``).

    ``mip=True`` additionally requires the solver to pass the empirical binary
    probe; an explicitly named/env solver that fails the probe raises rather
    than silently falling back.
    """
    explicit = name or explicit_solver_name()
    if explicit:
        if mip:
            return _verified_mip_solver(explicit)
        return _make(explicit)
    candidates = MIP_SOLVER_CANDIDATES if mip else SOLVER_CANDIDATES
    tried: list[str] = []
    for candidate in candidates:
        try:
            if mip:
                return _verified_mip_solver(candidate)
            return _make(candidate)
        except SolverUnavailableError:
            tried.append(candidate)
    label = "MIP solver" if mip else "LP solver"
    raise SolverUnavailableError(
        f"no {label} available; tried: " + ", ".join(tried)
        + f". Install GLPK (and set {ENV_SOLVER_PATH}) or `pip install highspy` for the HiGHS fallback.")


def _verified_mip_solver(name: str) -> Any:
    """Return a solver only if it is available and passes the empirical MIP probe."""
    solver = _make(name)
    ok, reason = _probe_mip(name)
    if not ok:
        raise SolverUnavailableError(
            f"solver '{name}' is not verified MIP-capable: {reason}. "
            f"Use a MIP-capable solver (e.g. GLPK or CBC) for battery exclusivity.")
    return solver


def resolve_solver(mip: bool = False) -> tuple[str, Any]:
    """Return ``(name, solver)`` actually usable on this machine.

    Explicit configuration never falls back: if GRIDMIND_SOLVER or
    GRIDMIND_SOLVER_PATH is set and cannot be honoured, raise
    SolverUnavailableError rather than silently switching solvers.

    ``mip=True`` restricts resolution to solvers that pass the empirical binary
    probe; an explicitly selected solver that fails the probe raises rather than
    falling back to a different (LP) solver."""
    explicit = explicit_solver_name()
    if explicit:
        if mip:
            return explicit, _verified_mip_solver(explicit)
        return explicit, _make(explicit)
    if os.environ.get(ENV_SOLVER_PATH):
        name = DEFAULT_SOLVER  # an explicit executable implies the default file solver
        if mip:
            return name, _verified_mip_solver(name)
        return name, _make(name)
    if mip:
        errors: list[str] = []
        for candidate in MIP_SOLVER_CANDIDATES:
            try:
                return candidate, _verified_mip_solver(candidate)
            except SolverUnavailableError as exc:
                errors.append(f"{candidate}: {exc}")
        raise SolverUnavailableError("no MIP-capable solver available; " + " | ".join(errors))
    errors = []
    for candidate in SOLVER_CANDIDATES:
        try:
            return candidate, _make(candidate)
        except SolverUnavailableError as exc:
            errors.append(f"{candidate}: {exc}")
    raise SolverUnavailableError("no LP solver available; " + " | ".join(errors))


def solver_available(mip: bool = False) -> tuple[bool, str]:
    try:
        name, _ = resolve_solver(mip=mip)
        label = "MIP-capable" if mip else "available"
        return True, f"{name} is {label}"
    except SolverUnavailableError as exc:
        return False, str(exc)


def solver_inventory() -> list[dict[str, Any]]:
    """Per-candidate availability report (for `check-solver`).

    Each entry keeps the legacy keys ``solver``, ``available``, ``detail`` and
    adds ``supports_mip``: ``None`` when the solver is unavailable (not
    evaluated), otherwise the empirical binary-probe result.
    """
    out: list[dict[str, Any]] = []
    forced = explicit_solver_name()
    if forced:
        names = (forced,)
    elif os.environ.get(ENV_SOLVER_PATH):
        names = (DEFAULT_SOLVER,)  # explicit executable implies the default file solver
    else:
        names = SOLVER_CANDIDATES
    for name in names:
        try:
            _make(name)
        except SolverUnavailableError as exc:
            out.append({"solver": name, "available": False, "detail": str(exc),
                        "supports_mip": None})
            continue
        ok, reason = _probe_mip(name)
        out.append({"solver": name, "available": True, "detail": "usable",
                    "supports_mip": ok, "mip_detail": reason})
    return out
