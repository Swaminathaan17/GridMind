# GridMind - least-cost dispatch optimisation (research prototype)

GridMind is a **backend-only** linear-programming engine (Python 3.11, Pyomo, and HiGHS via
`highspy`) that finds the least-cost generation and transmission schedule for a **simulated**
8-node network. GLPK and CBC are optional external solvers, not required for the default setup.

> **Scope disclaimer.** GridMind is a research / decision-support prototype working on a synthetic
> network. It does not predict, forecast or control any real electricity grid, and its numbers must
> not be used for real operational, market or safety decisions.

**Stage status:** **GridMind 1.0 qualification-stage engineering release (research prototype)**.
Stages 5A through 5F and the P0–P6 engineering qualification work are documented as completed.
This is not a production deployment certification: GridMind is designed for synthetic-network
research and decision support only. Consolidated phase evidence is recorded in
`docs/qualification/GRIDMIND_P0-P6_ACCEPTANCE_REPORT.md` and the final qualification report.

## Mathematical formulation

### Single-period economic dispatch (the original model)

Sets: nodes N, generators G (located at node n(g)), lines L (from a(l) to b(l)). Interval length h hours.

| Symbol | Meaning |
|---|---|
| `p[g] >= 0` | output of generator g (MW) |
| `f[l]` (free) | flow on line l, positive from a(l) to b(l) (MW) |
| `u[n] >= 0` | unmet demand at node n (MW) |
| `c[g]` | generator cost (USD/MWh); `lambda` = unmet-demand penalty (USD/MWh, configurable) |
| `P[g]`, `A[g]`, `o[g]` | capacity, renewable availability, online flag (0/1) |
| `F[l]`, `s[l]` | line capacity, in-service flag (0/1) |
| `D[n]` | demand at node n (MW) |

```
minimise   h * ( sum_g c[g] p[g]  +  lambda * sum_n u[n] )                    (USD for the interval)
subject to
  p[g] <= P[g] * o[g]                       for all g      generator limit / generator outage
  p[g] <= A[g]                              for renewable g renewable availability
  -F[l] * s[l] <= f[l] <= F[l] * s[l]       for all l      line capacity / line outage
  sum_{g at n} p[g] + sum_{l into n} f[l] - sum_{l out of n} f[l] + u[n] = D[n]   for all n   (power balance)
  0 <= u[n] <= D[n]                         for all n      (u[n] = 0 in --strict mode)
```

Outages are **constraints** (`o[g]=0`, `s[l]=0` force output/flow to 0); elements are never deleted
from the model. Because `u[n]` is a recourse variable the model is always feasible; in `--strict`
mode `u = 0`, so a network that cannot deliver all load is reported as **infeasible**.
The model is a lossless transport (network-flow) LP: no impedance, losses, voltage angles, unit
commitment or reserves. Ramping, storage and time-varying data live in the multi-period model below.

### Multi-period scheduling (intertemporal LP)

`gridmind/scheduling.py` extends the same transport model over a horizon of T periods of
`step_hours = dt` hours each. Sets add batteries B; every constant becomes a per-period parameter
from a `Horizon` (`gridmind/multiperiod.py`): demand `D[n,t]`, generation limit
`min(P[g], A[g,t]) * o[g,t]`, line limit `F[l] * s[l,t]`.

| Symbol | Meaning |
|---|---|
| `p[g,t] >= 0` | generator output (MW) |
| `f[l,t]` (free) | line flow (MW), positive from -> to |
| `u[n,t] >= 0` | unmet demand (MW) |
| `ch[b,t], dis[b,t] >= 0` | battery charge / discharge power (MW) |
| `e[b,t] >= 0` | state of charge AFTER period t (MWh) |

```
minimise   dt * ( sum_{g,t} c[g] p[g,t] + lambda * sum_{n,t} u[n,t]
                  + sum_{b,t} kappa[b] dis[b,t] )  +  eps * dt * sum_{b,t} (ch[b,t] + dis[b,t])
subject to
  0 <= p[g,t] <= min(P[g], A[g,t]) * o[g,t]        generator limits / outages
  -F[l]*s[l,t] <= f[l,t] <= F[l]*s[l,t]            line limits / outages
  p[g,t] - p[g,t-1] <= ru[g]     (and symmetric)   ramp limits, if declared
  p[g,0] - p_init[g] <= ru[g]    (and symmetric)   ramp from declared initial output
  gen + inflow - outflow + dis - ch + u = demand    nodal balance every (n, t)
  e[b,t] = e[b,t-1] + eta_c[b] * ch[b,t] * dt
                 - dis[b,t] * dt / eta_d[b]        SoC dynamics (e[b,-1] = initial SoC)
  min_soc[b] <= e[b,t] <= cap[b];  e[b,T-1] = final_soc[b] (when declared)
  0 <= ch[b,t] <= P_ch[b];  0 <= dis[b,t] <= P_dis[b]
  0 <= u[n,t] <= D[n,t]                             (u = 0 in --strict mode)
```

Conventions: power in MW, energy in MWh, cost in USD (objective totals), marginal cost in USD/MWh;
`e` is the **end-of-period** state and the initial SoC is the state *before* period 0.

