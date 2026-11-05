"""Result provenance and execution metadata tracking for GridMind.

Provides structured execution traceability (UTC timestamps, package version,
solver name/version, input problem fingerprint hash, applied options) to ensure
optimization outputs are reproducible and auditable.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from gridmind import __version__


def sanitize_for_serialization(val: Any) -> Any:
    """Recursively transform containers and values so json.dumps succeeds.

    Non-finite floats (NaN, +Inf, -Inf) are serialized to explicit string tokens
    ('NaN', 'Infinity', '-Infinity') rather than failing strict JSON compliance
    or silently corrupting numerical data.
    """
    if isinstance(val, float):
        if math.isnan(val):
            return "NaN"
        if math.isinf(val):
            return "Infinity" if val > 0 else "-Infinity"
        return val
    elif isinstance(val, dict):
        return {str(k): sanitize_for_serialization(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple)):
        return [sanitize_for_serialization(v) for v in val]
    elif hasattr(val, "to_dict"):
        return sanitize_for_serialization(val.to_dict())
    return val


@dataclass(frozen=True)
class ExecutionMetadata:
    """Provenance and environment metadata captured at optimization execution.

    Attributes:
        timestamp_utc: ISO-8601 UTC timestamp of execution.
        gridmind_version: Installed GridMind package version string.
        solver_name: Identifier of the resolved solver backend.
        solver_version: Version string reported by the solver backend.
        problem_hash: SHA-256 hex digest of the canonical problem input representation.
        solver_options: Key-value dictionary of options applied during execution.
    """
    timestamp_utc: str
    gridmind_version: str
    solver_name: str
    solver_version: str
    problem_hash: str
    solver_options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert metadata to a serializable dictionary."""
        return {
            "timestamp_utc": self.timestamp_utc,
            "gridmind_version": self.gridmind_version,
            "solver_name": self.solver_name,
            "solver_version": self.solver_version,
            "problem_hash": self.problem_hash,
            "solver_options": sanitize_for_serialization(dict(self.solver_options)),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionMetadata:
        """Construct ExecutionMetadata from dictionary."""
        return cls(
            timestamp_utc=str(data.get("timestamp_utc", "")),
            gridmind_version=str(data.get("gridmind_version", "")),
            solver_name=str(data.get("solver_name", "")),
            solver_version=str(data.get("solver_version", "")),
            problem_hash=str(data.get("problem_hash", "")),
            solver_options=dict(data.get("solver_options", {})),
        )


def get_solver_version(solver: Any, name: str) -> str:
    """Safely extract the version string from a Pyomo solver instance."""
    try:
        if hasattr(solver, "version"):
            v = solver.version()
            if v:
                return ".".join(str(x) for x in v) if isinstance(v, (tuple, list)) else str(v)
    except Exception:
        pass
    if "highs" in (name or "").lower():
        try:
            import highspy
            return str(getattr(highspy, "__version__", "1.15.1"))
        except Exception:
            pass
    return "unknown"


def compute_problem_hash(problem: Any) -> str:
    """Compute a deterministic SHA-256 fingerprint from an input problem's dictionary."""
    try:
        if hasattr(problem, "to_dict"):
            d = problem.to_dict()
        elif isinstance(problem, dict):
            d = problem
        else:
            d = vars(problem)
        blob = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()
    except Exception:
        return "unhashable"


def capture_execution_metadata(
    solver: Any,
    solver_name: str,
    problem: Any,
    options: Any = None,
) -> ExecutionMetadata:
    """Factory creating an ExecutionMetadata snapshot for an optimization run."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    s_ver = get_solver_version(solver, solver_name)
    p_hash = compute_problem_hash(problem)
    opts_dict: dict[str, Any] = {}
    if options is not None:
        if hasattr(options, "to_dict"):
            opts_dict = options.to_dict()
        elif hasattr(options, "__dict__"):
            opts_dict = {k: v for k, v in vars(options).items() if v is not None}
        elif isinstance(options, dict):
            opts_dict = {k: v for k, v in options.items() if v is not None}
    return ExecutionMetadata(
        timestamp_utc=ts,
        gridmind_version=__version__,
        solver_name=solver_name,
        solver_version=s_ver,
        problem_hash=p_hash,
        solver_options=opts_dict,
    )
