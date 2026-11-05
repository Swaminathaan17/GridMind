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


_PROBE_STATUSES = frozenset({"passed", "failed", "unavailable", "unsupported", "not_run"})


def validate_coverage_artifact(payload: Any) -> list[str]:
    errors: list[str] = []
    record = _require_mapping(payload, "coverage", errors)
    if record is None:
        return errors
    if record.get("kind") != COVERAGE_KIND:
        errors.append(f"coverage.kind must be {COVERAGE_KIND!r}")
    if record.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append(f"coverage.schema_version must be {ARTIFACT_SCHEMA_VERSION}")
    if not is_timestamp(record.get("generated_at_utc")):
        errors.append("coverage.generated_at_utc must be an ISO-8601 UTC timestamp ending in Z")
    if not isinstance(record.get("environment"), Mapping):
        errors.append("coverage.environment must be an object")
    errors.extend(validate_provenance(record.get("provenance"), "coverage.provenance"))

    solvers = _require_mapping(record.get("solvers"), "coverage.solvers", errors)
    if solvers is not None:
        if not solvers:
            errors.append("coverage.solvers must not be empty")
        for name, solver in solvers.items():
            label = f"coverage.solvers[{name!r}]"
            item = _require_mapping(solver, label, errors)
            if item is None:
                continue
            if item.get("solver") != name:
                errors.append(f"{label}.solver must match its key")
            if not isinstance(item.get("available"), bool):
                errors.append(f"{label}.available must be a boolean")
            for formulation in ("lp", "mip"):
                probe = _require_mapping(item.get(formulation), f"{label}.{formulation}", errors)
                if probe is None:
                    continue
                status = probe.get("status")
                if status not in _PROBE_STATUSES:
                    errors.append(f"{label}.{formulation}.status must be one of "
                                  f"{sorted(_PROBE_STATUSES)}")
                if status == "passed":
                    objective = probe.get("objective_usd")
                    if not isinstance(objective, (int, float)) or isinstance(objective, bool) \
                            or not math.isfinite(float(objective)):
                        errors.append(f"{label}.{formulation}.objective_usd must be finite")
                    if probe.get("validation_passed") is not True:
                        errors.append(f"{label}.{formulation}.validation_passed must be true")
                    if probe.get("solver") != name:
                        errors.append(f"{label}.{formulation}.solver must match its key")

    summary = _require_mapping(record.get("summary"), "coverage.summary", errors)
    if summary is not None and not isinstance(summary.get("coverage_complete"), bool):
        errors.append("coverage.summary.coverage_complete must be a boolean")
    comparisons = record.get("comparisons")
    if not isinstance(comparisons, list):
        errors.append("coverage.comparisons must be a list")
    else:
        for index, comparison in enumerate(comparisons):
            label = f"coverage.comparisons[{index}]"
            item = _require_mapping(comparison, label, errors)
            if item is None:
                continue
            if item.get("formulation") not in ("lp", "mip"):
                errors.append(f"{label}.formulation must be 'lp' or 'mip'")
            if not isinstance(item.get("equivalent"), bool):
                errors.append(f"{label}.equivalent must be a boolean")
    return errors


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def validate_scalability_artifact(payload: Any) -> list[str]:
    errors: list[str] = []
    record = _require_mapping(payload, "scalability", errors)
    if record is None:
        return errors
    if record.get("kind") != SCALABILITY_KIND:
        errors.append(f"scalability.kind must be {SCALABILITY_KIND!r}")
    if record.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append(f"scalability.schema_version must be {ARTIFACT_SCHEMA_VERSION}")
    if not is_timestamp(record.get("generated_at_utc")):
        errors.append("scalability.generated_at_utc must be an ISO-8601 UTC timestamp ending in Z")
    if not isinstance(record.get("environment"), Mapping):
        errors.append("scalability.environment must be an object")
    errors.extend(validate_provenance(record.get("provenance"), "scalability.provenance"))
    thresholds = _require_mapping(record.get("thresholds"), "scalability.thresholds", errors)
    if thresholds is not None:
        for key in ("max_time_exponent", "max_rss_growth_mb", "max_rss_growth_factor",
                    "leak_tolerance_mb", "leak_tolerance_fraction"):
            if not _is_number(thresholds.get(key)) or float(thresholds.get(key)) <= 0:
                errors.append(f"scalability.thresholds.{key} must be finite and > 0")

    families = _require_mapping(record.get("families"), "scalability.families", errors)
    if families is not None:
        if not families:
            errors.append("scalability.families must not be empty")
        for kind, family in families.items():
            label = f"scalability.families[{kind!r}]"
            item = _require_mapping(family, label, errors)
            if item is None:
                continue
            runs = item.get("runs")
            if not isinstance(runs, list) or not runs:
                errors.append(f"{label}.runs must be a non-empty list")
                continue
            for index, run in enumerate(runs):
                run_label = f"{label}.runs[{index}]"
                result = _require_mapping(run, run_label, errors)
                if result is None:
                    continue
                status = result.get("status")
                if status not in ("ok", "error"):
                    errors.append(f"{run_label}.status must be 'ok' or 'error'")
                if status == "ok":
                    if result.get("solver_status") != "optimal":
                        errors.append(f"{run_label}.solver_status must be 'optimal'")
                    if result.get("validation_passed") is not True:
                        errors.append(f"{run_label}.validation_passed must be true")
                    for key in ("wall_seconds", "peak_rss_mb"):
                        if not _is_number(result.get(key)) or float(result.get(key)) < 0:
                            errors.append(f"{run_label}.{key} must be finite and >= 0")
                    if not _is_number(result.get("objective_usd")):
                        errors.append(f"{run_label}.objective_usd must be finite")
                elif not isinstance(result.get("error"), str) or not result.get("error"):
                    errors.append(f"{run_label}.error must be a non-empty string")

    leak = _require_mapping(record.get("leak"), "scalability.leak", errors)
    if leak is not None:
        if not isinstance(leak.get("repetitions"), int) or leak.get("repetitions") < 2:
            errors.append("scalability.leak.repetitions must be an integer >= 2")
        if not isinstance(leak.get("leak_suspected"), bool):
            errors.append("scalability.leak.leak_suspected must be a boolean")
        if not _is_number(leak.get("rss_growth_mb")):
            errors.append("scalability.leak.rss_growth_mb must be finite")

    checks = record.get("checks")
    if not isinstance(checks, list):
        errors.append("scalability.checks must be a list")
    else:
        for index, check in enumerate(checks):
            label = f"scalability.checks[{index}]"
            item = _require_mapping(check, label, errors)
            if item is not None and not isinstance(item.get("passed"), bool):
                errors.append(f"{label}.passed must be a boolean")

    summary = _require_mapping(record.get("summary"), "scalability.summary", errors)
    if summary is not None:
        if not isinstance(summary.get("passed"), bool):
            errors.append("scalability.summary.passed must be a boolean")
        for key in ("checks", "failures"):
            if not isinstance(summary.get(key), int) or isinstance(summary.get(key), bool) \
                    or summary.get(key) < 0:
                errors.append(f"scalability.summary.{key} must be a non-negative integer")
    return errors