**Simultaneity without MIP (partial mitigation, not a guarantee).** An LP may find it "optimal" to
charge and discharge a battery in the same period when that is free (no throughput cost, eta = 1) or
when it helps feasibility bookkeeping. Rather than adding binary variables, the objective carries a
tiny lexicographic tie-break `eps * dt * (ch + dis)` (default `eps = 1e-3` USD/MWh, field
`tiebreak_epsilon`). This *discourages* gratuitous cycles: whenever a simultaneous pair can be
reduced by a common `delta` without changing the SoC trajectory or the nodal balance, the reduction
saves `2*eps*delta*dt` and the LP prefers it.

It is **not** a universal guarantee. The "reduce the cycle" argument assumes the pair is
**SoC-neutral**, i.e. decreasing both `ch` and `dis` by `delta` leaves `e[b,t]` unchanged. The SoC
step is `e[t] = e[t-1] + eta_c * ch * dt - dis * dt / eta_d`, so a common reduction changes the state
by `delta * dt * (1/eta_d - eta_c)`. That is zero only when `eta_c * eta_d = 1`; with round-trip
losses (`eta_c * eta_d < 1`) it is positive, so the cycle shifts energy and may be **required** to
reach a binding terminal SoC, a capacity floor, or a `p[g,t] >= 0` balance. In that case the optimum
can contain simultaneous charge and discharge even with `eps > 0` and non-negative throughput costs,
and the independent validator's `simultaneous_charge_discharge` check will (correctly) flag such a
solution, so `ScheduleResult.ok` is false. A fully cycle-free formulation is a separate, larger
change (a signed net-battery-power variable, or complementarity constraints); it is out of scope here.
The operating cost reported separately excludes the tie-break; validation recomputes
`operating + tiebreak` and checks it against the solver's objective to 1e-6 relative tolerance.

The formulation stays a pure LP by default: outages and renewable availability are 0/1 *data*, not
variables.

**Genuine exclusivity (`battery_mode="exclusive"`).** For a hard guarantee, the schedule can be
built as a mixed-integer program. A binary `z[b,t]` is introduced per battery/period and the
following two constraints are added on top of the unchanged LP:

```
ch[b,t]  <= P_ch[b]  * (1 - z[b,t])
dis[b,t] <= P_dis[b] * z[b,t]          z[b,t] in {0, 1}
```

