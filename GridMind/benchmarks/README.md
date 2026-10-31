# Stage 5E qualification harness

This directory is separate from the `gridmind` runtime package. It runs existing
public workloads, records environment/provenance and enforces the committed
performance and reproducibility baseline. It does not change models, solver
defaults or dispatch behaviour.

## Reproduce the baseline

Run these commands from the `GridMind` directory (the one containing
`pyproject.toml`). Use a new label for every run; `measure` refuses to overwrite
an existing directory.

```powershell
python -m benchmarks.qualification measure `
  --out benchmarks/runs/stage5e_20261031 `
  --repetitions 3

python -m benchmarks.qualification build-baseline `
  --measurements benchmarks/runs/stage5e_20261031/measurements.json `
  --out benchmarks/baseline.json
```

`benchmarks/runs/` is gitignored. Only the small `baseline.json` manifest and
this documentation are committed; raw per-run CSVs/JSON remain local evidence.
The captured reference environment, measured limits and gate evidence are
recorded in `../STAGE5E_QUALIFICATION.md`.

## Gate a new run

```powershell
python -m benchmarks.qualification measure `
  --out benchmarks/runs/stage5e_gate_20261101 `
  --repetitions 3

python -m benchmarks.qualification gate `
  --measurements benchmarks/runs/stage5e_gate_20261101/measurements.json `
  --baseline benchmarks/baseline.json
```

Exit code 0 means every checked workload stayed within its committed limits and
produced the same decision-relevant hash. Exit code 1 reports each failed check.
The gate checks:

- decision hash equality after removing timing fields and rounding floats to six
  decimals;
- median wall time against the committed wall limit;
- median solver time against the committed solver limit where available;
- maximum peak resident set size against the committed RSS limit;
- the same solver name as the reference baseline.

Use `--workloads smoke` for a fast subset. Do not relax a limit to make a run
pass; regenerate and review the baseline only when a deliberate workload change
has been approved.

## Threshold policy

Time limits use the median of the committed baseline repetitions so a single
scheduler or I/O outlier cannot inflate the gate. Memory uses the observed
maximum.

- wall limit = `ceil(median observed wall x 1.50, 3 dp)`;
- solver limit = `ceil(median observed solver x 1.50, 3 dp)`;
- RSS limit = `ceil(max observed RSS x 1.25 + 16 MiB, 1 dp)`.

The multipliers are fixed in the baseline file so every threshold can be
recomputed from the measured samples. They provide headroom over observed
run-to-run noise; they are not portability claims. The baseline is a reference
machine (`Windows 10`, Python 3.11.17, `appsi_highs`), so strict gates belong on
that machine or an equivalent runner, not on shared CI hardware.

## Limitations

- Wall time includes Python/Pyomo model construction, not only solver time.
- Measurements were taken with `appsi_highs`; GLPK and CBC were unavailable.
- The workloads use the synthetic sample network and horizon; they are not a
  production-scale capacity test.
- The default LP battery formulation can permit simultaneous charge/discharge;
  that existing modelling caveat is unchanged by this harness.
