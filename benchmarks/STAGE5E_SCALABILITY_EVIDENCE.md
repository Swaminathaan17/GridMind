# Stage 5E scalability and memory evidence (local, not accepted)

**Status:** local evidential run only. This report does **not** accept Stage 5E and
does not establish production-scale capacity, asymptotic performance or
cross-machine portability. Stage 5D remains the last officially accepted stage.

## Source artifact

- Artifact: `benchmarks/runs/stage5e_e6_scalability_20261031/scalability.json`
- Kind: `gridmind_scalability_measurements`, schema version 1
- Generated: `2026-10-31T08:24:21Z`
- Harness version: `5E.4`
- Recorded branch/commit: `stage-5d-operational-readiness` /
  `6a7e5200471a2f19f8c1e71fe1ed44541741f37b`, `dirty: true`
- Environment hash: `ec5174de08aaf9d7a5d25b1f3ca6d3c77b5d79a247e8c7e881db5343dbda8237`

The raw artifact is under the gitignored `benchmarks/runs/` directory. Because the
worktree was dirty, this is development evidence, not a release artifact. This
report is the committed summary; the raw measurements remain local.

## Environment

- Windows 10 `10.0.26300`, AMD64, 8 logical CPUs, 16,198.2 MiB RAM.
- CPython 3.11.17; pyomo 6.10.1; highspy 1.15.1.
- `appsi_highs` available and MIP-capable; `glpk` and `cbc` unavailable.
- Input hashes: `data/sample_network.json`
  `9be1c5aa032572197426d5aadd10743cb8e600efe8a6c64962eb961f38a820c3`;
  `data/sample_horizon.json`
  `5d3d6db44f287bf062f7381444288ae03d1bc023f50e24aac6c6a2e603ad6f66`.

## Configuration and guardrails

| Setting | Value |
|---|---|
| LP dispatch sizes (nodes) | 10, 40, 160 |
| MIP schedule sizes (periods) | 8, 24, 48 |
| Leak probe | MIP size 8, 5 repetitions |
| Maximum time exponent | 3.0 |
| Maximum RSS growth | 512.0 MiB |
| Maximum RSS growth factor | 4.0 |
| Leak tolerance | 64.0 MiB or 10% |

Each size ran once in a fresh child process. The child process measures wall time,
solver time and peak RSS, then writes a worker JSON. The leak probe runs the same
MIP workload five times in one process and samples RSS after each run.

## Results

### LP dispatch

| Nodes | Objective (USD) | Wall (s) | Solver (s) | Peak RSS (MiB) |
|---:|---:|---:|---:|---:|
| 10 | 450 | 1.464224 | 0.024632 | 60.2 |
| 40 | 1,950 | 1.557242 | 0.057213 | 60.8 |
| 160 | 7,950 | 1.675705 | 0.156115 | 62.1 |

- Observed wall-time exponent: `0.04865797868473699` (limit `3.0`).
- RSS growth: `1.9 MiB`, factor `1.0315614617940199` (limits `512.0 MiB` / `4.0`).

### MIP schedule (battery-exclusive)

| Periods | Objective (USD) | Wall (s) | Solver (s) | Peak RSS (MiB) |
|---:|---:|---:|---:|---:|
| 8 | 800 | 1.778843 | 0.068234 | 62.0 |
| 24 | 2,400 | 1.673116 | 0.107386 | 62.9 |
| 48 | 4,800 | 1.712071 | 0.180637 | 63.5 |

- Observed wall-time exponent: `-0.021352980016152313` (limit `3.0`).
- RSS growth: `1.5 MiB`, factor `1.0241935483870968` (limits `512.0 MiB` / `4.0`).

### Repeated-run leak probe

| Repetition | RSS (MiB) |
|---:|---:|
| 1 | 62.2 |
| 2 | 62.6 |
| 3 | 63.1 |
| 4 | 63.4 |
| 5 | 63.5 |

- RSS growth: `1.3 MiB`, fraction `0.020900321543408314`.
- Limits: `64.0 MiB` / `10%`; `leak_suspected: false`.

### Overall

`13` checks, `0` failures, `passed: true`. All six worker solves reported
`status=ok`, `solver_status=optimal` and `validation_passed=true`.

## Honest interpretation