These are *tight* big-M rows (the big-M coefficient is the battery's own power limit), so
`z[b,t] = 0` allows charging only and `z[b,t] = 1` allows discharging only: a positive charge and a
positive discharge in the same period are mutually exclusive at any throughput cost, including zero.
The LP variables, balances, SoC dynamics and objective are identical. `--battery-mode exclusive`
(also available on `resilience` and `mp-benchmark`) selects this model.

MIP mode requires a MIP-capable solver. Availability is decided **empirically** at runtime: each
candidate in `MIP_SOLVER_CANDIDATES` (`glpk`, `cbc`, `appsi_highs`) is asked to solve a tiny binary
probe, and only a solver that returns an integral optimum is accepted. If none is available,
`solve_schedule(..., battery_mode="exclusive")` returns status `solver_unavailable`; it never falls
back to an LP-only result. The default `battery_mode="lp"` path is unchanged and still needs only an
LP solver.

The default is therefore unchanged: `lp` remains the documented, tie-break-mitigated formulation.

### Uncertainty, two-stage stochastic scheduling and CVaR

`gridmind/uncertainty.py` and `gridmind/stochastic.py` add an **optional** scenario-based
stochastic layer on top of the deterministic scheduler. Nothing here changes `solve_schedule`,
`build_schedule_model`, or the default API; the deterministic path is bit-for-bit unchanged.

**Synthetic scenarios (not forecasts).** `generate_scenarios(problem, UncertaintyConfig(...))`
builds a finite discrete distribution over `S` scenarios. Each scenario is a pair of demand and
renewable-availability trajectories. Generation is **deterministic given the seed**: one
`numpy.random.default_rng(seed)` drives every draw, so identical `(problem, config)` always yield a
byte-identical `ScenarioSet` (serialisable via `to_dict`/`from_dict`). Demand noise is AR(1);
renewable availability noise is clamped to `[0, capacity_mw]`. Every scenario is assigned to one of
four documented stress families with the configured probabilities:

| family | meaning |
|---|---|
| `ordinary` | demand and renewables fluctuate by their sigmas |
| `high_demand` | demand scaled by `high_demand_factor` |
| `low_renewable` | renewable availability scaled by `low_renewable_factor` |
| `compound` | both stresses applied together |

**These distributions are simple parametric shapes chosen for reproducibility and for exercising the
optimiser. They are NOT fitted to or validated against real grid data and carry no claim of realism.**

**Two-stage formulation.** Decisions split into two stages:

* *First stage (here-and-now, shared).* For the first `lead_periods` periods every scenario must
  commit to the **same battery** charge/discharge/SoC (and exclusivity binary when relevant). Storage
  is the quantity that must be committed before uncertain demand/availability is revealed.
* *Recourse (scenario, per realization).* Generator output, line flows, unserved energy, and the
  battery decisions of later periods are scenario-specific and adapt to the realized `xi_s`.

Non-anticipativity is enforced exactly by equality coupling of every first-stage decision across all
scenarios (`na_ch`/`na_dis`/`na_e`, plus `na_z` in exclusive mode). `lead_periods=0` degenerates to
*wait-and-see* (each scenario optimised independently); `lead_periods=periods` is a single
deterministic schedule. The default is `lead_periods=1`. Intermediate branching times are **not**
modelled (that would need a scenario tree).

**Objective (USD).** Per-scenario realized cost is exactly the deterministic objective:
`cost_s = dt*(sum_g c[g] p[s,g,t] + penalty*sum_n u[s,n,t] + sum_b kappa[b] dis[s,b,t]) + eps*dt*sum_b(ch+dis)`.
The risk-aware objective minimises

```
(1 - lambda) * E[cost]  +  lambda * CVaR_alpha(cost)
```

with `lambda = risk_weight` in `[0,1]` (`0` = risk-neutral expected cost, `1` = pure CVaR).
CVaR uses the Rockafellar-Uryasev representation
`CVaR_alpha = min_eta [ eta + 1/(1-alpha) * sum_s pi_s * v_s ]`, `v_s >= cost_s - eta`, `v_s >= 0`,
where `eta` is the VaR level. `cvar_alpha` is the CVaR confidence in `(0,1)`. Battery physics,
generator bounds, balance, line limits, ramp limits and terminal SoC are copied verbatim from the
deterministic model; exclusive mode adds the genuine binary exclusivity per scenario and requires a
verified MIP solver (no silent LP fallback).

**Out-of-sample evaluation (`gridmind/risk_eval.py`).** A *policy* is the first-stage commitment
produced by a solve. `compare_policies` fits three policies in-sample (deterministic mean, risk-neutral
`lambda=0`, risk-aware `lambda>0`) and evaluates them on a **held-out** scenario set. For each
evaluation scenario the policy's first-stage commitments are **fixed** and later periods re-optimised,
so no evaluation scenario can influence an earlier decision. All policies see the **same** holdout
scenarios and probabilities, so the comparison is apples-to-apples. Reported metrics: expected cost,
expected unmet energy, `P(unmet)`, worst-case cost, VaR, CVaR, p95, runtime, solver status and
per-scenario independent-validation results. No policy is called superior without a measured
difference; where policies coincide, they are reported as coinciding.

## Architecture

| File | Responsibility |
|---|---|
| `gridmind/network.py` | immutable network data classes, JSON loading, structural validation |
| `gridmind/multiperiod.py` | `Battery`, `Horizon`, `ScheduleProblem` (data model, JSON io, sample day builder) |
| `gridmind/solver.py` | solver discovery: env vars, candidate chain (GLPK, CBC, HiGHS), inventory for `check-solver` |
| `gridmind/optimizer.py` | single-period `build_model` (Pyomo) + `solve_dispatch` |
| `gridmind/scheduling.py` | multi-period `build_schedule_model` (Pyomo) + `solve_schedule` + `ScheduleResult` |
| `gridmind/validation.py` | solver-independent re-check of every constraint and the objective (single- and multi-period) |
| `gridmind/scenarios.py` | single-period scenarios, time-located horizon scenarios, seeded random generators |
| `gridmind/analysis.py` | binding constraints, overloaded lines, congestion analysis, sensitivity |
| `gridmind/resilience.py` | schedule metrics, storage ablation (with/without batteries), resilience suites |
| `gridmind/benchmark.py` | baselines, per-scenario evaluation, single- and multi-period batches, CSV export |
| `gridmind/uncertainty.py` | reproducible synthetic uncertainty scenarios (demand/renewable), probabilities, stress families |
| `gridmind/stochastic.py` | two-stage risk-aware stochastic scheduler (expected cost + CVaR, non-anticipativity) |
| `gridmind/risk_eval.py` | out-of-sample policy evaluation and deterministic / risk-neutral / risk-aware comparison |
| `gridmind/control.py` | rolling-horizon controller, realized re-dispatch, stored-trace execution records |
| `gridmind/stress.py` | causal stress environments, executed-trace capture/validation, method comparability |
| `gridmind/stress_bench.py` | Stage 5C benchmark orchestration and schema-v2 inclusion/exclusion artifacts |
| `gridmind/cli.py` | command-line interface |
| `data/sample_network.json` | documented sample network |
| `data/sample_horizon.json` | committed 24x1h synthetic day (profiles, two batteries, initial dispatch) |
| `tests/` | pytest suite (+ independent scipy reference LPs for both models) |

### Solvers

`gridmind/solver.py` tries, in order: an explicit choice (`GRIDMIND_SOLVER`, optionally with
`GRIDMIND_SOLVER_PATH` pointing at the executable), then `glpk`, `cbc`, `appsi_highs` (HiGHS via
`pip install highspy`). When either env var is set explicitly, there is **no silent fallback** -
an unusable choice is reported as `solver_unavailable` so tests and pipelines cannot mask a
misconfiguration. `python -m gridmind check-solver` prints the availability of every candidate.
No solver binary or path is hardcoded; nothing under `.venv/` belongs in the repository.

Failure policy: a result is only populated when the solver terminates **optimal**; otherwise
`status` is `infeasible`, `unbounded`, `infeasible_or_unbounded`, `solver_unavailable` or `error`,
`objective` is `None` and the dispatch/flow dicts are empty. Every optimal solution is re-validated
independently (tolerance 1e-5 MW, objective rel. 1e-6) and `result.ok` is false if validation fails.

## Sample network (data/sample_network.json)

Nodes: North Generation, West Renewables, Central Hub, East Hub, South Hub (no load) and three demand
nodes (North 120 MW, Central 165 MW, South 90 MW = 375 MW). Generators: gas-north 200 MW @84 (ramp 60/60),
hydro-central 65 MW avail @42, solar-west 72 MW avail @0, wind-west 48 MW avail @0, gas-east 170 MW @105
(ramp 50/50), gas-south 120 MW @128 (ramp 80/80) (USD/MWh). Eight lines with capacities 70-225 MW.
Default unmet penalty: 1000 USD/MWh, interval 1 h.

## Sample horizon (data/sample_horizon.json)

A committed, fully synthetic 24x1h day on the same network: diurnal demand (gaussian bumps at 08:00
and 19:00, clipped to [0.70, 1.35]x base), clear-sky solar lobe (06:00-18:00, peak 72 MW), sine
wind shape, mild hydro dip; thermal availability is constant. Two batteries: `battery-central`
(100 MWh, 40 MW both ways, eta 0.95, SoC 50 -> min 10 -> final 50) and `battery-south` (40 MWh,
20 MW, eta 0.90, SoC 10 -> min 5 -> final 10). Declared initial thermal outputs: gas-north 100,
gas-east 80, gas-south 40 MW (so the ramp-from-initial constraints bind). Load
`data/sample_horizon.json` with `schedule`/`resilience`/`mp-benchmark`, or build variants with
`gridmind.multiperiod.sample_schedule_problem()`.

## Windows setup (PowerShell)

```powershell
cd C:\Users\DELL\Downloads\GridMind\GridMind
py -3.11 -m venv ..\.venv            # venv sits next to the project folder
..\.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements-lock.txt  # exact pinned environment
pip install -e . --no-deps            # install GridMind without changing pins

# Optional: prefer GLPK or CBC instead (no path is hardcoded in the code):
$env:GRIDMIND_SOLVER_PATH = "C:\glpk\w64\glpsol.exe"   # the file, or its folder
# Persist for future sessions:  setx GRIDMIND_SOLVER_PATH "C:\glpk\w64\glpsol.exe"
# Optional: $env:GRIDMIND_SOLVER = "glpk"

# For multi-solver coverage, both solver directories must be on PATH:
# $env:PATH = "C:\glpk\w64;C:\cbc\bin;$env:PATH"
# See benchmarks/README.md for downloadable versions and checksums.

python -m gridmind check-solver     # shows every candidate: glpk, cbc, appsi_highs
pytest -q                           # 295 passed, 1 intentionally skipped
```

`tests/test_scalability.py` contains one opt-in performance test. It is skipped unless
`GRIDMIND_RUN_PERF=1`; set that variable and run `pytest -q tests/test_scalability.py` to execute it.
The skipped test is not counted as a pass.

cmd.exe equivalent: `set GRIDMIND_SOLVER_PATH=C:\glpk\w64\glpsol.exe`.

## Usage

```powershell
# single-period commands (unchanged)
python -m gridmind scenarios
python -m gridmind solve --scenario normal
python -m gridmind solve --scenario line_outage --json results\line_outage.json
python -m gridmind solve --scenario feeder_outage --strict          # infeasible, no invented numbers
python -m gridmind compare                                          # core scenarios vs baseline -> results\comparison.csv/.json
python -m gridmind sensitivity --kind demand
python -m gridmind sensitivity --kind generator_cost --target gas-north
python -m gridmind sensitivity --kind line_capacity --target line-north-central
python -m gridmind benchmark --n 100 --seed 42 --out results\benchmark.csv
python -m gridmind demo                                             # outage-changes-dispatch + unmet demand + infeasible

# multi-period commands
python -m gridmind schedule --scenario h-normal                     # 24h dispatch, costs, SoC trajectories
python -m gridmind schedule --scenario h-compound --strict          # stress day with unmet demand forbidden
python -m gridmind schedule --horizon data\sample_horizon.json --json results\schedule.json
python -m gridmind resilience                                       # all core stress days, with vs without storage
python -m gridmind resilience --scenario h-compound --out results
python -m gridmind mp-benchmark --n 20 --seed 42 --out results\mp_benchmark.csv
python -m gridmind uncertainty                                      # show the default synthetic scenario config

# uncertainty / risk commands (Stage 4)
python -m gridmind stochastic --scenarios 40 --seed 42              # two-stage risk-aware, out-of-sample
python -m gridmind stochastic --risk-weight 1.0 --cvar-alpha 0.95 --lead-periods 1
python -m gridmind stochastic --scenarios 20 --seed 7 --json results\stochastic.json
python -m gridmind stochastic --battery-mode exclusive              # MIP exclusivity inside the risk model
```

```powershell
# Stage 5A: reproducible comparative benchmark (deterministic / risk-neutral / risk-aware)
python -m gridmind experiment                                       # 5 seeds x 40 scenarios -> results\stage5a_benchmark\
python -m gridmind experiment --seed 42 --seed 43 --scenarios 60    # custom seeds/scale
python -m gridmind experiment --battery-mode exclusive              # MIP battery exclusivity
```

```powershell
# Stage 5B: rolling-horizon control vs a fixed open-loop schedule (receding horizon)
python -m gridmind control                                          # look-ahead 6, deterministic, LP
python -m gridmind control --lookahead 8 --seed 20260201            # longer look-ahead, fixed seed
python -m gridmind control --mode stochastic --scenarios 20 --risk-weight 0.5
python -m gridmind control --battery-mode exclusive                 # MIP exclusivity per replan
python -m gridmind control --failure-policy abort                   # stop on the first failed replan
python -m gridmind control --out results\stage5b_audited            # machine-readable JSON record
```

`scenarios` lists both families (single-period ids like `peak_demand`, horizon ids prefixed `h-`).
`schedule` prints per-period demand/cost/unmet, battery SoC trajectories and the independent
validation verdict; `--json` writes the full `ScheduleResult`.
`resilience` solves each stress scenario twice (storage fleet on/off) and reports cost savings and
unserved-energy reduction attributable to storage; it writes `resilience.csv`/`resilience.json`.
`mp-benchmark` batches the core horizon scenarios plus `--n` seeded random ones into a CSV with
status, costs, unserved energy, validation verdict, myopic and proportional references, plus a
`.summary.json` aggregate. Same seed + same horizon file => identical scenarios and objectives.
`--horizon`, `--network` and `--penalty` override the data files on all three commands.

`stochastic` generates reproducible synthetic scenarios, splits them into in-sample/holdout sets,
fits deterministic / risk-neutral (`--risk-weight 0`) / risk-aware (`--risk-weight >0`) policies, and
evaluates all three on the **same** holdout scenarios, printing expected cost, CVaR, worst case,
`P(unmet)` and per-policy validation. `--json` writes a complete experiment record (seed, uncertainty
config, risk config, battery mode, solver, statuses, objective components, metrics and validation).
Scenario generation is deterministic given `--seed`, so a run is reproducible; the report is model
output, not a real-world measurement.

### Stage 5A - reproducible comparative benchmark

`experiment` runs the deterministic / risk-neutral / risk-aware comparison across **multiple seeds**
and writes a self-describing set of artefacts to a dedicated directory (default
`results/stage5a_benchmark/`; existing results are never touched):

| File | Contents |
| --- | --- |
| `experiment_config.json` | full protocol: seeds, scenario count/weights, sigmas, `risk_weight`, `cvar_alpha`, `lead_periods`, `battery_mode` |
| `runs/seed_<seed>.json` | one complete run per seed: scenario split ids, fits, per-policy evaluations |
| `per_scenario_metrics.csv` | one row per `(seed, policy, holdout scenario)`: realized cost, unmet energy, probability |
| `aggregate_summary.json` | cross-seed aggregates and paired comparisons |
| `README.txt` | metrics, units, assumptions and limitations for the run |

Protocol (why the numbers are fair):

* Each seed generates one scenario set, split once into in-sample/holdout. All three policies are
  fitted on the **same** in-sample scenarios and evaluated on the **same** holdout scenarios.
* Evaluation is non-clairvoyant: the policy's first-stage commitments are **fixed**; only recourse
  (later periods) adapts to the realized scenario. No evaluation scenario influences an earlier decision.
* Probabilities are used wherever the scenario set defines weights.

Metrics (per policy, over seeds: count / mean / median / std / p95):

* `expected_cost_usd`, `expected_unmet_energy_mwh`, `probability_of_unmet` (realized, out-of-sample).
* Risk: `var_usd` (alpha-quantile), `cvar_usd` (mean of worst `1-alpha` tail), `p95_cost_usd`,
  `worst_case_cost_usd`. CVaR confidence is `--cvar-alpha` (default 0.95); the loss is the realized
  scenario cost in USD.
* `runtime_seconds`, `feasible_run_rate`. Failures are **counted and listed** (by seed), never folded
  into a mean.

Paired comparisons (`risk_aware - risk_neutral`, `risk_neutral - deterministic`,
`risk_aware - deterministic`): per-scenario realized-cost differences are pooled across seeds and we
report mean, sd, min, max and Cohen's `dz = mean(diff)/sd(diff)`. `dz` is an **effect size**, not a
significance test - synthetic, dependent scenarios give no valid p-value, so none is reported.

Reproducibility: same seeds + same horizons => identical scenario ids, costs and aggregate metrics
(wall-clock timings differ between runs and are excluded from equality checks).

JSON output (`--json`) contains status, termination condition, objective, dispatch, flows, per-node
served/unmet demand, raw solver duals (when the solver provides them), the validation report and the baseline comparison.
`benchmark.csv` columns include `scenario_id, status, objective_usd, unmet_mw, runtime_s, baseline_cost_usd,
saving_usd, congestion_cost_usd, validation_passed`. Same seed => same scenarios and objectives.

### Stage 5B - rolling-horizon control

`control` runs a **receding-horizon controller** against a fixed, full-horizon **open-loop** schedule
evaluated under **identical realized conditions**, so the two are directly comparable.

Protocol (why the comparison is fair and non-clairvoyant):

* A reproducible synthetic forecast-error model (`ForecastErrorModel`) fixes, from one seed, three
  independent streams: the **a-priori base forecast** (a noisy draw around the horizon data), the
  **realization** (base forecast + a fixed error draw), and a **fresh forecast per control step** `k`
  (a k-specific draw around the base forecast for periods `>= k`, observed realized values for `< k`).
  The forecast at step `k` therefore carries **no information about the realized future** - a property
  the test suite verifies by corrupting the realized future and asserting the period-`k` decision is
  unchanged.
* At each period `k` the controller solves a look-ahead window `[k, k+lookahead)`, executes **only the
  first period**, then advances the carried physical state (battery SoC and last generator dispatch, so
  ramp coupling is continuous) before replanning. The terminal SoC condition is imposed only on the
  final window; intermediate windows release it.
* Modes: `deterministic` (single LP/MIP window solve) and `stochastic` (two-stage risk model per
  window). In stochastic mode only the first-stage **battery** action is here-and-now; generator
  dispatch and unserved energy are wait-and-see and are realized by a **network-aware re-dispatch** at
  the true conditions - a scenario mean is never executed as-is.
* `--battery-mode exclusive` adds MIP charge/discharge exclusivity to every window solve.
* Failure policy: if a window fails to solve, `hold` (default) executes an explicit, always-feasible
  frozen-battery least-cost recourse and logs the step as `recovered`; `abort` stops and records the
  remaining periods as not executed. A failed replan **never fabricates a plan or objective**.

Reported metrics (both controller and baseline, same realization): realized operating cost, unserved
energy, battery throughput, realized CVaR at `--cvar-confidence`, per-step and mean/max solve time,
executed periods and failed/recovered/fell-back replan counts, power/energy curtailment, and the
controller-minus-baseline differences. The controller record also includes an **independent**
`verify_executed_trajectory` report over the actual executed arrays at realized inputs.

The reported CVaR is explicitly a **per-period realized operating-cost CVaR with uniform weights**
(`realized_cvar_basis = "per_period_realized_operating_cost_uniform_weights"`), not the scenario CVaR
used inside the stochastic planner. `realized_var_usd`, `realized_cvar_alpha` and the full list of
period observations are recorded so the metric can be reproduced.

**Fair baseline.** The open-loop schedule is still optimised on the time-0 forecast and its battery
action is held fixed, but generator dispatch is re-optimised at realized conditions exactly as the
controller's is. The record keeps the old as-planned cost/validation under `planned_*` (for
transparency) but the comparison uses `realized_*`. Executing the as-planned generator dispatch
against different realized demand is generally **not physically feasible**, so its cost must not be
used as a counterfactual.

