# Stage 5E acceptance report

**Decision: ACCEPTED WITH DOCUMENTED LIMITATIONS**
**Branch:** `stage-5e-work-in-progress`
**Code/evidence commit:** `10eded5c056c15b45c341a39c7f73b80fcdea2ed`
**Review date:** 2026-11-03
**Last previously accepted stage:** Stage 5D

Stage 5E's E1-E9 acceptance evidence has been reviewed. The clean-provenance E9
artifact passes schema validation and all four acceptance categories. The latest
GitHub Actions run `41623451166` passed both Ubuntu and Windows jobs for commit
`10eded5`. E5 multi-solver parity was separately demonstrated with GLPK 4.65,
CBC 2.10.13 and `appsi_highs` 1.15.1 on the harness's LP/MIP probes.

This is acceptance of the Stage 5E reproducibility and qualification scope, not
a claim of production-scale capacity, cross-machine performance portability, or
new optimisation-model capability. Stage 5F is not started by this report.

## Clean E9 evidence

Artifact:
`benchmarks/runs/stage5e_e9_clean_20261103/reliability.json` (local,
gitignored; not committed).

- SHA-256: `20EBB33B32A4C2DAC8F11766FA2FA74991052E7D8C310C662270A1117D3FF201`
- Schema validation: 0 errors.
- Acceptance: `passed=True`; functional, statistical, performance and scalability
  categories all passed.
- Provenance commit: `10eded5c056c15b45c341a39c7f73b80fcdea2ed`.
- Provenance branch: detached clean worktree (`HEAD)); `dirty=false`.
- Harness: `5E.4`; generated 2026-11-03 11:45:37Z–11:50:40Z.
- Solver inventory: `appsi_highs` 1.15.1 available and MIP-capable; GLPK and CBC
  unavailable in this clean-worktree environment. Their separate E5 evidence
  remains the source for three-solver parity.

| Workload | n | Wall median (s) | 95% CI upper (s) |
|---|---:|---:|---:|
| `smoke` | 15 | 0.032682 | 0.033448 |
| `benchmark` | 15 | 0.292812 | 0.295886 |
| `control_default` | 15 | 1.926990 | 3.997402 |
| `mp_benchmark` | 15 | 2.759205 | 4.979652 |

| Family | Time exponent | Bootstrap 95% CI upper |
|---|---:|---:|
| `dispatch_lp` | 0.030475 | 0.069264 |
| `schedule_mip` | 0.080565 | 0.125312 |

Methodology remains as defined in the artifact: 3 independent batches x 5 measured
repetitions plus warm-up per performance workload; 3 batches x 3 repetitions per
scalability size; distribution-free median interval; 10,000-resample bootstrap
with seed `20261101`; Tukey outliers flagged and retained. These are synthetic
workloads on one reference machine.

## Requirement decision

| Requirement | Decision | Evidence |
|---|---|---|
| E1 Packaging, dependencies, CI | PASS | Pinned package/dependency setup; CI run `41623451166` passed Ubuntu and Windows for `10eded5`. |
| E2 Environment/provenance and baseline | PASS | Committed baseline validates; clean E9 provenance records commit, environment and input hashes. |
| E3 Deterministic hashing and performance gate | PASS | Qualification gate and reproducibility-only smoke gate; CI enforces hash/determinism/solver identity. |
| E4 Artifact schema/provenance hardening | PASS | Statistical artifact schema validation returned zero errors; schema rejection paths covered by tests. |
| E5 Multi-solver validation | PASS (harness scope) | GLPK 4.65, CBC 2.10.13 and HiGHS 1.15.1 passed LP/MIP probes; objectives matched and independent maximum constraint violation was zero. |
| E6 Scalability/peak memory | PASS (limited) | Synthetic local guardrails; not production-scale or portability evidence. |
| E7 Numerical robustness | PASS | Dedicated numerical robustness tests. |
| E8 Documentation/reproducibility | PASS | Qualification, audit, benchmark and acceptance documentation updated on this branch. |
| E9 Statistical power | PASS | Clean E9 artifact; all four acceptance categories passed; schema-valid and `dirty=false`. |

## E5 evidence

The E5 artifact is
`benchmarks/runs/stage5e_e5_coverage_glpk_cbc_20261102/coverage.json`
(local, gitignored), SHA-256
`C68318F0B5250252E99132CFCD379F5281D338B800921B277208168B7F82E950`.
GLPK 4.65, CBC 2.10.13 and `appsi_highs` 1.15.1 each passed LP and MIP
coverage. LP objective was 20300.0 and MIP objective 400.0 for each solver;
objective deltas and maximum independent constraint violations were 0.0.
The probes are deliberately small harness formulations, not production workloads.
GLPK/CBC are optional external binaries and must be installed separately.

## CI and regression evidence

GitHub Actions run: `41623451166`
(`https://github.com/Swaminathaan17/GridMind/actions/runs/41623451166`).
Both Ubuntu and Windows jobs passed for code commit `10eded5`.

A prior full local regression run reported 302 passed and 1 skipped. The skipped
test is the opt-in scalability performance benchmark when
`GRIDMIND_RUN_PERF != 1`; it is not counted as a pass. CI is the authoritative
cross-platform check for the tested commit.

## Limitations and release hygiene

- E9 and E5 raw JSON artifacts are gitignored and remain local. Their hashes and
  locations are recorded here; this report does not imply that the raw files are
  available to GitHub readers.
- Clean E9 was generated in a detached clean worktree at the exact tested code
  commit. Documentation commits made after this evidence do not change the code
  commit represented by the artifact.
- E5 solver parity covers small LP/MIP probes only.
- E6/E9 workloads are synthetic and local to the reference machine. No production
  capacity or cross-machine timing claim is made.
- GLPK and CBC were not available in the clean E9 worktree's solver inventory;
  their independently generated E5 coverage artifact is the evidence for those
  solvers.

## Reproduction

From the `GridMind` project directory:

```powershell
python -m benchmarks.reliability run --out benchmarks/runs/<new-label>
python -m benchmarks.solver_coverage run --out benchmarks/runs/<new-label>
python -m pytest -q
```

Use a new output directory for every run. The `benchmarks/runs/` directory is
gitignored. See `benchmarks/README.md` for solver installation and PATH details.

**Final decision:** Stage 5E is accepted for its stated reproducibility and
qualification scope, with the limitations above. Stage 5F is not included.