The 13 guardrail checks pass, all solves are optimal and independently validated,
and observed RSS growth is small in these synthetic runs. However, the wall-time
exponent is **not** a reliable asymptotic slope: every child process pays Pyomo
import and startup overhead, so wall time is roughly 1.4-1.8 s even for the
smallest cases. The MIP exponent is slightly negative for that reason. Solver time,
which excludes much of that startup cost, rises with size (`0.0246 -> 0.1561 s` for
LP and `0.0682 -> 0.1806 s` for MIP), but one sample per size is not enough for a
statistical claim.

The leak probe's first wall sample (`1.64 s`) also includes startup; later samples
are about `0.08 s`. The RSS trend, not the wall trend, is the meaningful part of
that probe.

**Therefore this run establishes only that these specific local workloads completed,
validated and stayed inside the configured guardrails. It does not establish
production capacity, a memory ceiling, portability to CI runners, solver parity or
a performance guarantee outside the reference environment.**

## Reproduction

From the `GridMind` directory, using a new output label:

```powershell
python -m benchmarks.scalability run `
  --out benchmarks/runs/scalability_<label>
```

Equivalent explicit defaults:

```powershell
python -m benchmarks.scalability run `
  --out benchmarks/runs/scalability_<label> `
  --lp-sizes 10 40 160 `
  --mip-sizes 8 24 48 `
  --leak-repetitions 5 `
  --leak-size 8
```

The command prints each size and the final check summary and writes
`scalability.json` under the output directory. The harness refuses to overwrite an
existing directory.

## Independent local rerun

On 2026-11-01 the auditor reran the default command into
`benchmarks/runs/audit_scalability_20261101/`. It also passed `13/13` checks, with
LP/MIP wall exponents `-0.0238` / `0.0800`, RSS growth `1.7` / `1.2 MiB` and leak
growth `1.2 MiB` (`suspected: false`). The differing exponents confirm that the
cold-start wall-time term is noise-dominated; this rerun is a local reproducibility
check, not additional capacity evidence.

## Fresh E9 multi-batch statistical evidence (2026-11-02)

The original run above used one sample per size. The E9 reliability harness now
collects independent batches and reports uncertainty:

- Artifact: `benchmarks/runs/stage5e_e9_final_20261102/reliability.json` (gitignored)
- Kind: `gridmind_statistical_matrix`, schema version 1, harness `5E.4`
- Generated: `2026-11-02T15:16:28Z`
- Recorded branch/commit: `stage-5e-work-in-progress` /
  `6ce5ee601bd7f7cf36371429c6d7d193075f04ee`, `dirty: true`
- Configuration: 3 scalability batches x 3 repetitions per size; leak probe 3
  batches x 5 repetitions. Median interval: distribution-free binomial order
  statistic (achieved coverage 0.964844 at n=15). Exponent interval: 10,000
  bootstrap resamples, seed `20261101`.

| Family | Sizes | Samples/size | Time exponent | Bootstrap 95% CI | RSS growth |
|---|---|---:|---:|---|---:|
| `dispatch_lp` | 10, 40, 160 | 9 | 0.087698 | 0.007223 - 0.133149 | 2.2 MiB (x1.037) |
| `schedule_mip` | 8, 24, 48 | 9 | 0.120748 | -0.025166 - 0.161263 | 1.4 MiB (x1.023) |

All functional, statistical, performance and scalability categories passed. The
leak probe was not suspected in any batch (1.2-1.4 MiB, about 1.9-2.3%, against
64 MiB / 10% limits). The exponents remain dominated by Pyomo/process startup and
are bounded over the observed sizes only; this still does not establish asymptotic
complexity, production capacity or cross-machine portability. The artifact records
`dirty: true` because the reviewed fixes were uncommitted; regenerate it after
commit if clean provenance is required.

Reproduction:

```powershell
python -m benchmarks.reliability run `
  --out benchmarks/runs/stage5e_e9_final_<label>
```

## Validation performed

```text
python -c "from benchmarks import schema; import json; ..."
schema errors: 0
summary: {'checks': 13, 'failures': 0, 'passed': True}

python -c "from benchmarks import schema; import json; ... reliability.json"
schema errors: 0
acceptance: {'functional': True, 'statistical': True, 'performance': True, 'scalability': True}
```