Honest reading of the measured results (reference developer environment, HiGHS, `data/sample_horizon.json`,
look-ahead 6, seed 20260201, sigmas 0.06/0.12). Both sides are feasible at realized inputs:

| Run | controller cost | baseline cost | Δ cost | Δ unmet | Δ CVaR |
| --- | --- | --- | --- | --- | --- |
| deterministic, LP | 599,839.25 | 599,069.98 | **+769.27** | 0.00 | +1,828.48 |
| deterministic, exclusive | 600,773.28 | 600,028.26 | +745.02 | 0.00 | +1,382.35 |
| stochastic, LP (20 scenarios, rw 0.5) | 600,162.11 | 599,069.98 | +1,092.12 | 0.00 | +4,618.72 |

The controller is **not better** than the open-loop baseline here: it is slightly worse, and the
risk-averse stochastic mode is more expensive. This is the expected outcome - the open-loop plan is
solved with the full horizon and perfect terminal condition, while the controller sees only a short
forecast window and re-plans under error. The controller **reproduces the open-loop optimum exactly**
when the look-ahead spans the whole horizon and there is no forecast error (certainty equivalence),
and is strictly worse when the look-ahead is too short to prepare for a demand ramp - both are
asserted in the test suite. No superiority claim is made anywhere.

Historical Stage 5B artefacts under `results/stage5b_control/` predate realized re-dispatch: their
`executed` arrays equal the planned arrays and their baseline cost is the infeasible as-planned
dispatch. They are **not** valid comparisons and are never overwritten. The audited rerun lives under
`results/stage5b_audited/`.

