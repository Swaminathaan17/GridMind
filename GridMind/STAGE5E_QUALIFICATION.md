# Stage 5E qualification status (in progress, not accepted)

**Branch:** `stage-5e-work-in-progress`
**Audit HEAD:** `683cf6b2727cf1953b6ea2c5441ab97093d2aa23` (documentation changes are uncommitted)
**Baseline commit:** `20eb70e786f048a8a6bdd5e2798314c920992020`
**Baseline captured:** 2026-10-31T05:20:09Z
**Tests:** 258 passed, 1 intentionally skipped (full suite, verified 2026-11-01)
**Status:** Stage 5E is **in progress and not accepted**. P0 (E1-E3) is complete; E4, E7 and the
documentation part of E8 are verified locally; E5 is blocked by missing GLPK/CBC; E6 is
**partially verified** with local-only evidence; remote CI has not been observed; E9 benchmark
statistical power remains open.

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

| Area | Status on 2026-11-01 | Evidence / limitation |
|---|---|---|
| Packaging and pins | Verified locally | `pyproject.toml`, `requirements-lock.txt`, constrained `requirements.txt`; the lock installs and the suite passes. |
| Full test suite | Verified locally | 259 collected: 258 passed, 1 intentionally skipped (`GRIDMIND_RUN_PERF != 1`). |
| CI workflow | Verified locally, remote pending | Windows/Linux jobs run the suite, solver discovery, baseline validation and a smoke reproducibility gate. No remote run was observed from this workstation. |
| Qualification and performance gate | Verified locally | Committed baseline validates (`VALID`); historical baseline self-check was `PASS (48 checks, 0 failures)` at capture time; current independent smoke gate `PASS (6 checks, 0 failures)`; reproducibility-only smoke gate `PASS (3 checks, 0 failures)`. The old raw baseline measurement does not validate under the hardened schema. |
| Artifact schema and provenance | Verified locally | `benchmarks/schema.py` validates measurements, baselines, coverage and scalability artifacts; malformed hashes, missing provenance and invalid solver identity are rejected by tests. The baseline is explicitly `legacy: true`. |
| Multi-solver coverage | **Blocked** | `appsi_highs` passes LP and MIP coverage (`coverage_complete=true`, LP objective 20,300, MIP objective 400). `glpk` and `cbc` are unavailable, so parity is **not verified**. |
| Scalability and peak memory | **Partially verified, local-only** | Local artifact has 13 checks and 0 failures; see `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`. The artifact is under gitignored `benchmarks/runs/`, uses one repetition per size and is not a production-scale or portability claim. |
| Numerical robustness | Verified locally | `tests/test_numerical_robustness.py` covers tolerance boundaries, scale extremes, ill-conditioned costs, zero demand, strict infeasibility, determinism and near-zero battery MIP. |
| Documentation and reproducibility | Completed locally | This report, `benchmarks/README.md`, `README.md`, `STAGE5D_READINESS.md`, `STAGE5E_AUDIT.md` and the scalability evidence report now match the measured state. |

## Reference environment

- Windows 10 (10.0.26300), Intel Core i7-8665U, 8 logical CPUs, 16,198 MiB RAM.
- CPython 3.11.17; pyomo 6.10.1; highspy 1.15.1; numpy 2.4.6; pandas 3.1.0;
  scipy 1.17.1; pytest 9.2.0; matplotlib 3.11.2.
- `appsi_highs` available and MIP-capable. `glpk` and `cbc` were unavailable.
- Baseline recorded from a clean worktree at the commit above.

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

Current local evidence on 2026-11-01:

```text
python -m benchmarks.qualification validate-baseline --baseline benchmarks/baseline.json
VALID

python -m benchmarks.qualification measure --out benchmarks/runs/audit_smoke_20261101 --workloads smoke --repetitions 2
smoke status=ok median_wall=0.034009s deterministic=True

python -m benchmarks.qualification gate --measurements benchmarks/runs/audit_smoke_20261101/measurements.json --baseline benchmarks/baseline.json --workloads smoke
PASS (6 checks, 0 failures)

python -m benchmarks.qualification gate --measurements benchmarks/runs/audit_smoke_20261101/measurements.json --baseline benchmarks/baseline.json --workloads smoke --reproducibility-only
PASS (3 checks, 0 failures)

python -m pytest -q
258 passed, 1 skipped
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

This proves the workflow logic locally. It is not evidence that the remote
Windows/Linux workflow jobs have passed.

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
  runners because those limits are machine-specific. **Remote CI execution is
  still pending.**
- Only `appsi_highs` was exercised. GLPK/CBC parity and multi-solver coverage
  remain **blocked** until those executables are supplied and coverage is rerun.
- Local scalability evidence passes its guardrails, but it covers synthetic
  problems, one repetition per size and a dirty worktree at commit
  `6a7e520`; it is not a production-scale capacity claim or a portability claim.
  See `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`.
- Artifact schema/provenance hardening, numerical robustness and documentation
  truth-up are complete locally. Benchmark statistical power (E9) and remote CI
  confirmation remain open, so **Stage 5E is not accepted**.
