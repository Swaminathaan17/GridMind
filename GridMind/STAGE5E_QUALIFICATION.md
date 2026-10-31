# Stage 5E qualification baseline (P0 complete)

**Branch:** `stage-5d-operational-readiness`
**Baseline commit:** `20eb70e786f048a8a6bdd5e2798314c920992020`
**Captured:** 2026-10-31T05:20:09Z
**Tests:** 233 passed (full suite, 245.57 s)
**Status:** P0 E1-E3 complete. This is not a claim that the full Stage 5E plan is complete.

## Scope

P0 establishes the minimum reproducible qualification foundation:

- **E1:** package metadata, pinned dependencies and CI on Windows and Linux.
- **E2:** environment/provenance manifest and committed performance baseline.
- **E3:** deterministic decision hashing and an absolute performance gate.

No optimisation model, solver default, dispatch rule or public CLI contract was
changed. Strict timing and memory limits are for the reference machine only.

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

- Baseline self-check: `PASS (48 checks, 0 failures)`.
- Independent fresh smoke gate:
  `PASS (6 checks, 0 failures)`, with the committed `smoke` decision hash.
- Full suite: `233 passed in 245.57 s`.

The gate checks decision-hash equality, deterministic repetitions, solver name,
median wall time, median solver time and maximum peak RSS. Timing fields are
removed and floats are rounded to six decimals before hashing.

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
- CI (`.github/workflows/ci.yml`) validates the baseline structure and generates
  a smoke manifest on Windows and Linux. It does not enforce timing/RSS gates on
  shared runners because those limits are machine-specific.
- Only `appsi_highs` was exercised. GLPK/CBC parity and multi-solver coverage
  remain Stage 5E E5 work.
- The workloads use the synthetic sample network and horizon; they are not a
  production-scale capacity test (Stage 5E E6).
- Artifact schema/provenance hardening, numerical robustness, benchmark
  statistical power and full documentation truth-up remain E4 and E7-E9.