Artefacts (`--out <dir>`; default writes nothing unless `--out`/`--json` is given): `<name>.json` with
the full comparison record (both `controller` and `baseline` sub-records plus the differences),
schema-versioned (`CONTROL_SCHEMA_VERSION = 2`). Stage 5A outputs under `results/stage5a_benchmark/`
are never touched.

### Stage 5C - stress benchmark and executable-policy integrity

`gridmind/stress_bench.py` compares six methods on **identical realized stress conditions**:
deterministic open-loop, two committed full-horizon open-loop stochastic battery policies
(risk-neutral/risk-aware), two causal receding-horizon stochastic hybrids (risk-neutral/risk-aware), and
the deterministic rolling-horizon controller. The benchmark never re-solves a method to manufacture a
validation result: each method stores the per-period trajectory it actually executed, and inclusion is
decided from that stored trace.

* Every result separates `solver_status` (what the optimiser returned) from `policy_status` (what the
  executed policy achieved). The legacy `status` field equals `policy_status`, so a solver-optimal but
  physically infeasible execution is never reported as `optimal`.
* Each stored trace row contains realized demand/availability, initial and final SoC, executed
  dispatch/flows/charge/discharge/unmet, committed battery actions, and the realized operating cost.
  `validation_status` is `passed`, `failed`, `incomplete`, `missing_trace`, or `skipped`; only
  solver-optimal results with a complete, independently validated trace are included in comparisons.
