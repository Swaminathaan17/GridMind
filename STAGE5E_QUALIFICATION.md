> **Final status update — 2026-11-03:** The earlier dated status/history below is
> retained as a record of the work in progress and is superseded by the final
> acceptance decision in [STAGE5E_ACCEPTANCE_REPORT.md](STAGE5E_ACCEPTANCE_REPORT.md).
> Stage 5E is now **ACCEPTED for its stated reproducibility and qualification
> scope**, with synthetic/local benchmark and solver-probe limitations.
>
> Clean E9 artifact:
> `benchmarks/runs/stage5e_e9_clean_20261103/reliability.json` (local/gitignored),
> SHA-256
> `20EBB33B32A4C2DAC8F11766FA2FA74991052E7D8C310C662270A1117D3FF201`.
> It was generated from clean detached worktree commit
> `10eded5c056c15b45c341a39c7f73b80fcdea2ed`, with `dirty=false`.
> Schema validation returned 0 errors; acceptance passed all four categories:
> functional, statistical, performance and scalability. The clean-worktree solver
> inventory had `appsi_highs` 1.15.1; GLPK/CBC were unavailable there. Three-solver
> LP/MIP parity remains supported by the separate E5 coverage artifact.
>
> Latest GitHub Actions run `41623451166` passed Ubuntu and Windows for commit
> `10eded5`. The raw E9/E5 artifacts remain local and gitignored; hashes are
> recorded in the acceptance report. Documentation commits after the tested code
> commit do not alter the code revision represented by E9.

# Stage 5E qualification status (E5 and E9 complete, acceptance pending clean provenance)

**Branch:** `stage-5e-work-in-progress`
**Review HEAD:** `6ce5ee601bd7f7cf36371429c6d7d193075f04ee` (review changes are uncommitted)
**Baseline commit:** `20eb70e786f048a8a6bdd5e2798314c920992020`
**Baseline captured:** 2026-10-31T05:20:09Z
**Tests:** 295 passed, 1 intentionally skipped (full suite, verified 2026-11-02)
**CI on pushed HEAD:** latest GitHub Actions run for `6ce5ee6` passed Windows and Ubuntu
(maintainer-reported; API/page returned 404 and `gh` is unavailable, so metadata was not
independently re-fetched). The uncommitted review changes are verified locally only.
**Status:** Stage 5E is **not accepted**. E1-E4 and E6-E9 have local evidence, and E5
multi-solver parity is now **verified locally**; E6 remains local-only and limited. Acceptance
still requires clean-provenance E9 evidence and a final review.

**Stage status:** Stage 5D is the last officially accepted stage. Nothing in this report accepts
Stage 5E or turns local timing evidence into a portability claim.

## Baseline scope (P0)

P0 establishes the minimum reproducible qualification foundation:

- **E1:** package metadata, pinned dependencies and CI on Windows and Linux.
- **E2:** environment/provenance manifest and committed performance baseline.
- **E3:** deterministic decision hashing and an absolute performance gate.

No optimisation model, solver default, dispatch rule or public CLI contract was
changed. Strict timing and memory limits are for the reference machine only.

## Current Stage 5E evidence status

| Area | Status on 2026-11-02 | Evidence / limitation |
|---|---|---|
| Packaging and pins | Verified locally | `pyproject.toml`, `requirements-lock.txt`, constrained `requirements.txt`; the lock installs, `pip check` is clean and the suite passes. |
| Full test suite | Verified locally | 296 collected: 295 passed, 1 intentionally skipped (`GRIDMIND_RUN_PERF != 1`) in 222.67 s (final run after the solver-version parser fix). |
| CI workflow | **Verified for pushed HEAD** | Latest GitHub Actions run for `6ce5ee6` passed Windows and Ubuntu (maintainer-reported; metadata not independently re-fetched because the API returned 404 and `gh` is unavailable). The uncommitted review diff was not part of that run. |
| Qualification and performance gate | Verified locally | Committed baseline validates (`VALID`); historical baseline self-check was `PASS (48 checks, 0 failures)` at capture time; current independent smoke gate `PASS (6 checks, 0 failures)`; reproducibility-only smoke gate `PASS (3 checks, 0 failures)`. The old raw baseline measurement does not validate under the hardened schema. |
| Artifact schema and provenance | Verified locally | `benchmarks/schema.py` validates measurements, baselines, coverage, scalability and statistical artifacts; malformed hashes, missing provenance and invalid solver identity are rejected by tests. The baseline is explicitly `legacy: true`. |
| Multi-solver coverage | **Verified locally** | Fresh run `benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102/coverage.json` with GLPK 4.65, CBC 2.10.13 and `appsi_highs` 1.15.1: all three pass LP and MIP (`coverage_complete=true`). LP objective `20300.0` and MIP objective `400.0` for every solver; delta `0.0` against the GLPK reference and maximum independent constraint violation `0.0`, within `1e-6` relative tolerance. Schema validation 0 errors. |
| Scalability and peak memory | **Verified locally (limited)** | The original single-sample artifact has 13 checks and 0 failures; the fresh E9 run adds three independent batches and three repetitions per size. See `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`. This remains synthetic, local-only evidence and not a production-scale or portability claim. |
| Benchmark statistical power (E9) | **Verified locally** | `benchmarks/reliability.py` + `tests/test_reliability.py`; fresh artifact `benchmarks/runs/stage5e_e9_final_20261102/reliability.json` passed all functional, statistical, performance and scalability categories with 15 measured repetitions per performance workload and 9 samples per scalability size. |
| Numerical robustness | Verified locally | `tests/test_numerical_robustness.py` covers tolerance boundaries, scale extremes, ill-conditioned costs, zero demand, strict infeasibility, determinism and near-zero battery MIP. |
| Documentation and reproducibility | Completed locally | This report, `benchmarks/README.md`, `README.md`, `STAGE5E_AUDIT.md`, `STAGE5E_ACCEPTANCE_REPORT.md` and the scalability evidence report now match the measured state. `STAGE5D_READINESS.md` remains a historical Stage 5D record. |

