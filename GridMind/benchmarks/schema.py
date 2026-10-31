"""Artifact schema and provenance validation for Stage 5E evidence.

This module is deliberately dependency-free and separate from the runtime
package. It defines what a *complete* qualification artifact looks like so that
malformed or partial evidence is rejected instead of being accepted silently.

The rules are intentionally strict for newly generated evidence:

* a run has a schema version, harness version and UTC timestamps;
* it records the git commit/branch/dirty flag, an environment hash, the hashes
  of the input data files it consumed and the identity/version of every solver
  candidate;
* every workload repetition records its own timestamps, configuration hash,
  output/artifact hashes, solver identity and timing/memory fields;
* hashes are lowercase SHA-256 hex strings and timestamps are ISO-8601 UTC.

Legacy artifacts created before the provenance hardening can carry
``"legacy": true`` in their provenance block, but they must still contain the
minimum traceability fields (timestamps, git, environment hash, input hashes and
solver identity). The flag only documents that the values were reconstructed
from an older evidence file; it does not relax structural checks.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ARTIFACT_SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
HARNESS_VERSION = "5E.4"

MEASUREMENT_KIND = "gridmind_qualification_measurements"
BASELINE_KIND = "gridmind_qualification_baseline"
WORKLOAD_KIND = "gridmind_workload_result"
COVERAGE_KIND = "gridmind_solver_coverage"
SCALABILITY_KIND = "gridmind_scalability_measurements"
STATISTICAL_KIND = "gridmind_statistical_matrix"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALID_STATUSES = frozenset({"ok", "error", "unsupported"})


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.match(value))


def is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not valid qualification evidence")
        rounded = round(value, 6)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return _canonical(item())
    raise TypeError(f"unsupported value in qualification evidence: {type(value).__name__}")


def canonical_sha256(payload: Any) -> str:
    """Hash a JSON-compatible payload deterministically (no timing filtering)."""
    encoded = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"),
                         allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_mapping(value: Any, label: str, errors: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{label} must be an object")
        return None
    return value


def validate_provenance(provenance: Any, label: str = "provenance") -> list[str]:
    errors: list[str] = []
    record = _require_mapping(provenance, label, errors)
    if record is None:
        return errors
    if record.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        errors.append(f"{label}.schema_version must be {PROVENANCE_SCHEMA_VERSION}")
    if not isinstance(record.get("harness_version"), str) or not record.get("harness_version"):
        errors.append(f"{label}.harness_version must be a non-empty string")
    for key in ("started_at_utc", "finished_at_utc"):
        if not is_timestamp(record.get(key)):
            errors.append(f"{label}.{key} must be an ISO-8601 UTC timestamp ending in Z")
    if not isinstance(record.get("legacy", False), bool):
        errors.append(f"{label}.legacy must be a boolean when present")

    git = _require_mapping(record.get("git"), f"{label}.git", errors)
    if git is not None:
        if "commit" not in git or "branch" not in git or "dirty" not in git:
            errors.append(f"{label}.git must contain commit, branch and dirty")
        elif git.get("commit") is not None and not isinstance(git.get("commit"), str):
            errors.append(f"{label}.git.commit must be a string or null")
        if not isinstance(git.get("dirty"), bool):
            errors.append(f"{label}.git.dirty must be a boolean")

    if not is_sha256(record.get("environment_sha256")):
        errors.append(f"{label}.environment_sha256 must be 64 lowercase hex characters")

    inputs = _require_mapping(record.get("input_hashes"), f"{label}.input_hashes", errors)
    if inputs is not None:
        if not inputs:
            errors.append(f"{label}.input_hashes must not be empty")
        for name, digest in inputs.items():
            if not is_sha256(digest):
                errors.append(f"{label}.input_hashes[{name!r}] must be 64 lowercase hex characters")

    solvers = record.get("solver_identity")
    if not isinstance(solvers, list) or not solvers:
        errors.append(f"{label}.solver_identity must be a non-empty list")
    else:
        for index, solver in enumerate(solvers):
            item = _require_mapping(solver, f"{label}.solver_identity[{index}]", errors)
            if item is None:
                continue
            if not isinstance(item.get("solver"), str) or not item.get("solver"):
                errors.append(f"{label}.solver_identity[{index}].solver must be a non-empty string")
            if not isinstance(item.get("available"), bool):
                errors.append(f"{label}.solver_identity[{index}].available must be a boolean")
            version = item.get("version")
            if version is not None and not isinstance(version, str):
                errors.append(f"{label}.solver_identity[{index}].version must be a string or null")
            supports_mip = item.get("supports_mip")
            if supports_mip is not None and not isinstance(supports_mip, bool):
                errors.append(f"{label}.solver_identity[{index}].supports_mip must be a boolean or null")
    return errors


def validate_workload_result(record: Any, label: str = "workload") -> list[str]:
    errors: list[str] = []
    payload = _require_mapping(record, label, errors)
    if payload is None:
        return errors
    if payload.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append(f"{label}.schema_version must be {ARTIFACT_SCHEMA_VERSION}")
    if payload.get("kind") != WORKLOAD_KIND:
        errors.append(f"{label}.kind must be {WORKLOAD_KIND!r}")
    if not isinstance(payload.get("workload"), str) or not payload.get("workload"):
        errors.append(f"{label}.workload must be a non-empty string")
    status = payload.get("status")
    if status not in _VALID_STATUSES:
        errors.append(f"{label}.status must be one of {sorted(_VALID_STATUSES)}")
    if not isinstance(payload.get("description"), str):
        errors.append(f"{label}.description must be a string")
    if not isinstance(payload.get("parameters"), Mapping):
        errors.append(f"{label}.parameters must be an object")
    for key in ("started_at_utc", "finished_at_utc"):
        if not is_timestamp(payload.get(key)):
            errors.append(f"{label}.{key} must be an ISO-8601 UTC timestamp ending in Z")
    inputs = _require_mapping(payload.get("input_hashes"), f"{label}.input_hashes", errors)
    if inputs is not None:
        if not inputs:
            errors.append(f"{label}.input_hashes must not be empty")
        for name, digest in inputs.items():
            if not is_sha256(digest):
                errors.append(f"{label}.input_hashes[{name!r}] must be 64 lowercase hex characters")
    if not is_sha256(payload.get("config_sha256")):
        errors.append(f"{label}.config_sha256 must be 64 lowercase hex characters")
    if not isinstance(payload.get("summary"), Mapping):
        errors.append(f"{label}.summary must be an object")
    artifacts = _require_mapping(payload.get("artifacts"), f"{label}.artifacts", errors)
    if artifacts is not None:
        for name, digest in artifacts.items():
            if not is_sha256(digest):
                errors.append(f"{label}.artifacts[{name!r}] must be 64 lowercase hex characters")

    if status == "ok":
        if not isinstance(payload.get("solver"), str) or not payload.get("solver"):
            errors.append(f"{label}.solver must be a non-empty string for an ok result")
        if not is_sha256(payload.get("decision_sha256")):
            errors.append(f"{label}.decision_sha256 must be 64 lowercase hex characters")
        for key in ("wall_seconds", "peak_rss_mb"):
            value = payload.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(float(value)) or float(value) < 0:
                errors.append(f"{label}.{key} must be a finite non-negative number")
        solver_seconds = payload.get("solver_seconds")
        if solver_seconds is not None:
            if not isinstance(solver_seconds, (int, float)) or isinstance(solver_seconds, bool) \
                    or not math.isfinite(float(solver_seconds)) or float(solver_seconds) < 0:
                errors.append(f"{label}.solver_seconds must be a finite non-negative number or null")
        solver_identity = _require_mapping(payload.get("solver_identity"),
                                           f"{label}.solver_identity", errors)
        if solver_identity is not None:
            if solver_identity.get("solver") != payload.get("solver"):
                errors.append(f"{label}.solver_identity.solver must match solver")
    else:
        if not isinstance(payload.get("error"), str) or not payload.get("error"):
            errors.append(f"{label}.error must be a non-empty string for a non-ok result")
        if payload.get("decision_sha256") is not None \
                and not is_sha256(payload.get("decision_sha256")):
            errors.append(f"{label}.decision_sha256 must be null or 64 lowercase hex characters")
    return errors


def validate_measurement_artifact(payload: Any) -> list[str]:
    errors: list[str] = []
    record = _require_mapping(payload, "measurement", errors)
    if record is None:
        return errors
    if record.get("kind") != MEASUREMENT_KIND:
        errors.append(f"measurement.kind must be {MEASUREMENT_KIND!r}")
    if record.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append(f"measurement.schema_version must be {ARTIFACT_SCHEMA_VERSION}")
    if not is_timestamp(record.get("generated_at_utc")):
        errors.append("measurement.generated_at_utc must be an ISO-8601 UTC timestamp ending in Z")
    if not isinstance(record.get("environment"), Mapping):
        errors.append("measurement.environment must be an object")
    if not isinstance(record.get("harness"), Mapping):
        errors.append("measurement.harness must be an object")
    errors.extend(validate_provenance(record.get("provenance"), "measurement.provenance"))

    workloads = record.get("workloads")
    if not isinstance(workloads, Mapping) or not workloads:
        errors.append("measurement.workloads must be a non-empty object")
        return errors
    for name, workload in workloads.items():
        label = f"measurement.workloads[{name!r}]"
        aggregate = _require_mapping(workload, label, errors)
        if aggregate is None:
            continue
        if not isinstance(aggregate.get("repetitions"), int) or aggregate.get("repetitions") < 1:
            errors.append(f"{label}.repetitions must be a positive integer")
        if not isinstance(aggregate.get("deterministic"), bool):
            errors.append(f"{label}.deterministic must be a boolean")
        results = aggregate.get("results")
        if not isinstance(results, list) or not results:
            errors.append(f"{label}.results must be a non-empty list")
        else:
            for index, repetition in enumerate(results):
                errors.extend(validate_workload_result(
                    repetition, f"{label}.results[{index}]"))
    return errors


def validate_baseline_artifact(payload: Any) -> list[str]:
    errors: list[str] = []
    record = _require_mapping(payload, "baseline", errors)
    if record is None:
        return errors
    if record.get("kind") != BASELINE_KIND:
        errors.append(f"baseline.kind must be {BASELINE_KIND!r}")
    if record.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append(f"baseline.schema_version must be {ARTIFACT_SCHEMA_VERSION}")
    if not isinstance(record.get("reference_environment"), Mapping):
        errors.append("baseline.reference_environment must be an object")
    if not isinstance(record.get("threshold_policy"), Mapping):
        errors.append("baseline.threshold_policy must be an object")
    if "provenance" in record:
        errors.extend(validate_provenance(record.get("provenance"), "baseline.provenance"))

    workloads = record.get("workloads")
    if not isinstance(workloads, Mapping) or not workloads:
        errors.append("baseline.workloads must be a non-empty object")
        return errors
    for name, workload in workloads.items():
        label = f"baseline.workloads[{name!r}]"
        item = _require_mapping(workload, label, errors)
        if item is None:
            continue
        thresholds = _require_mapping(item.get("thresholds"), f"{label}.thresholds", errors)
        if thresholds is not None:
            for key, value in thresholds.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(float(value)) or value <= 0:
                    errors.append(f"{label}.threshold {key} must be finite and > 0")
        if not is_sha256(item.get("decision_sha256")):
            errors.append(f"{label}.decision_sha256 must be 64 lowercase hex characters")
        measured = _require_mapping(item.get("measured"), f"{label}.measured", errors)
        if measured is not None:
            for key in ("wall_seconds", "peak_rss_mb"):
                if not isinstance(measured.get(key), Mapping):
                    errors.append(f"{label}.measured.{key} must be an object")
            if not isinstance(measured.get("repetitions"), int) or measured.get("repetitions") < 1:
                errors.append(f"{label}.measured.repetitions must be a positive integer")
        if "parameters_sha256" in item and not is_sha256(item.get("parameters_sha256")):
            errors.append(f"{label}.parameters_sha256 must be 64 lowercase hex characters")
    return errors


def validate_hash_map(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    errors: list[str] = []
    mapping = _require_mapping(value, label, errors)
    if mapping is None:
        return errors
    if not allow_empty and not mapping:
        errors.append(f"{label} must not be empty")
    for name, digest in mapping.items():
        if not is_sha256(digest):
            errors.append(f"{label}[{name!r}] must be 64 lowercase hex characters")
    return errors


def validate_solver_identity_list(value: Any, label: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return errors
    for index, item in enumerate(value):
        entry = _require_mapping(item, f"{label}[{index}]", errors)
        if entry is None:
            continue
        if not isinstance(entry.get("solver"), str) or not entry.get("solver"):
            errors.append(f"{label}[{index}].solver must be a non-empty string")
        if not isinstance(entry.get("available"), bool):
            errors.append(f"{label}[{index}].available must be a boolean")
    return errors