_STATISTICAL_CATEGORIES = frozenset(
    {"functional", "statistical", "performance", "scalability"})


def _validate_optional_interval(value: Any, label: str, errors: list[str]) -> None:
    if value is None:
        return
    interval = _require_mapping(value, label, errors)
    if interval is None:
        return
    for key in ("lower", "upper"):
        if not _is_number(interval.get(key)):
            errors.append(f"{label}.{key} must be finite")
    if _is_number(interval.get("lower")) and _is_number(interval.get("upper")) \
            and float(interval["lower"]) > float(interval["upper"]):
        errors.append(f"{label}.lower must not exceed upper")
    confidence = interval.get("nominal_confidence")
    if not _is_number(confidence) or not 0 < float(confidence) < 1:
        errors.append(f"{label}.nominal_confidence must be finite and between 0 and 1")
    if not isinstance(interval.get("method"), str) or not interval.get("method"):
        errors.append(f"{label}.method must be a non-empty string")


def _validate_summary(value: Any, label: str, errors: list[str]) -> None:
    summary = _require_mapping(value, label, errors)
    if summary is None:
        return
    count = summary.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        errors.append(f"{label}.count must be a positive integer")
    for key in ("median", "min", "max", "q1", "q3", "iqr", "mad"):
        if not _is_number(summary.get(key)):
            errors.append(f"{label}.{key} must be finite")
    samples = summary.get("samples")
    if not isinstance(samples, list) or not samples or not all(_is_number(v) for v in samples):
        errors.append(f"{label}.samples must be a non-empty list of finite numbers")
    _validate_optional_interval(summary.get("median_interval"),
                                f"{label}.median_interval", errors)


