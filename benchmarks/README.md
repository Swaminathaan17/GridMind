> **Final Stage 5E status (2026-11-03): ACCEPTED for the stated qualification
> scope.** Clean E9 evidence is at
> `benchmarks/runs/stage5e_e9_clean_20261103/reliability.json` (local/gitignored),
> SHA-256
> `20EBB33B32A4C2DAC8F11766FA2FA74991052E7D8C310C662270A1117D3FF201`.
> It was generated from clean worktree commit
> `10eded5c056c15b45c341a39c7f73b80fcdea2ed`; schema validation returned zero
> errors, `dirty=false`, and all four acceptance categories passed. GitHub Actions
> run `41623451166` passed Ubuntu and Windows for `10eded5`.
>
> The clean E9 environment had `appsi_highs` 1.15.1; GLPK/CBC were unavailable in
> that worktree. Three-solver LP/MIP parity is separately documented by the E5
> coverage artifact below. Raw E5/E9 JSON files remain local and gitignored.
> Synthetic workload and reference-machine limitations still apply. See
> [the final acceptance report](../STAGE5E_ACCEPTANCE_REPORT.md).

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

On shared CI runners, use `--reproducibility-only` together with
`--workloads smoke`. It still validates the artifacts, determinism, decision hash
and solver identity, but skips the wall-time, solver-time and RSS thresholds,
which are only meaningful on the reference machine. CI runs this gate against
the committed baseline on every push and pull request.

## Multi-solver coverage (GLPK/CBC)

`appsi_highs` is installed with the Python dependencies. GLPK and CBC are optional
external executables and are not bundled. The verified Windows setup uses the
official GLPK 4.65 and CBC 2.10.13 archives, installed outside the repository:

```powershell
$tools = Join-Path $env:TEMP "e5-solvers"
New-Item -ItemType Directory -Force $tools | Out-Null

curl.exe -L "https://sourceforge.net/projects/winglpk/files/latest/download" `
  -o "$tools\winglpk-4.65.zip"
curl.exe -L "https://github.com/coin-or/Cbc/releases/download/releases/2.10.13/Cbc-releases.2.10.13-w64-msvc17-md.zip" `
  -o "$tools\cbc-2.10.13.zip"

Get-FileHash -Algorithm SHA256 "$tools\winglpk-4.65.zip"
# expected A8C195B92682C3AC1F6DAE5F6CE2472C2478835F9873BE4A0C6771C37DB53C2D
Get-FileHash -Algorithm SHA256 "$tools\cbc-2.10.13.zip"
# expected B9702AD7501B4249A9721984CE3C6DC8FB9B6CFB995F42D493A5BD54B8F42A74

Expand-Archive -LiteralPath "$tools\winglpk-4.65.zip" `
  -DestinationPath "$tools\glpk" -Force
Expand-Archive -LiteralPath "$tools\cbc-2.10.13.zip" `
  -DestinationPath "$tools\cbc-msvc" -Force

$env:PATH = "$tools\glpk\glpk-4.65\w64;$tools\cbc-msvc\bin;$env:PATH"
python -m gridmind check-solver
python -m benchmarks.solver_coverage run `
  --out benchmarks/runs/solvers_<label>
```

The SourceForge "latest" URL currently resolves to `winglpk-4.65.zip`; verify the
SHA-256 before extracting. Use the CBC `w64-msvc17-md` archive on Windows (the
MinGW archive required a DLL that was absent on the reference machine). The
coverage harness removes `GRIDMIND_SOLVER_PATH` from child probes, so both
executable directories must be on `PATH`; setting `GRIDMIND_SOLVER_PATH` alone is
not sufficient for this command.

The current local artifact is
`benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102/coverage.json` (schema
errors 0, SHA-256
`C68318F0B5250252E99132CFCD379F5281D338B800921B277208168B7F82E950`). GLPK 4.65,
CBC 2.10.13 and `appsi_highs` 1.15.1 each passed both formulations: LP objective
`20300.0`, MIP objective `400.0`, zero objective delta and zero maximum constraint
violation, within the `1e-6` relative tolerance. These are the harness's tiny
probe formulations, not production-scale performance evidence. Use a new output
label for every run; the command refuses to overwrite existing evidence.

## Other Stage 5E evidence

These commands use the same artifact schemas and provenance capture:

```powershell
python -m benchmarks.scalability run `
  --out benchmarks/runs/scalability_<label>

python -m benchmarks.solver_coverage run `
  --out benchmarks/runs/solvers_<label>

python -m benchmarks.reliability run `
  --out benchmarks/runs/reliability_<label>
```

`scalability` measures LP dispatch and multi-period MIP growth, peak RSS and a
repeated-run leak probe. `reliability` collects independent batches of the
qualification workloads plus independent scalability batches, then applies the
robust statistics and acceptance gates from `stats.py` and `reliability.py`. The
committed summaries are `STAGE5E_SCALABILITY_EVIDENCE.md` and
`../STAGE5E_QUALIFICATION.md`; raw artifacts stay under the gitignored
`benchmarks/runs/` directory. `solver_coverage` probes every discovered solver;
the current local run verified GLPK, CBC and `appsi_highs` (see the next section).
The opt-in performance test runs with:

```powershell
$env:GRIDMIND_RUN_PERF = "1"
python -m pytest -q tests/test_scalability.py
```

E9 defaults are 3 batches x 5 measured repetitions + 1 warm-up for each
performance workload and 3 batches x 3 repetitions per scalability size. The
harness refuses to overwrite an existing directory and rejects zero successful
repetitions, missing warm-ups, incomplete batches, fewer than two successful
scalability sizes and child-process timeouts. The 95% median interval needs at
least 6 observations; below that it reports no interval and the corresponding
gate fails rather than inventing one. The full requirement matrix and local
command evidence are in `../STAGE5E_AUDIT.md` and
`../STAGE5E_ACCEPTANCE_REPORT.md`. E5 and E9 are complete locally; Stage 5E remains
**not accepted** pending clean-provenance release evidence and a final review. The
pushed `6ce5ee6` CI run passed Windows and Ubuntu (maintainer-reported);
uncommitted changes here are verified locally only.

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
- Qualification performance measurements were taken with `appsi_highs`; the
  GLPK/CBC coverage run is separate and does not recalibrate the baseline.
- The workloads use the synthetic sample network and horizon; they are not a
  production-scale capacity test.
- The default LP battery formulation can permit simultaneous charge/discharge;
  that existing modelling caveat is unchanged by this harness.