* Terminal SoC, trace/reported metric consistency, period coverage, and battery-mode feasibility are
  checked independently. Solver-optimal but policy-infeasible results are listed in `exclusions` with an
  explicit reason and are never dropped silently.
* `battery_mode="lp"` permits simultaneous charge/discharge; `exclusive` rejects it. Invalid SoC or
  power transitions are always explicit errors, never silently clamped.
* `stochastic_risk_neutral` and `stochastic_risk_aware` are **committed full-horizon open-loop battery
  policies**. The stochastic model is solved with its battery decisions shared across all scenarios for
  every period (`lead_periods = periods` inside the policy), so the executed battery trajectory is
  exactly the trajectory whose terminal SoC was optimized. Generator dispatch, flows, and unserved
  energy are still re-dispatched at realized conditions each period. The caller's `lead_periods` is
  recorded as `requested_lead_periods`; the policy config records
  `committed_battery_periods = periods` and `commitment_horizon = "full"`. This is a committed
  open-loop policy, not adaptive recourse: there is no scenario tree and no re-planning.
* `stochastic_rolling_risk_neutral` and `stochastic_rolling_risk_aware` are **causal receding-horizon
  stochastic hybrids**. At each step they sample scenario paths only from forecasts available at that
  step, solve the unchanged two-stage model over a short window, execute only the current-period battery
  commitment, re-dispatch generators/flows/unserved energy at realized conditions, and advance the exact
  SoC. The next step re-plans from the new state. This is a causal commitment policy, not a full
  multistage scenario-tree optimum; its policy label records
  `no_scenario_tree`.
