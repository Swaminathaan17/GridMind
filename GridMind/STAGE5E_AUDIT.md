# Stage 5E independent audit and evidence log

**Branch:** `stage-5e-work-in-progress`
**HEAD at audit start:** `683cf6b` (worktree clean)
**Audit date:** 2026-11-01
**Follow-up review HEAD:** `6ce5ee601bd7f7cf36371429c6d7d193075f04ee` (2026-11-02,
review changes uncommitted)
**CI on pushed HEAD:** latest GitHub Actions run for `6ce5ee6`: Windows and Ubuntu
jobs passed (maintainer-reported; the GitHub API and Actions page returned 404
from this workstation and `gh` is unavailable, so the run metadata was not
independently re-fetched). The uncommitted review changes in this worktree were
not part of that run and are verified locally only.
**Scope:** read-only verification of the existing Stage 5E work, followed by the CI
reproducibility gate, documentation/evidence consolidation, the E9 statistical
reliability follow-up, and the E5 multi-solver closure. Stage 5E is **not accepted**
by this document: E5 is now verified locally, but clean-provenance release evidence
and a final acceptance review remain outstanding.

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
- Installed GLPK 4.65 and CBC 2.10.13 from official binary archives outside the
  repository, verified their executable versions, and reran solver coverage with
  both directories on `PATH`.

## Requirement matrix

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| E1 | Reproducible packaging, dependency management and CI | VERIFIED | `pyproject.toml`, `requirements-lock.txt`, constrained `requirements.txt`; lock install and `pip check` pass. Latest GitHub Actions run for pushed commit `6ce5ee6` passed Windows and Ubuntu (maintainer-reported; see header limitation). The uncommitted review diff is not covered by that run. |
| E2 | Environment/provenance manifest and committed baseline | VERIFIED | `benchmarks/baseline.json` validates; provenance records branch, commit, dirty flag, environment hash, input hashes and solver identities. |
| E3 | Deterministic decision hashing and performance gate | VERIFIED | Fresh smoke measurement passed the gate with stable decision hashes; timing fields are excluded from hashing and strict limits remain reference-machine-only. |
| E4 | Artifact schema and provenance hardening | VERIFIED | `benchmarks/schema.py` validates measurement, baseline, coverage, scalability and statistical artifacts; tests reject missing provenance, malformed hashes, missing timestamps and invalid solver identity. The baseline is explicitly `legacy: true`. |
| E5 | Multi-solver validation | **VERIFIED LOCALLY** | Fresh run `stage5e_e5_coverage_glpk_cbc_20261102` with GLPK 4.65, CBC 2.10.13 and `appsi_highs` 1.15.1: all three passed LP and MIP, `coverage_complete=true`, objective deltas `0.0` against the GLPK reference, and maximum constraint violation `0.0`. The `1e-6` relative objective tolerance was met. |
| E6 | Scalability and peak-memory evidence | **VERIFIED LOCALLY (LIMITED)** | Original run passed 13/13 guardrails; fresh E9 evidence adds 3 batches x 3 repetitions per size. Evidence is synthetic and local-only, not production-capacity or portability evidence. |
| E7 | Numerical robustness | VERIFIED | `tests/test_numerical_robustness.py` covers tolerance boundaries, scale extremes, ill-conditioned costs, zero demand, strict infeasibility, determinism and near-zero battery MIP. |
| E8 | Documentation and reproducibility | **COMPLETED LOCALLY** | This audit, `STAGE5E_QUALIFICATION.md`, `benchmarks/README.md`, `README.md`, `STAGE5E_ACCEPTANCE_REPORT.md` and `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md` distinguish CI-tested `6ce5ee6` from the uncommitted local review changes. `STAGE5D_READINESS.md` remains a historical Stage 5D record. |
| E9 | Benchmark statistical power | **VERIFIED LOCALLY** | Fresh artifact `stage5e_e9_final_20261102` passed all four acceptance categories with 15 measured samples per performance workload and 9 per scalability size. Provenance preserves `dirty: true`; this is not clean-tree release evidence. |

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
- Updated this audit to distinguish locally verified, CI-tested, limited and
  blocked requirements.

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
- `benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102/coverage.json`: all
  three solvers passed LP and MIP probes; schema validation 0 errors. LP objective
  `20300.0` and MIP objective `400.0` for every solver, with zero deltas and zero
  maximum constraint violation. SHA-256
  `C68318F0B5250252E99132CFCD379F5281D338B800921B277208168B7F82E950`.
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

