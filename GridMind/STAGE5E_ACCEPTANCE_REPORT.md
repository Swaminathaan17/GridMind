# Stage 5E acceptance report

**Decision: NOT ACCEPTED (E5 closed; clean-provenance review pending)**

**Branch:** `stage-5e-work-in-progress`
**Review HEAD:** `6ce5ee601bd7f7cf36371429c6d7d193075f04ee`
**Review date:** 2026-11-02
**CI on pushed HEAD:** latest GitHub Actions run for `6ce5ee6` passed the Windows
and Ubuntu jobs (maintainer-reported; the GitHub API and Actions page returned 404
from this workstation and `gh` is unavailable, so the run metadata was not
independently re-fetched). This CI result applies to the pushed commit, not to the
uncommitted review changes in this worktree.
**Last accepted stage:** Stage 5D

Stage 5E's implementation and local evidence are complete for E1-E9. E5
multi-solver parity is now verified locally with GLPK, CBC and `appsi_highs`; it no
longer blocks acceptance. The fresh E9 artifact still records `dirty: true` because
the reviewed fixes were not committed, and that provenance is preserved.
Acceptance remains pending clean-provenance E9 evidence and a final review.

## Scope

This report covers the Stage 5E reproducibility and qualification work only. It
does not accept Stage 5E, does not change the Stage 5D acceptance status, and does
not begin Stage 5F. No model, solver default, dispatch rule or public CLI contract
was changed.

## Environment

- Windows 10, 8 logical CPUs, 16,198 MiB RAM.
- CPython 3.11.17; pyomo 6.10.1; highspy 1.15.1; numpy 2.4.6; pandas 3.1.0;
  scipy 1.17.1; pytest 9.2.0; matplotlib 3.11.2.
- `appsi_highs` 1.15.1 available and MIP-capable. Optional GLPK 4.65 and CBC
  2.10.13 binaries were supplied separately for coverage.
- `pip check`: no broken requirements.

## Verification executed

```text
python -m pytest -q
295 passed, 1 skipped in 222.67s

python -m pytest -q tests/test_qualification.py tests/test_solver_coverage.py tests/test_failure_handling.py tests/test_numerical_robustness.py
64 passed in 9.66s

python -m benchmarks.reliability run --out benchmarks/runs/stage5e_e9_final_20261102
acceptance passed=True categories={'functional': True, 'statistical': True, 'performance': True, 'scalability': True}

$env:PATH = "<glpk>\w64;<cbc>\bin;$env:PATH"
python -m benchmarks.solver_coverage run --out benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102
coverage_complete=True available=['glpk', 'cbc', 'appsi_highs'] unavailable=[]
```

The skipped test is the opt-in scalability performance benchmark
(`GRIDMIND_RUN_PERF != 1`); it is not counted as a pass.

## Requirement matrix

| # | Requirement | Status | Evidence / remaining gap |
|---|---|---|---|
| E1 | Reproducible packaging, dependency management and CI | VERIFIED | `pyproject.toml`, `requirements-lock.txt`, constrained `requirements.txt`; lock install and `pip check` pass. Latest GitHub Actions run for pushed `6ce5ee6` passed Windows and Ubuntu (maintainer-reported; run metadata not independently re-fetched because the API returned 404 and `gh` is unavailable). The uncommitted review diff is not covered by that run. |
| E2 | Environment/provenance manifest and committed baseline | VERIFIED | `benchmarks/baseline.json` validates; provenance records commit, branch, dirty flag, environment hash, input hashes and solver identities. |
| E3 | Deterministic decision hashing and performance gate | VERIFIED | Qualification gate passes on a fresh smoke measurement; decision hashes are stable across repetitions; timing fields are excluded from hashing. |
| E4 | Artifact schema and provenance hardening | VERIFIED | `benchmarks/schema.py` validates measurement, baseline, coverage, scalability and statistical artifacts; malformed hashes, missing provenance and invalid solver identity are rejected by tests. |
| E5 | Multi-solver validation | **VERIFIED LOCALLY** | GLPK 4.65, CBC 2.10.13 and `appsi_highs` 1.15.1 each pass LP and MIP coverage. LP objective `20300.0` and MIP objective `400.0` for every solver; objective delta `0.0` and maximum independent constraint violation `0.0`, within `1e-6` relative tolerance. Artifact schema errors 0. |
| E6 | Scalability and peak-memory evidence | **VERIFIED LOCALLY (LIMITED)** | Original run passed 13/13 guardrails; fresh E9 run adds 3 batches x 3 repetitions per size. Evidence is synthetic/local-only, not production capacity or portability. |
| E7 | Numerical robustness | VERIFIED | `tests/test_numerical_robustness.py` covers tolerance boundaries, scale extremes, ill-conditioned costs, zero demand, strict infeasibility, determinism and near-zero battery MIP. |
| E8 | Documentation and reproducibility | COMPLETED LOCALLY | `README.md`, `STAGE5E_QUALIFICATION.md`, `benchmarks/README.md`, `STAGE5E_AUDIT.md`, this report and the scalability evidence report match the measured state and distinguish CI-tested `6ce5ee6` from uncommitted local changes. `STAGE5D_READINESS.md` is retained as a historical Stage 5D record. |
| E9 | Benchmark statistical power | VERIFIED LOCALLY | Fresh artifact `benchmarks/runs/stage5e_e9_final_20261102/reliability.json`; all four acceptance categories pass with 15 measured samples per performance workload and 9 samples per scalability size. |

## E9 result summary