* Benchmark artifacts are schema-versioned (`STRESS_BENCH_SCHEMA_VERSION = 2`) and include problem,
  realized-conditions, forecast, and configuration hashes. Historical Stage 5C v1 artifacts under
  `results/stage5c_corrected_smoke_20261028_14/` and `results/stage5c_resilience/` are preserved
  unchanged and must not be mixed with v2 comparisons. The historical schema-v2 smoke run
  `results/stage5c_batch2_smoke_20261030_103247/` is also preserved unchanged; it recorded the earlier
  zeroed-post-lead execution defect, so its two stochastic baselines were excluded for terminal-SoC
  violations. After the Stage 5C finalization, the open-loop stochastic policies execute the full
  committed battery schedule and pass executed-trace validation; the post-fix smoke run
  `results/stage5c_final_smoke_20261030_123721/` includes and validates all six methods.

## Baseline and units

All costs are USD for one interval (`interval_hours`, default 1 h) on the single-period path, or for
the whole horizon on the multi-period path (`step_hours * periods`), identical to the objective
definition (generation + unmet penalty + battery throughput). Reference points, all **ignoring the
network** (they are references, not deployable schedules):
* **Proportional baseline** - cost-blind: every available generator runs at the same fraction of its limit so supply = demand.
  `saving = baseline - optimised`. Because the baseline ignores transmission limits it may occasionally be *cheaper*
  than a network-feasible optimum (negative saving), especially under outages.
* **Copper-plate bound** - merit-order dispatch with unlimited transmission, a lower bound on the optimum.
  `congestion cost = optimised - copper-plate bound`.
* **Myopic copper-plate reference** (multi-period) - the same merit-order computed independently in
  each period, ignoring ramps, storage and the network. Not a bound: storage can make the real
  optimum cheaper than this no-storage reference; the per-scenario `gap_vs_myopic_usd` column shows
  the value the optimiser adds over solving each period in isolation.
* **Proportional horizon reference** (multi-period) - the cost-blind proportional rule applied per
  period; likewise a reference, not a bound.

## Verification status (read this)

* 296 tests collected on 2026-11-02: **295 passed, 1 intentionally skipped**. The skip is the
  opt-in scalability performance test (`GRIDMIND_RUN_PERF != 1`), not a pass. The suite covers
  solver-free tests (data model, scenarios, determinism, validator, baselines, failure handling,
  scenario generation, model structure, CVaR/VaR metrics, experiment statistics, control forecast
  model) plus solver tests for both models, the stochastic layer, the comparative benchmark, the
  rolling-horizon controller, Stage 5C trace/inclusion integrity, Stage 5E qualification gates,
  artifact schemas, E9 robust statistics and reliability gates, solver coverage and numerical
  robustness. Solver tests skip with a reason when no solver is found.
* Every multi-period solve is cross-checked two ways: the independent validator
  (`gridmind/validation.py`, no Pyomo) re-derives all constraints and the objective from the data,
  and `tests/reference_lp_mp.py` rebuilds the whole LP with `scipy.optimize.linprog` (HiGHS) and
  compares objectives, dispatch, unmet, charge/discharge and SoC to 1e-4/1e-6. Every scenario of a
  stochastic solve is independently validated the same way.
* Hand-computed optima pin the formulation: battery arbitrage (800 USD at eta=1; 1,080 USD at
  eta=0.8), ramp-constrained generation (19,200 USD vs 7,500 USD unconstrained).
* Risk metrics are pinned to hand-computed discrete values (e.g. costs `[0,10,20,100]` with uniform
  weights: VaR(0.5)=10, CVaR(0.5)=60; VaR(0.75)=20, CVaR(0.75)=100), and tested properties include
  "pure-CVaR in-sample CVaR <= risk-neutral CVaR", "wait-and-see expected cost <= committed expected
  cost", non-anticipativity equalities across scenarios, and infeasible scenarios returning no numbers.
* The reference environment: CPython 3.11.17, pyomo 6.10.1, highspy 1.15.1 (`appsi_highs`), on
  Windows 10; **295 passed, 1 intentionally skipped** on 2026-11-02. Optional external GLPK 4.65
  and CBC 2.10.13 also passed LP and MIP coverage with identical objectives and zero constraint
  violation; see `benchmarks/README.md` for reproducible installation. Same seed => same scenarios
  and objectives across runs and the exercised solver. E9 reliability evidence is generated by
  `python -m benchmarks.reliability run`; see `STAGE5E_QUALIFICATION.md`.
* Stage 5A benchmark integrity is tested: two runs of the same seeds produce byte-identical
  `experiment_config.json` and `per_scenario_metrics.csv` (only wall-clock timings differ); all three
  policies are asserted to evaluate the **same** holdout scenario ids; missing/failed policies are
  reported (with seed) rather than dropped silently. Descriptive stats, the p95 interpolation and
  Cohen's `dz` are pinned to hand-computed values.