1. E5 parity is verified locally, but GLPK and CBC are external optional binaries
   and are not bundled or installed by the Python package. Reproduction requires
   the documented downloads, checksums and `PATH` setup.
2. E6 scalability evidence is synthetic and local-only. The fresh E9 artifact adds
   multi-batch uncertainty, but it is still not production-scale or cross-machine
   portability evidence.
3. The qualification baseline is reference-machine-only and was captured with
   other workstation applications running.
4. The raw baseline measurement file is legacy and does not validate under the
   hardened schema. `baseline.json` is valid and current gates use fresh
   measurements, but rerunning the original 48-check self-check would require a
   migration of that raw artifact.
5. The E9 artifact records `dirty: true`; regenerate it from a committed worktree
   if clean-provenance release evidence is required.

**Stage 5E is not accepted.** E1-E9 have local evidence and E5 is no longer
blocked, but clean-provenance E9 evidence and a final acceptance review remain
outstanding.

## Follow-up evidence review (2026-11-02)

**HEAD:** `6ce5ee601bd7f7cf36371429c6d7d193075f04ee`, branch
`stage-5e-work-in-progress`. The review changes are uncommitted, so all generated
provenance records `dirty: true`.

**Code and test changes:**

- `benchmarks/qualification.py` now catches child-process timeouts and start
  failures and records them as workload errors instead of aborting the run.
- `benchmarks/stats.py` exposes `minimum_median_interval_samples` (6 at 95%) so
  the sample requirement is explicit rather than implicit.
- `benchmarks/reliability.py` now requires at least one warm-up, checks batch
  completeness and warm-up recording, checks both the median point estimate and
  the conservative interval upper bound, rejects zero successful performance
  repetitions, rejects fewer than two successful scalability sizes, ignores empty
  size groups, and records the sample requirement and warm-up/outlier policy in
  the artifact methodology.
- Added `tests/test_reliability.py` and expanded `tests/test_stats.py` and
  `tests/test_qualification.py`.

**Verification:**

```text
python -m pytest -q
294 passed, 1 skipped in 198.70s

python -m pytest -q tests/test_stats.py tests/test_qualification.py tests/test_scalability.py tests/test_reliability.py
68 passed, 1 skipped in 5.41s

python -m benchmarks.reliability run --out benchmarks/runs/stage5e_e9_final_20261102
acceptance passed=True categories={'functional': True, 'statistical': True, 'performance': True, 'scalability': True}

python -m benchmarks.solver_coverage run --out benchmarks/runs/stage5e_e5_coverage_20261102
coverage_complete=True available=['appsi_highs'] unavailable=['glpk', 'cbc']
```

**Fresh E9 evidence:** `benchmarks/runs/stage5e_e9_final_20261102/reliability.json`
(gitignored), schema validation 0 errors, generated `2026-11-02T15:16:28Z`.
Configuration: 3 batches x 5 measured repetitions + 1 warm-up for each performance
workload (15 measured samples), and 3 batches x 3 repetitions per scalability size
(9 samples per size). The median interval achieved coverage `0.964844` (order
statistic 4). All functional, statistical, performance and scalability categories
passed; the leak probe was not suspected. The full table is in
`STAGE5E_QUALIFICATION.md` and the artifact is summarised in
`benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`.