def validate_statistical_artifact(payload: Any) -> list[str]:
    errors: list[str] = []
    record = _require_mapping(payload, "statistical", errors)
    if record is None:
        return errors
    if record.get("kind") != STATISTICAL_KIND:
        errors.append(f"statistical.kind must be {STATISTICAL_KIND!r}")
    if record.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        errors.append(f"statistical.schema_version must be {ARTIFACT_SCHEMA_VERSION}")
    if not is_timestamp(record.get("generated_at_utc")):
        errors.append("statistical.generated_at_utc must be an ISO-8601 UTC timestamp ending in Z")
    if not isinstance(record.get("environment"), Mapping):
        errors.append("statistical.environment must be an object")
    errors.extend(validate_provenance(record.get("provenance"), "statistical.provenance"))

    methodology = _require_mapping(record.get("methodology"),
                                   "statistical.methodology", errors)
    if methodology is not None:
        for key in ("batches", "repetitions_per_batch", "warmup_repetitions_per_batch",
                    "measured_repetitions_per_workload", "scalability_batches",
                    "scalability_repetitions_per_size"):
            value = methodology.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"statistical.methodology.{key} must be a non-negative integer")
        for key in ("batches", "repetitions_per_batch", "scalability_batches",
                    "scalability_repetitions_per_size"):
            value = methodology.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value < 2:
                errors.append(f"statistical.methodology.{key} must be at least 2")

    acceptance = _require_mapping(record.get("acceptance"),
                                  "statistical.acceptance", errors)
    if acceptance is not None:
        if not isinstance(acceptance.get("passed"), bool):
            errors.append("statistical.acceptance.passed must be a boolean")
        categories = _require_mapping(acceptance.get("categories"),
                                      "statistical.acceptance.categories", errors)
        if categories is not None:
            if set(categories) != _STATISTICAL_CATEGORIES:
                errors.append("statistical.acceptance.categories must contain exactly "
                              f"{sorted(_STATISTICAL_CATEGORIES)}")
            for key, value in categories.items():
                if not isinstance(value, bool):
                    errors.append(f"statistical.acceptance.categories.{key} must be a boolean")
        _validate_checks(acceptance.get("checks"), "statistical.acceptance.checks", errors)

    performance = _require_mapping(record.get("performance"),
                                   "statistical.performance", errors)
    if performance is not None:
        batches = performance.get("batches")
        if not isinstance(batches, list) or not batches:
            errors.append("statistical.performance.batches must be a non-empty list")
        workloads = _require_mapping(performance.get("workloads"),
                                     "statistical.performance.workloads", errors)
        if workloads is not None:
            if not workloads:
                errors.append("statistical.performance.workloads must not be empty")
            for name, workload in workloads.items():
                label = f"statistical.performance.workloads[{name!r}]"
                item = _require_mapping(workload, label, errors)
                if item is None:
                    continue
                total = item.get("repetitions_total")
                if not isinstance(total, int) or isinstance(total, bool) or total < 1:
                    errors.append(f"{label}.repetitions_total must be a positive integer")
                warmups = item.get("warmup_repetitions_total")
                if not isinstance(warmups, int) or isinstance(warmups, bool) or warmups < 0:
                    errors.append(f"{label}.warmup_repetitions_total must be a non-negative integer")
                samples = _require_mapping(item.get("samples"), f"{label}.samples", errors)
                if samples is not None:
                    for key in ("wall_seconds", "solver_seconds", "peak_rss_mb"):
                        values = samples.get(key)
                        if not isinstance(values, list) or not all(_is_number(v) for v in values):
                            errors.append(f"{label}.samples.{key} must be a list of finite numbers")
                    if not samples.get("wall_seconds"):
                        errors.append(f"{label}.samples.wall_seconds must not be empty")
                summary = _require_mapping(item.get("summary"), f"{label}.summary", errors)
                if summary is not None:
                    for key in ("wall_seconds", "solver_seconds", "peak_rss_mb"):
                        if summary.get(key) is not None:
                            _validate_summary(summary.get(key), f"{label}.summary.{key}", errors)
                medians = item.get("batch_wall_medians")
                if not isinstance(medians, list) or not medians \
                        or not all(_is_number(v) for v in medians):
                    errors.append(f"{label}.batch_wall_medians must be a non-empty "
                                  "list of finite numbers")
                hashes = item.get("decision_hashes")
                if not isinstance(hashes, list) or not hashes \
                        or not all(is_sha256(v) for v in hashes):
                    errors.append(f"{label}.decision_hashes must be a non-empty list "
                                  "of SHA-256 hashes")
                solvers = item.get("solvers")
                if not isinstance(solvers, list) or not solvers \
                        or not all(isinstance(v, str) and v for v in solvers):
                    errors.append(f"{label}.solvers must be a non-empty list of names")
                _require_mapping(item.get("baseline_limits"),
                                 f"{label}.baseline_limits", errors)

    scalability = _require_mapping(record.get("scalability"),
                                   "statistical.scalability", errors)
    if scalability is not None:
        batches = scalability.get("batches")
        if not isinstance(batches, list) or not batches:
            errors.append("statistical.scalability.batches must be a non-empty list")
        thresholds = _require_mapping(scalability.get("thresholds"),
                                      "statistical.scalability.thresholds", errors)
        if thresholds is not None:
            for key in ("max_time_exponent", "max_rss_growth_mb", "max_rss_growth_factor",
                        "leak_tolerance_mb", "leak_tolerance_fraction"):
                if not _is_number(thresholds.get(key)) or float(thresholds[key]) <= 0:
                    errors.append(f"statistical.scalability.thresholds.{key} "
                                  "must be finite and > 0")
        if not isinstance(scalability.get("thresholds_consistent"), bool):
            errors.append("statistical.scalability.thresholds_consistent must be a boolean")
        families = _require_mapping(scalability.get("families"),
                                    "statistical.scalability.families", errors)
        if families is not None:
            if not families:
                errors.append("statistical.scalability.families must not be empty")
            for kind, family in families.items():
                label = f"statistical.scalability.families[{kind!r}]"
                item = _require_mapping(family, label, errors)
                if item is None:
                    continue
                sizes = item.get("sizes")
                if not isinstance(sizes, list) or len(sizes) < 2 \
                        or not all(isinstance(v, int) and not isinstance(v, bool) for v in sizes):
                    errors.append(f"{label}.sizes must contain at least two integers")
                per_size = _require_mapping(item.get("per_size"), f"{label}.per_size", errors)
                if per_size is not None:
                    for size, values in per_size.items():
                        _require_mapping(values, f"{label}.per_size[{size!r}]", errors)
                growth = _require_mapping(item.get("growth"), f"{label}.growth", errors)
                if growth is not None:
                    exponent = growth.get("time_exponent")
                    if exponent is not None and not _is_number(exponent):
                        errors.append(f"{label}.growth.time_exponent must be finite or null")
                    _validate_optional_interval(
                        growth.get("time_exponent_bootstrap_interval"),
                        f"{label}.growth.time_exponent_bootstrap_interval", errors)
                    for key in ("rss_growth_mb", "rss_growth_factor"):
                        if not _is_number(growth.get(key)):
                            errors.append(f"{label}.growth.{key} must be finite")
                replicates = _require_mapping(item.get("replicates_per_size"),
                                              f"{label}.replicates_per_size", errors)
                if replicates is not None:
                    for size, count in replicates.items():
                        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                            errors.append(f"{label}.replicates_per_size[{size!r}] "
                                          "must be a positive integer")
        leak = _require_mapping(scalability.get("leak"),
                                "statistical.scalability.leak", errors)
        if leak is not None:
            if not isinstance(leak.get("leak_suspected"), bool):
                errors.append("statistical.scalability.leak.leak_suspected must be a boolean")
            growths = leak.get("rss_growth_mb")
            if not isinstance(growths, list) or not growths \
                    or not all(_is_number(v) for v in growths):
                errors.append("statistical.scalability.leak.rss_growth_mb must be a "
                              "non-empty list of finite numbers")
            if not isinstance(leak.get("batch_values"), list) or not leak.get("batch_values"):
                errors.append("statistical.scalability.leak.batch_values must be a non-empty list")
        _validate_checks(scalability.get("checks"),
                         "statistical.scalability.checks", errors)
    return errors


def _validate_checks(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a non-empty list")
        return
    for index, check in enumerate(value):
        item = _require_mapping(check, f"{label}[{index}]", errors)
        if item is None:
            continue
        category = item.get("category")
        if category not in _STATISTICAL_CATEGORIES:
            errors.append(f"{label}[{index}].category must be one of "
                          f"{sorted(_STATISTICAL_CATEGORIES)}")
        if not isinstance(item.get("name"), str) or not item.get("name"):
            errors.append(f"{label}[{index}].name must be a non-empty string")
        if not isinstance(item.get("passed"), bool):
            errors.append(f"{label}[{index}].passed must be a boolean")
        if not isinstance(item.get("detail"), str):
            errors.append(f"{label}[{index}].detail must be a string")


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