## Reference environment

- Windows 10 (10.0.26300), Intel Core i7-8665U, 8 logical CPUs, 16,198 MiB RAM.
- CPython 3.11.17; pyomo 6.10.1; highspy 1.15.1; numpy 2.4.6; pandas 3.1.0;
  scipy 1.17.1; pytest 9.2.0; matplotlib 3.11.2.
- `appsi_highs` 1.15.1 available and MIP-capable. GLPK 4.65 and CBC 2.10.13 are
  optional external binaries; they are available for coverage when their
  directories are prepended to `PATH` (not installed as Python dependencies).
- Baseline recorded from a clean worktree at the commit above; coverage evidence
  was generated from the dirty review worktree.

## Reproduce

Run from the `GridMind` directory:

```powershell
python -m benchmarks.qualification measure `
  --out benchmarks/runs/stage5e_baseline_20261031c `
  --repetitions 3

python -m benchmarks.qualification build-baseline `
  --measurements benchmarks/runs/stage5e_baseline_20261031c/measurements.json `
  --out benchmarks/baseline.json

python -m benchmarks.qualification gate `
  --measurements benchmarks/runs/stage5e_baseline_20261031c/measurements.json `
  --baseline benchmarks/baseline.json
```

`benchmarks/runs/` is gitignored; the committed evidence is
`benchmarks/baseline.json` plus this report. `measure` refuses to overwrite an
existing run directory. `build-baseline` refuses to overwrite an existing
baseline.

## Baseline results

Wall and solver values are seconds; RSS values are MiB. Time limits use the
median of three repetitions. RSS limits use the observed maximum.

| Workload | Wall median | Wall max | Wall limit | Solver median | Solver limit | RSS max | RSS limit |
|---|---:|---:|---:|---:|---:|---:|---:|
| `benchmark` | 0.661 | 0.662 | 0.992 | 0.484 | 0.727 | 98.7 | 139.4 |
| `control_default` | 5.336 | 5.496 | 8.004 | 2.165 | 3.247 | 107.2 | 150.0 |
| `experiment_default` | 169.723 | 171.752 | 254.585 | 494.672 | 742.008 | 177.2 | 237.5 |
| `experiment_small` | 31.353 | 284.228 | 47.030 | 91.124 | 136.686 | 135.4 | 185.3 |
| `mp_benchmark` | 5.963 | 5.974 | 8.946 | 4.925 | 7.387 | 116.3 | 161.4 |
| `smoke` | 0.079 | 0.080 | 0.119 | 0.040 | 0.060 | 96.1 | 136.2 |
| `stochastic_20` | 15.535 | 15.748 | 23.303 | 14.270 | 21.406 | 133.8 | 183.3 |
| `stress_default` | 96.510 | 108.204 | 144.765 | 15.857 | 23.786 | 176.2 | 236.3 |

The `experiment_small` wall maximum of 284.228 s was a single scheduler/I/O
outlier; its median was 31.353 s. This is why time limits use the median rather
than the maximum. Every decision hash was deterministic across the three
repetitions.

## Gate evidence

Historical baseline evidence captured on 2026-10-31:

- Baseline self-check at capture time: `PASS (48 checks, 0 failures)`.
- Independent fresh smoke gate: `PASS (6 checks, 0 failures)` with the committed
  `smoke` decision hash.
- Full suite at baseline: `233 passed in 245.57 s`.