Artifact: `benchmarks/runs/stage5e_e9_final_20261102/reliability.json` (gitignored
raw evidence, schema validation 0 errors), generated `2026-11-02T15:16:28Z`,
harness `5E.4`. Provenance records commit `6ce5ee6`, branch
`stage-5e-work-in-progress`, `dirty: true`; the artifact is preserved as generated
and is not presented as clean-tree release evidence.

| Workload | n | Wall median (s) | Wall 95% CI (s) | Wall limit (s) | RSS max (MiB) |
|---|---:|---:|---|---:|---:|
| `smoke` | 15 | 0.080451 | 0.068893 - 0.102671 | 0.119 | 97.1 |
| `benchmark` | 15 | 0.728311 | 0.662253 - 0.917734 | 0.992 | 99.8 |
| `control_default` | 15 | 5.690944 | 4.921989 - 7.594785 | 8.004 | 108.2 |
| `mp_benchmark` | 15 | 6.679710 | 5.929645 - 7.769540 | 8.946 | 117.1 |

| Family | Sizes | Samples/size | Time exponent | Bootstrap 95% CI |
|---|---|---:|---:|---|
| `dispatch_lp` | 10, 40, 160 | 9 | 0.087698 | 0.007223 - 0.133149 |
| `schedule_mip` | 8, 24, 48 | 9 | 0.120748 | -0.025166 - 0.161263 |

Method: 3 independent batches x 5 measured repetitions + 1 warm-up per performance
workload; 3 batches x 3 repetitions per scalability size; distribution-free
binomial median interval (achieved coverage 0.964844, minimum n=6); 10,000-resample
bootstrap exponent interval with seed `20261101`; Tukey outliers flagged and
retained. The leak probe was not suspected in any batch.

Interpretation: these results are limited to the synthetic workloads, the observed
size ranges and the reference machine. The scalability exponents are influenced by
Pyomo/process startup and are not asymptotic complexity claims.

## E5 result summary

Artifact: `benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102/coverage.json`
(gitignored raw evidence, schema validation 0 errors), generated
`2026-11-02T17:48:59Z` to `2026-11-02T17:49:10Z`, harness `5E.4`. SHA-256:
`C68318F0B5250252E99132CFCD379F5281D338B800921B277208168B7F82E950`.
Provenance records commit `6ce5ee6`, branch `stage-5e-work-in-progress`,
`dirty: true`.

| Solver | Version | LP objective | MIP objective | Max violation |
|---|---|---:|---:|---:|
| GLPK | `GLPSOL: GLPK LP/MIP Solver, v4.65` | 20300.0 | 400.0 | 0.0 |
| CBC | `Version: 2.10.13` | 20300.0 | 400.0 | 0.0 |
| `appsi_highs` | `1.15.1` | 20300.0 | 400.0 | 0.0 |

All deltas against the GLPK reference are `0.0`. LP and MIP objectives are
compared only within their own formulation. The harness's relative objective
tolerance is `1e-6`, and its independent validator reported zero maximum
constraint violation for every probe.

Reproduction requires the official GLPK 4.65 and CBC 2.10.13 archives, the
checksums, and `PATH` setup documented in `benchmarks/README.md`. The binaries are
not Python dependencies or bundled artifacts. The coverage child probes clear
`GRIDMIND_SOLVER_PATH`, so both directories must be on `PATH`.

## Defects fixed during this review

- Child-process timeouts and start failures are now recorded as workload errors
  instead of aborting the qualification run.
- Reliability gates now reject zero successful performance repetitions, missing
  warm-ups, incomplete batches, fewer than two successful scalability sizes and
  empty size groups.
- The 95% median interval now has an explicit minimum sample requirement (6);
  below it the interval is absent and the gate fails rather than inventing a bound.
- Performance acceptance checks both the median point estimate and the interval
  upper bound, while still retaining outliers.
- Solver inventory now captures CBC's actual version line instead of the banner
  text, with a focused parser test.
- Added dedicated reliability tests and expanded statistics/qualification tests.

## Remaining blockers

1. **Clean-provenance evidence (release hygiene):** the E9 and E6 artifacts are
   local, synthetic and recorded from a dirty worktree. Regenerate the E9 artifact
   after committing the review changes if clean-tree release evidence is required.
2. **Scope limit:** E5 verifies the harness's tiny LP/MIP formulations, not
   production-scale or cross-machine performance. It does not recalibrate the
   reference-machine baseline.

E5 GLPK/CBC parity is no longer a blocker.

## Acceptance recommendation

**Do not accept Stage 5E yet.** E1 packaging/CI is verified for pushed `6ce5ee6`,
and E1-E9 have local evidence, including verified LP/MIP parity for GLPK, CBC and
`appsi_highs`. Regenerate the E9 artifact from a clean commit and rerun this
acceptance review; E5 requires no further work.

## Reproduction

From the `GridMind` directory:

```powershell
python -m benchmarks.reliability run `
  --out benchmarks/runs/stage5e_e9_final_<label>

$env:PATH = "<glpk>\w64;<cbc>\bin;$env:PATH"
python -m benchmarks.solver_coverage run `
  --out benchmarks/runs/stage5e_e5_<label>

python -m pytest -q
```

GLPK and CBC installation URLs, SHA-256 checksums, `PATH` setup and the
`GRIDMIND_SOLVER_PATH` caveat are in `benchmarks/README.md`. `benchmarks/runs/`
is gitignored. Use a new output label for every run; `reliability run`,
`qualification measure` and `solver_coverage run` refuse to overwrite existing
evidence.