**What this closes:** E9 benchmark statistical power is verified locally with a
reproducible sampling plan, explicit minimum sample size, uncertainty intervals,
outlier retention and fail-closed gates.

## E5 multi-solver closure (2026-11-02)

**Solvers installed outside the repository:**

| Solver | Binary | Version | Archive SHA-256 |
|---|---|---|---|
| GLPK | `glpk-4.65\w64\glpsol.exe` | `GLPSOL: GLPK LP/MIP Solver, v4.65` | `A8C195B92682C3AC1F6DAE5F6CE2472C2478835F9873BE4A0C6771C37DB53C2D` |
| CBC | `cbc-msvc\bin\cbc.exe` | `Version: 2.10.13` | `B9702AD7501B4249A9721984CE3C6DC8FB9B6CFB995F42D493A5BD54B8F42A74` |

GLPK was downloaded from the official WinGLPK 4.65 archive and CBC from the
official Coin-OR 2.10.13 Windows MSVC archive. The MinGW CBC archive was not
usable on this machine because a required DLL was absent; the MSVC build runs.
Both directories were prepended to `PATH` for `check-solver` and coverage. The
coverage harness clears `GRIDMIND_SOLVER_PATH` in its child probes, so installing
with that variable alone is insufficient for this command.

A version-capture defect was found and fixed: CBC ignores `--version` as a command
but prints `Version: 2.10.13` in its banner, and the inventory previously recorded
only `Welcome to the CBC MILP Solver`. `benchmarks/qualification.py` now parses the
CBC version line, with a focused unit test.

**Coverage command and result:**

```text
$env:PATH = "<glpk>\w64;<cbc>\bin;$env:PATH"
python -m benchmarks.solver_coverage run --out benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102
  glpk           available=True  lp=passed      mip=passed
  cbc            available=True  lp=passed      mip=passed
  appsi_highs    available=True  lp=passed      mip=passed
coverage_complete=True available=['glpk', 'cbc', 'appsi_highs'] unavailable=[]
exit=0
```

Artifact `coverage.json` was generated `2026-11-02T17:48:59Z` to
`2026-11-02T17:49:10Z`, schema validation returned 0 errors, and its SHA-256 is
`C68318F0B5250252E99132CFCD379F5281D338B800921B277208168B7F82E950`. LP objective
was `20300.0` for all three solvers; MIP objective was `400.0` for all three;
objective delta and maximum independent constraint violation were `0.0` in every
case, within the harness's `1e-6` relative objective tolerance. LP and MIP
objectives were compared only within their own formulation.

Regression after the solver-version parser change:

```text
python -m pytest -q
295 passed, 1 skipped in 222.67s

python -m pytest -q tests/test_qualification.py tests/test_solver_coverage.py tests/test_failure_handling.py tests/test_numerical_robustness.py
64 passed in 9.66s
```

The skipped test is the opt-in scalability performance benchmark
(`GRIDMIND_RUN_PERF != 1`); it is not counted as a pass.

**What remains blocked:**

1. The E9 artifact and E6 scalability evidence are local-only, synthetic and
   recorded from a dirty worktree; they are not portability or clean-provenance
   release evidence.
2. The E5 probes are the harness's designed tiny LP/MIP formulations, not
   production-scale or cross-machine performance evidence.

CI is no longer an open blocker: the pushed `6ce5ee6` run passed Windows and Ubuntu
(maintainer-reported; metadata not independently re-fetched from this workstation).

**Stage 5E remains not accepted.** E5 no longer blocks it. The acceptance report
`STAGE5E_ACCEPTANCE_REPORT.md` records the updated E1-E9 matrix and the remaining
clean-provenance requirement.

## Recommended next unit

**Clean-provenance acceptance review:** commit the reviewed changes only when
explicitly authorised, regenerate the E9 artifact from the committed worktree, and
rerun the acceptance review. E5 itself is closed; no further solver installation or
parity claim is pending.
