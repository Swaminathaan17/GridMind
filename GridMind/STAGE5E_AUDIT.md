# Stage 5E independent audit and evidence log

**Branch:** `stage-5e-work-in-progress`
**HEAD at audit start:** `683cf6b` (worktree clean)
**Audit date:** 2026-11-01
**Scope:** read-only verification of the existing Stage 5E work, followed by two
focused units: the CI reproducibility gate and the documentation/evidence
consolidation. Stage 5E is **not accepted** by this document.

## Audit method

- Checked the checked-out branch and preserved the existing worktree; no reset,
  clean, rebase, branch switch, commit, merge, tag or push was performed.
- Read `STAGE5E_QUALIFICATION.md`, `STAGE5D_READINESS.md`,
  `benchmarks/README.md`, `benchmarks/qualification.py`,
  `benchmarks/schema.py`, `benchmarks/solver_coverage.py`,
  `benchmarks/scalability.py`, the committed baseline and existing run artifacts.
- Ran the full suite and the focused qualification tests. Existing local
  benchmark artifacts were inspected and schema-validated; the scalability harness
  was independently rerun at its default sizes as a reproducibility check.

## Requirement matrix

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| 1 | Reproducible packaging and dependency management | VERIFIED | `pyproject.toml`, `requirements-lock.txt`, `requirements.txt`; CI installs the lock and the package with `--no-deps`; full suite passes under Python 3.11.17. |
| 2 | CI and automated verification | **VERIFIED LOCALLY; REMOTE PENDING** | CI runs the full suite, solver discovery, baseline structural validation and a two-repetition smoke measurement on Windows and Linux. This audit added the missing baseline comparison: the smoke manifest is now gated with `--reproducibility-only`. The gate logic is locally verified; remote workflow execution is **not yet observed** from this workstation. |
| 3 | Qualification harness and performance gates | VERIFIED | `benchmarks/qualification.py`; committed `benchmarks/baseline.json`; smoke gate `PASS (6 checks, 0 failures)` on 2026-11-01. Time/RSS limits are explicitly reference-machine-only. |
| 4 | Benchmark schema and provenance | VERIFIED | `benchmarks/schema.py` validates measurements, baselines, coverage and scalability artifacts; tests reject missing provenance, malformed hashes, missing timestamps and invalid solver identity. The committed baseline is explicitly marked `legacy: true`, documenting reconstruction rather than silently upgrading it. |
| 5 | Multi-solver validation | BLOCKED | `benchmarks/solver_coverage.py` and tests exist. Local evidence covers `appsi_highs` only (LP objective 20300 USD, MIP objective 400 USD, `coverage_complete=true`). `glpk` and `cbc` are not installed, so cross-solver parity is **not** verified and is reported unavailable, not passed. |
| 6 | Scalability and peak memory evidence | **PARTIALLY VERIFIED (LOCAL ONLY)** | `benchmarks/scalability.py` and guardrail tests exist; local run `stage5e_e6_scalability_20261031` passed 13/13 checks (LP and MIP growth, peak RSS, repeated-run leak probe). A committed summary now exists in `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`, but the raw artifact remains under gitignored `benchmarks/runs/`, one sample per size is not statistical evidence, and the opt-in performance test is skipped by default. |
| 7 | Numerical robustness | VERIFIED | `tests/test_numerical_robustness.py`: explicit tolerance boundary, tiny/large scales, ill-conditioned costs, zero demand, strict infeasibility, determinism and near-zero battery MIP. All pass. |
| 8 | Documentation and reproducibility | **COMPLETED LOCALLY** | This audit, `STAGE5E_QUALIFICATION.md`, `benchmarks/README.md`, `README.md`, `STAGE5D_READINESS.md` and `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md` now match the measured state. Stage 5D is identified as the last accepted stage; Stage 5E is explicitly not accepted. Remote CI, multi-solver parity, local-only scalability limits and the intentional skipped test are stated rather than implied as passes. |

## Selected implementation unit

**Gap:** CI measured a smoke workload but never compared its decision hash or
solver identity with `benchmarks/baseline.json`. A deterministic but changed
decision would therefore pass CI without being detected.

**Change:**

- Added `require_performance: bool = True` to
  `benchmarks/qualification.evaluate_gate`.
- Added `gate --reproducibility-only`, which keeps schema validation,
  determinism, decision-hash equality and solver-identity checks while omitting
  machine-specific wall, solver-time and RSS thresholds.
- Wired CI to gate the generated smoke manifest against the committed baseline.
- Documented the option in `benchmarks/README.md` and corrected the CI statement
  in `STAGE5E_QUALIFICATION.md`.

**Files changed in this unit:**

- `GridMind/benchmarks/qualification.py`
- `GridMind/tests/test_qualification.py`
- `GridMind/benchmarks/README.md`
- `GridMind/STAGE5E_QUALIFICATION.md`
- `.github/workflows/ci.yml`
- `GridMind/STAGE5E_AUDIT.md` (this audit)

## Documentation and evidence consolidation (second unit)

**Gap:** Requirement 8 was incomplete. Documentation still described Stage 5E as
P0-only, reported stale test counts and branch/commit context, and the existing
scalability evidence had no committed summary.

**Change:**

- Updated `README.md`, `STAGE5D_READINESS.md` and `STAGE5E_QUALIFICATION.md` to
  the current branch/HEAD, test result, solver reality and stage status.