* Stage 5B controller integrity is tested: no future-information leakage (corrupting the realized
  future leaves the period-0 decision unchanged), forecast != realization under noise and equality at
  zero sigma, SoC and dispatch carry-forward continuity, terminal-SoC handling, both failure policies
  (no fabricated plan/objective on failure), LP and MIP exclusivity, stochastic execution with no
  fabricated unserved energy, the certainty-equivalence result (full look-ahead, no noise => matches
  the open-loop optimum), myopic-worse-than-open-loop, schema/round-trip, and CLI artefact
  reproducibility. An independent `verify_executed_trajectory` check now runs over every controller
  and baseline execution; regression tests cover executed-trajectory feasibility, charge curtailment
  before unmet demand, mode-aware LP/exclusive feasibility, and the re-dispatched (not as-planned)
  baseline comparison.
* Stage 5C integrity is tested for stored-trace validation without a second solve, exact SoC/throughput
  continuity, incomplete-trace exclusion precedence, zero-eligible-method truthfulness, custom
  problem/config provenance, selected LP/MIP battery-mode propagation, and the causal stochastic hybrid:
  changing only future realized conditions leaves its current-period executed actions unchanged, and
  executed battery actions never exceed the committed first-stage actions.



## Stage 5F & GridMind 1.0 Capabilities

### Security-Constrained Preventive Scheduling (SCPS)
GridMind 1.0 provides Security-Constrained Preventive Scheduling (`gridmind/security.py`) over multi-period intertemporal networks subject to N-1 contingency sets:
- **Preventive + Corrective Co-Optimization**: Solves a monolithic mathematical program coupling preventive dispatch trajectories across periods with post-contingency recourse corrective trajectories for generator and transmission line outages.
- **Ramping Limits & Recourse**: Surviving generators respect downward and upward corrective ramp limits relative to preventive dispatch. Failed components are forced to zero output/flow.
- **Independent Verification Engine**: Results are strictly evaluated by `gridmind/security_validation.py` (`verify_scps_result`), recomputing feasibility across all contingency trajectories independently from the solver.

### Unified Solver Resilience & Options (`gridmind/solver.py`)
GridMind provides structured solver management via `SolverOptions`:
- Explicit parameterization of `time_limit_seconds`, `mip_gap`, and `threads`.
- Safe translation to Pyomo solver options and direct backend bindings (`highs_options` for `appsi_highs`).
- HiGHS global thread scheduler isolation and safe multi-instance reset.
- Normalized termination status mapping across solvers: `optimal`, `infeasible`, `unbounded`, `time_limit`, `interrupted`, and `solver_error`.

### Execution Provenance & Safe Serialization (`gridmind/provenance.py`)
- Automatic generation of `ExecutionMetadata` on every solve (`SolveResult`, `ScheduleResult`, `SCPSResult`).
- Deterministic SHA-256 fingerprinting of input network/horizon problems.
- Full tracking of UTC execution timestamp, GridMind release version, solver backend version, and applied options.
- Hardened `to_dict()` and `json.dumps()` serialization: non-finite floats (`NaN`, `+Inf`, `-Inf`) serialize safely to explicit standard strings without runtime errors or silent corruption.

## Assumptions and limitations

Lossless transport model; no impedance, losses, voltage angles, unit commitment or reserves;
constant marginal costs; renewable availability is a fixed forecast; outages are deterministic
inputs (0/1 data, no N-1 probability); storage is a simple two-port energy balance with fixed
efficiencies (no degradation dynamics beyond a linear throughput cost, no self-discharge);
ramp limits are symmetric MW-per-period when declared; random scenarios are simple uniform
perturbations; dual values are reported in the solver's raw sign convention; the baselines are
simple references, not industry dispatch rules. Simulated data only - not a real grid, not for
operational use.

Stage 4 uncertainty/risk additions inherit these limits and add their own:
* Scenarios are **synthetic parametric draws** (Gaussian noise, AR(1) demand, four stress families),
  reproducible from a seed. They are **not** forecasts and are **not** validated against real data.
* Only **battery storage** is a first-stage (here-and-now) decision; generator dispatch, flows and
  unserved energy are always recourse. Intermediate branching times are not modelled (no scenario
  tree); the model is a two-stage program, not a multi-stage one.
* The two-stage split uses uncertainty in demand and renewable availability only; outage probability,
  correlated failures and cost uncertainty are out of scope. CVaR is computed over the finite scenario
  set (a discrete, in-sample estimate), and out-of-sample metrics depend on the held-out sample size.
* Risk-aware and deterministic policies may coincide on problems where the first-stage decision has
  little leverage; the tool reports measured differences and never asserts a policy is superior
  without evidence. These are model outputs, not real-world performance claims.

Stage 5A benchmark caveats:
* The comparison is between **policies**, not solver objective values: a lower in-sample CVaR
  objective does **not** imply lower out-of-sample CVaR. Only the out-of-sample metrics are compared.
* `lead_periods` defaults to 1, so on a short horizon the three policies often coincide; near-zero
  paired differences are reported as such and no superiority is claimed.
* Aggregates draw on the finite synthetic scenario sample; `p95`/worst-case figures depend on the
  holdout size (`n_scenarios * holdout_fraction`), and paired `dz` assumes independent pairs, which
  synthetic AR(1) scenarios only approximately satisfy.