The retained raw baseline measurement file predates schema hardening. It no longer
validates under the current schema, so the 48-check self-check is a historical
record, not a currently reproducible command. The committed `baseline.json`
itself is valid under the current schema and remains the gate input.

Current local evidence on 2026-11-02:

```text
python -m benchmarks.qualification validate-baseline --baseline benchmarks/baseline.json
VALID

python -m benchmarks.qualification measure --out benchmarks/runs/audit_smoke_20261101 --workloads smoke --repetitions 2
smoke status=ok median_wall=0.034009s deterministic=True

python -m benchmarks.qualification gate --measurements benchmarks/runs/audit_smoke_20261101/measurements.json --baseline benchmarks/baseline.json --workloads smoke
PASS (6 checks, 0 failures)

python -m benchmarks.qualification gate --measurements benchmarks/runs/audit_smoke_20261101/measurements.json --baseline benchmarks/baseline.json --workloads smoke --reproducibility-only
PASS (3 checks, 0 failures)

python -m benchmarks.reliability run --out benchmarks/runs/stage5e_e9_final_20261102
acceptance passed=True categories={'functional': True, 'statistical': True, 'performance': True, 'scalability': True}

$env:PATH = "<glpk>\w64;<cbc>\bin;$env:PATH"
python -m benchmarks.solver_coverage run --out benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102
coverage_complete=True available=['glpk', 'cbc', 'appsi_highs'] unavailable=[]

python -m pytest -q
295 passed, 1 skipped in 222.67s
```

The strict gate checks decision-hash equality, deterministic repetitions, solver
name, median wall time, median solver time and maximum peak RSS. Timing fields
are removed and floats are rounded to six decimals before hashing.
`--reproducibility-only` keeps schema validation, determinism, decision-hash
equality and solver identity, but intentionally omits the machine-specific
wall, solver-time and RSS thresholds used by CI.

The local CI-equivalent smoke gate is:

```powershell
python -m benchmarks.qualification measure `
  --out benchmarks/runs/local_ci_smoke_<label> `
  --workloads smoke `
  --repetitions 2

python -m benchmarks.qualification gate `
  --measurements benchmarks/runs/local_ci_smoke_<label>/measurements.json `
  --baseline benchmarks/baseline.json `
  --workloads smoke `
  --reproducibility-only