- Added `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md` from the existing
  `stage5e_e6_scalability_20261031` artifact, with environment, configuration,
  measured values, honest limitations, reproduction command and schema validation.
- Documented the optional scalability, solver-coverage and opt-in performance
  commands in `benchmarks/README.md`.
- Updated this audit to distinguish locally verified, remote-pending, blocked and
  partially verified requirements.

**Files changed in this unit:**

- `GridMind/README.md`
- `GridMind/STAGE5D_READINESS.md`
- `GridMind/STAGE5E_QUALIFICATION.md`
- `GridMind/benchmarks/README.md`
- `GridMind/benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md` (new)
- `GridMind/STAGE5E_AUDIT.md` (this audit)

No code, model, solver default, CLI contract, branch or commit was changed in this
unit.

## Commands and results

Baseline audit, before changes:

```text
python -m pytest -q
255 passed, 1 skipped in 125.98s
```

Focused implementation tests:

```text
python -m pytest -q tests/test_qualification.py
27 passed in 1.70s
```

Independent smoke evidence:

```text
python -m benchmarks.qualification measure --out benchmarks/runs/audit_smoke_20261101 --workloads smoke --repetitions 2
smoke status=ok median_wall=0.034009s deterministic=True

python -m benchmarks.qualification gate --measurements benchmarks/runs/audit_smoke_20261101/measurements.json --baseline benchmarks/baseline.json --workloads smoke
PASS (6 checks, 0 failures)

python -m benchmarks.qualification gate --measurements benchmarks/runs/audit_smoke_20261101/measurements.json --baseline benchmarks/baseline.json --workloads smoke --reproducibility-only
PASS (3 checks, 0 failures)
```

Final regression run after changes:

```text
python -m pytest -q
258 passed, 1 skipped in 125.62s
```

The skipped test is the opt-in scalability performance benchmark
(`GRIDMIND_RUN_PERF != 1`); it is not counted as a pass.

Documentation-unit validation:

```text
python -m pytest -q
258 passed, 1 skipped in 99.84s

scalability schema validation
schema errors: 0
summary: {'checks': 13, 'failures': 0, 'passed': True}

coverage schema validation
schema errors: 0
summary: {'coverage_complete': True, 'available_solvers': ['appsi_highs'], 'unavailable_solvers': ['glpk', 'cbc']}

python -m benchmarks.qualification validate-baseline --baseline benchmarks/baseline.json
VALID

python -m benchmarks.qualification gate --measurements benchmarks/runs/stage5e_baseline_20261031c/measurements.json --baseline benchmarks/baseline.json
error: invalid measurements (legacy raw artifact predates schema hardening; documented in STAGE5E_QUALIFICATION.md)

python -m benchmarks.scalability run --out benchmarks/runs/audit_scalability_20261101
summary passed=True failures=0/13

referenced-path check
all 17 primary referenced paths exist; 4 additional install/measurement paths also exist

git diff --check
(no output)

python -m benchmarks.qualification --help
python -m benchmarks.scalability --help
python -m benchmarks.solver_coverage --help
(all parse successfully)
```

## Existing evidence inspected

- `benchmarks/runs/stage5e_e5_coverage_20261031c/coverage.json`: `appsi_highs`
  passed LP and MIP probes; `glpk` and `cbc` unavailable.
- `benchmarks/runs/stage5e_e6_scalability_20261031/scalability.json`: 13 checks,
  0 failures; LP/MIP growth exponents 0.049/-0.021, maximum observed RSS growth
  1.9 MiB, leak probe 1.3 MiB over 5 repetitions. Schema validation: **0 errors**.
  Summarised in `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`.
- `benchmarks/runs/audit_scalability_20261101/scalability.json`: independent local
  rerun of the same defaults; 13 checks, 0 failures. Not additional capacity
  evidence.
- `benchmarks/baseline.json`: eight workloads, thresholds and legacy provenance
  consistent with the qualification report; `validate-baseline` returns `VALID`.
  The retained raw baseline measurement predates schema hardening and no longer
  validates under the current schema, so the historical 48-check self-check is not
  currently reproducible from it. Current smoke gates use a fresh measurement.

## Outstanding issues

1. CI needs an actual remote run to confirm the reproducibility gate on Windows
   and Linux. Local workflow logic is verified; remote execution is pending.
2. `glpk` and `cbc` are unavailable locally; multi-solver parity remains blocked
   until those executables are supplied and coverage is rerun.
3. Scalability evidence is local-only, came from a dirty worktree, uses one
   repetition per size and is dominated by process startup in its wall-time
   exponent. The committed report and reproduction command now exist, but this is
   not production-scale or portability evidence.
4. Benchmark statistical power (E9) remains open.
5. The qualification baseline is reference-machine-only and was captured with
   other workstation applications running.
6. The raw baseline measurement file is legacy and does not validate under the
   hardened schema. `baseline.json` is valid and current gates use fresh
   measurements, but rerunning the original 48-check self-check would require a
   migration of that raw artifact.

**Stage 5E is not accepted.** Completing the documentation unit closes the
documentation gap; it does not resolve remote CI, solver parity or the local-only
scalability/statistical limits.

## Recommended next unit

**Remote CI and multi-solver verification:** observe the Windows/Linux workflow
with the reproducibility gate, then supply GLPK and CBC and rerun
`benchmarks.solver_coverage` before making any cross-solver parity claim. After
that, assess benchmark statistical power (E9) and decide whether the local
scalability evidence requires a stronger sampling plan.
