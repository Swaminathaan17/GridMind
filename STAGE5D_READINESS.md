# Stage 5D operational-readiness report (accepted stage)

**Repo:** `C:\Users\DELL\Downloads\GridMind` / inner `GridMind`
**Stage 5D branch:** `stage-5d-operational-readiness`, based on `6a9c7e5`
**Environment:** Python 3.11.17, pyomo 6.10.1, highspy/appsi_highs MIP-capable
**Stage 5D baseline tests:** 233 passed (historical, verified 2026-10-31)
**Current Stage 5E tests:** 258 passed, 1 intentionally skipped (verified 2026-11-01)

**Stage status:** Stage 5D is the **last officially accepted stage**. Stage 5E remains
**in progress and not accepted**. This report is retained as the Stage 5D evidence record;
current Stage 5E evidence, limitations and blockers are in `STAGE5E_QUALIFICATION.md` and
`STAGE5E_AUDIT.md`.

## Status summary
- **Stage 5B:** Corrected and audited. Schema bumped to v3 with embedded realized demand/availability rows, problem hash, initial state. Executed trajectories independently validated; baseline re-dispatched at realized conditions. Fair comparison artifacts in `results/stage5b_audited/`. Historical `results/stage5b_control/` marked invalid and preserved unchanged.
- **INR localization:** Traceable USD->INR conversion added (`gridmind/currency.py`) with explicit configurable rate, provenance (source/date/direction), Indian formatting, USD-preserving conversion tree, traceability table, and tests. Non-overwriting artifacts in `results/stage5b_inr_final/` (USD canonical, INR derived).
- **Stage 5C:** Framework implemented and integrity-finalized (schema v2). Methods now store the exact executed trajectory; validation and comparison inclusion use that trace without re-solving. `solver_status` is separated from `policy_status`; solver-optimal but policy-infeasible executions are explicitly excluded. The benchmark covers six methods: deterministic open-loop, two committed full-horizon open-loop stochastic battery policies (risk-neutral/risk-aware), two causal receding-horizon stochastic hybrids (risk-neutral/risk-aware), and deterministic rolling-horizon. The open-loop stochastic policies commit the full-horizon battery schedule (matching the optimized terminal-SoC trajectory) and re-dispatch generation/flows/unmet at realized conditions; the hybrids re-plan causally from short windows, execute only current-period battery actions, and advertise `no_scenario_tree`. Historical v1 artifacts remain preserved.
- **Stage 5D:** Operational-readiness work completed and accepted. CLI metadata refreshed; MIP probe cache tracks executable identity, with regression tests for path and binary replacement. Accepted as the last official stage; do not treat Stage 5E work as accepted.
- **Stage 5E:** In progress and **not accepted**. Reproducible packaging/CI, pinned dependencies, committed environment/performance baseline, deterministic decision hashing, reproducibility-only CI gate, artifact-schema hardening, numerical robustness and local scalability/coverage evidence exist. GLPK/CBC parity remains blocked, remote CI is pending, and scalability evidence is local-only. See `STAGE5E_QUALIFICATION.md`, `STAGE5E_AUDIT.md` and `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md`.

## Evidence (key artifacts)
- `results/stage5b_audited/audit_summary.json` (verdict: PASS WITH CORRECTIONS)
- `results/stage5b_audited/control_lp.json`, `control_exclusive.json`, `control_stochastic.json` (schema v3, embedded realized rows)
- `results/stage5b_inr_final/control_lp_inr.json`, `currency_conversion.json`, `usd_inr_traceability.json`, `inr_summary.json`
- `results/stage5c_resilience/stress_benchmark.json`, `stress_benchmark_summary.json` (historical v1, preserved)
- `results/stage5c_batch2_smoke_20261030_103247/stress_benchmark.json`, `stress_benchmark_summary.json` (historical schema v2, six methods, preserved; pre-finalization execution defect)
- `results/stage5c_final_smoke_20261030_123721/stress_benchmark.json`, `stress_benchmark_summary.json` (post-finalization schema v2; all six methods included, validated, and execution-complete)
- `tests/test_control.py`, `tests/test_cli.py`, `tests/test_currency.py`, `tests/test_stress_corrections.py`, `tests/test_stress_benchmark.py` all passing
- `benchmarks/baseline.json`, `STAGE5E_QUALIFICATION.md` (Stage 5E baseline, thresholds and gate evidence)
- `STAGE5E_AUDIT.md` (requirement matrix and command evidence), `benchmarks/STAGE5E_SCALABILITY_EVIDENCE.md` (local scalability/memory report)

## Risks / gaps
- Stage 5C matrix is a small smoke run; full matrix not executed.
- Stage 5C open-loop stochastic policies now solve with a full-horizon battery commitment, so the executed trajectory matches the optimized terminal SoC. Any future policy that abandons its committed battery schedule is still detected as `policy_infeasible` and excluded from comparisons; that does not indicate a solver failure. The receding-horizon hybrids passed the smoke run, but a single small schedule is not comparative evidence.
- The stochastic hybrids are causal receding-horizon policies using the unchanged two-stage window model; they are not multistage scenario-tree optima and carry no global-optimality claim. The finite scenario sample also leaves Monte Carlo variance and foresight-model risk.
- `_merit_order_recourse` fallback has limitations (local recourse, no network export in that path) - reported via logs/curtailment; remains explicit.
- INR uses configurable default rate with provenance; no live FX feed.
- Stage 5E changes remain work in progress on `stage-5e-work-in-progress`; do not merge them to `main` or treat them as accepted until review is complete.
- Historical schema-v2 smoke run `results/stage5c_batch2_smoke_20261030_103247/`: open-loop, both stochastic hybrids, and deterministic rolling-horizon passed independent validation; both old committed-first-stage baselines were excluded for terminal-SoC violations. That artifact is preserved unchanged. The Stage 5C finalization fixes the open-loop stochastic execution contract; historical benchmark artifacts remain preserved.

## Recommended next steps (if proceeding)
1. Run a full Stage 5C experiment matrix with explicit seeds/schedules, multiple noise levels, and aggregate metrics that rank only eligible methods; keep artifacts in a new non-overwriting directory.
2. If currency reporting required broadly, wire INR metadata to CLI outputs with `--currency-inr` and `--inr-rate` flags (configurable, USD canonical).
3. Address any remaining artifact completeness items before freezing Stage 5B.
4. Treat Stage 5D as complete/accepted; do not fold remaining Stage 5E work into a Stage 5D completion claim.
5. Continue Stage 5E: observe remote CI, obtain GLPK/CBC for cross-solver parity, decide whether local scalability evidence is sufficient, and assess benchmark statistical power. Documentation truth-up and local evidence consolidation are complete but do not make Stage 5E accepted.