```

This proves the workflow logic locally. Separately, the latest GitHub Actions run
for pushed commit `6ce5ee6` passed Windows and Ubuntu (maintainer-reported; see the
header limitation). The uncommitted review changes in this worktree were not part
of that run.

## E9 statistical reliability evidence

Fresh local artifact:
`benchmarks/runs/stage5e_e9_final_20261102/reliability.json` (gitignored raw
evidence; schema validation returns 0 errors). It was generated on 2026-11-02 from
branch `stage-5e-work-in-progress` at commit `6ce5ee6` with a dirty worktree,
because the reviewed fixes were not yet committed. Harness version `5E.4`.

Method:

- 3 independent batches x 5 measured repetitions + 1 warm-up per batch = 15
  measured samples per performance workload. Warm-ups are recorded separately and
  excluded from every statistic.
- 3 independent scalability batches x 3 repetitions per size = 9 samples per size;
  LP sizes 10/40/160 nodes and MIP sizes 8/24/48 periods.
- Median is the primary location estimate. The 95% median interval is the
  distribution-free binomial order-statistic interval (achieved coverage 0.964844,
  order statistic 4 at n=15; the minimum supportable sample is 6). The scalability
  exponent interval is a 10,000-resample percentile bootstrap with seed `20261101`.
- Outliers are flagged with the Tukey 1.5*IQR rule and retained. Both the median
  point estimate and the conservative interval upper bound must stay under the
  baseline limit.
- The harness rejects zero successful repetitions, missing warm-ups, incomplete
  batches, fewer than two successful scalability sizes, missing replicate counts
  and child-process timeouts instead of silently producing an empty distribution.

| Workload | n | Wall median (s) | Wall 95% CI (s) | Wall limit (s) | Solver 95% CI (s) | Solver limit (s) | RSS max (MiB) |
|---|---:|---:|---|---:|---|---:|---:|
| `smoke` | 15 | 0.080451 | 0.068893 - 0.102671 | 0.119 | 0.037623 - 0.049915 | 0.060 | 97.1 |
| `benchmark` | 15 | 0.728311 | 0.662253 - 0.917734 | 0.992 | 0.485764 - 0.655400 | 0.727 | 99.8 |
| `control_default` | 15 | 5.690944 | 4.921989 - 7.594785 | 8.004 | 2.008828 - 3.127132 | 3.247 | 108.2 |
| `mp_benchmark` | 15 | 6.679710 | 5.929645 - 7.769540 | 8.946 | 4.901121 - 6.443513 | 7.387 | 117.1 |

| Family | Sizes | Samples/size | Time exponent | Bootstrap 95% CI | RSS growth |
|---|---|---:|---:|---|---:|
| `dispatch_lp` | 10, 40, 160 | 9 | 0.087698 | 0.007223 - 0.133149 | 2.2 MiB (x1.037) |
| `schedule_mip` | 8, 24, 48 | 9 | 0.120748 | -0.025166 - 0.161263 | 1.4 MiB (x1.023) |

All four acceptance categories passed. The leak probe was not suspected across the
three batches (1.2-1.4 MiB growth, about 1.9-2.3%, against 64 MiB / 10% limits).
These are conservative local reference-machine results, not portability or
production-capacity claims.

## E5 multi-solver coverage evidence

Fresh local artifact:
`benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102/coverage.json` (gitignored
raw evidence; schema validation 0 errors). It was generated `2026-11-02T17:48:59Z`
to `2026-11-02T17:49:10Z` from branch `stage-5e-work-in-progress` at commit
`6ce5ee6` with a dirty worktree. Harness version `5E.4`. Artifact SHA-256:
`C68318F0B5250252E99132CFCD379F5281D338B800921B277208168B7F82E950`.

| Solver | Version | LP | MIP | MIP probe |
|---|---|---|---|---|
| GLPK | `GLPSOL: GLPK LP/MIP Solver, v4.65` | passed, 20300.0 | passed, 400.0 | integral optimum |
| CBC | `Version: 2.10.13` | passed, 20300.0 | passed, 400.0 | integral optimum |
| `appsi_highs` | `1.15.1` | passed, 20300.0 | passed, 400.0 | integral optimum |

All objective deltas are `0.0` against the GLPK reference and all maximum
independent constraint violations are `0.0`. LP and MIP objectives are compared
only within their own formulation; the harness's relative objective tolerance is
`1e-6`.

The binaries are not bundled with GridMind. Reproduction uses the official GLPK
4.65 and CBC 2.10.13 Windows archives, checksums and `PATH` commands in
`benchmarks/README.md`. The harness deliberately removes `GRIDMIND_SOLVER_PATH`
from child probes, so both executable directories must be on `PATH` even when that
variable points at a valid executable. A CBC version-capture defect was fixed in
`benchmarks/qualification.py` with a focused unit test.

## Threshold policy

- Wall limit = `ceil(median baseline wall x 1.50, 3 dp)`.
- Solver limit = `ceil(median baseline solver x 1.50, 3 dp)`.
- RSS limit = `ceil(max baseline RSS x 1.25 + 16 MiB, 1 dp)`.

The multipliers are committed in `benchmarks/baseline.json`, so every limit can
be recomputed from the recorded samples. Do not relax a limit to make a run pass;
create and review a new baseline only for an approved workload or environment
change.

## Limitations and remaining work

- The baseline was captured on a developer workstation with other applications
  running. It is conservative and must be recalibrated on an idle reference
  machine before being used as a release blocker.
- The raw baseline measurement artifact predates schema hardening and is legacy.
  `baseline.json` validates and drives current gates, but the original 48-check
  self-check cannot be rerun from that raw artifact without migration. The current
  reproducible evidence uses a fresh smoke measurement.
- CI (`.github/workflows/ci.yml`) validates the baseline structure, generates a
  smoke manifest on Windows and Linux, and gates that manifest against the
  committed smoke decision hash, determinism and solver identity with
  `--reproducibility-only`. It does not enforce timing/RSS gates on shared
  runners because those limits are machine-specific. The latest run for pushed
  `6ce5ee6` passed Windows and Ubuntu (maintainer-reported). The uncommitted review
  changes were not covered by that run.
- GLPK/CBC parity is verified locally on the coverage harness's tiny LP and MIP
  formulations. The external binaries are optional and must be installed
  separately; the evidence is not a production-scale or cross-machine
  performance claim.
- Local scalability evidence passes its guardrails. The original artifact used one
  repetition per size from a dirty worktree at commit `6a7e520`; the fresh E9
  artifact adds three independent batches and three repetitions per size from a
  dirty worktree at commit `6ce5ee6`. Both cover synthetic problems and remain
  local-only, not production-scale capacity or portability claims. See
  `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`.
- Artifact schema/provenance hardening, numerical robustness, documentation
  truth-up, multi-solver coverage (E5) and benchmark statistical power (E9) are
  complete locally. **Stage 5E is not accepted** because clean-provenance release
  evidence and a final acceptance review remain outstanding.
- The E9 artifact records `dirty: true` because the reviewed fixes were still
  uncommitted. Its schema is valid and its results are reproducible with the
  commands in `benchmarks/README.md`; it must be regenerated after the review
  changes are committed if a clean-provenance release artifact is required.
