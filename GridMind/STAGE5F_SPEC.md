# Stage 5F — Security-Constrained Preventive Scheduling
## Mathematical Specification (Milestone 5F.1) — Revision 2

**Branch:** `stage-5f-work-in-progress`  
**Originally prepared:** 2026-11-03  
**Revised:** 2026-11-03 (Revision 2 — post-review corrections)  
**Status:** REVISED DRAFT — awaiting review before 5F.2 begins  
**Depends on:** Stage 5E (accepted 2026-11-03)

---

> [!IMPORTANT]
> This document is the **sole deliverable** of Milestone 5F.1. No production
> source code is modified in this milestone. All decisions documented here are
> subject to review before 5F.2 begins.

### Revision 2 change log

| Issue | Change |
|-------|--------|
| R1 | Corrective ramp constraints now explicitly **exempt the tripped generator**. A generator trip is instantaneous; ramp limits apply only to surviving generators. |
| R2 | Case 2 hand-calculation corrected. Original erroneously claimed N-1 secure. Correct result: **not N-1 secure**, 20 MW unmet at B per period. Full workings provided. |
| R3 | Battery SoC interpretation clarified. The independent-initial-SoC design is a **planning-stage joint optimisation**, not a model of an outage occurring mid-trajectory. The distinction is stated explicitly. |
| R4 | Objective function section rewritten. Equal contingency weighting decision stated. Tiebreak term added to corrective cost for API consistency. Corrective generation cost inclusion rationale strengthened. Double-counting analysis added. |
| R5 | All equations verified against `network.py`, `multiperiod.py`, `scheduling.py`, and `validation.py`. No invented parameters. |
| R6 | All four hand-calculated test cases corrected with independent arithmetic. Cases 3 and 4 redesigned for numerical clarity. |
| R7 | Baseline test section replaced with **actual recorded results** from this machine. Fabricated placeholder text removed. |
| R8 | New Section 13 added: explicit statement distinguishing transportation-flow security assessment from real electrical grid N-1 security. |

---

## 1. Problem definition

GridMind's existing multi-period scheduler (Stage 5B–5E) finds a single
least-cost schedule assuming all generators and lines remain in service. An
N-1 security criterion requires that the system remain operable — meaning
all demand can be met — after the loss of any single component.

**Security-Constrained Preventive Scheduling (SCPS)** answers the question:

> *Can we find one common base operating schedule (the "preventive" plan)
> such that, for every single-component outage, corrective redispatch of the
> remaining equipment can restore full supply?*

This is a two-level program:

- **Preventive (base) stage** — choose a common schedule valid under normal
  conditions, optimising cost subject to all normal operating constraints.
- **Contingency (corrective) stage** — for each N-1 scenario, evaluate
  whether corrective redispatch of surviving equipment can serve all demand.

The preventive schedule is identical across all contingency evaluations.
Each contingency is evaluated independently given that shared preventive plan.

The model is restricted to the **lossless transportation-flow abstraction**
already present in GridMind. No voltage angles, reactance, susceptance, or
AC/DC power-flow physics are added or implied. See Section 13 for the
implications of this restriction.

---

## 2. Notation and index sets

| Symbol | Meaning |
|--------|---------|
| $\mathcal{N}$ | Set of nodes |
| $\mathcal{G}$ | Set of generators |
| $\mathcal{L}$ | Set of transmission lines |
| $\mathcal{B}$ | Set of batteries |
| $\mathcal{T}$ | Set of time periods $\{0, 1, \ldots, T-1\}$ |
| $\mathcal{K}$ | Set of N-1 contingencies |
| $\mathcal{K}_G \subseteq \mathcal{K}$ | Generator-outage contingencies |
| $\mathcal{K}_L \subseteq \mathcal{K}$ | Line-outage contingencies |
| $dt$ | Duration of each period (hours, `Horizon.step_hours`) |
| $T$ | Number of periods (`Horizon.periods`) |
| $n(g)$ | Node at which generator $g$ is located (`Generator.node`) |
| $n(b)$ | Node at which battery $b$ is located (`Battery.node`) |
| $a(\ell), b(\ell)$ | From-node and to-node of line $\ell$ (`Line.from_node`, `Line.to_node`) |
| $g_k$ | The generator that fails in contingency $k \in \mathcal{K}_G$ |
| $\ell_k$ | The line that fails in contingency $k \in \mathcal{K}_L$ |
| $\mathcal{G}^{RU}$ | Generators with a declared `ramp_up_mw` |
| $\mathcal{G}^{RD}$ | Generators with a declared `ramp_down_mw` |

Each contingency $k \in \mathcal{K}$ is identified by $(type_k, id_k)$ where
$type_k \in \{generator, line\}$ and $id_k$ is the failed component's id.

---

## 3. Parameters

### 3.1 Network parameters (inherited from `Network` and `Horizon`)

All parameters are taken directly from existing data classes. No new
parameters are invented.

| Symbol | Source | Meaning |
|--------|--------|---------|
| $D_{n,t}$ | `Horizon.demand_mw[t][n]` | Demand at node $n$ in period $t$ (MW) |
| $\bar{P}_{g,t}$ | `Horizon.generation_limit(g, t)` | Generation upper bound: $\min(\text{capacity}_g, \text{availability}_{g,t}) \cdot \mathbb{1}[\text{online}_{g,t}]$ (MW) |
| $F_{\ell,t}$ | `Horizon.line_limit(l, t)` | Line thermal capacity: $\text{capacity}_\ell \cdot \mathbb{1}[\text{in\_service}_{\ell,t}]$ (MW) |
| $c_g$ | `Generator.cost_per_mwh` | Generator marginal cost (USD/MWh) |
| $\lambda$ | `Network.unmet_penalty_per_mwh` | Unmet-demand penalty (USD/MWh) |
| $\bar{P}^{ch}_b$ | `Battery.max_charge_mw` | Battery charge power limit (MW) |
| $\bar{P}^{dis}_b$ | `Battery.max_discharge_mw` | Battery discharge power limit (MW) |
| $\underline{E}_b$ | `Battery.min_soc_mwh` | Battery minimum state of charge (MWh) |
| $\bar{E}_b$ | `Battery.capacity_mwh` | Battery maximum state of charge (MWh) |
| $E^0_b$ | `Battery.initial_soc_mwh` | Initial SoC before period 0 (MWh) |
| $\eta^{ch}_b$ | `Battery.eta_charge` | Charge efficiency |
| $\eta^{dis}_b$ | `Battery.eta_discharge` | Discharge efficiency |
| $\kappa_b$ | `Battery.throughput_cost_per_mwh` | Throughput cost per MWh discharged (USD/MWh) |
| $\varepsilon$ | `ScheduleProblem.tiebreak_epsilon` | Tiebreak epsilon (USD/MWh) |
| $E^{final}_b$ | `Battery.final_soc_mwh` | Terminal SoC target, when declared (MWh) |
| $p^{init}_g$ | `ScheduleProblem.initial_dispatch_mw[g]` | Initial generator dispatch before period 0 (MW), when declared |

### 3.2 Corrective ramp parameters

GridMind's `Generator` already carries `ramp_up_mw` and `ramp_down_mw`. Stage 5F
reuses these for the corrective stage with no new parameters:

| Symbol | Source | Meaning |
|--------|--------|---------|
| $RU_g$ | `Generator.ramp_up_mw` | Max upward ramp per period (MW). `None` means unconstrained. |
| $RD_g$ | `Generator.ramp_down_mw` | Max downward ramp per period (MW). `None` means unconstrained. |

**Assumption A1 — Corrective ramp scale.** The same per-period ramp bounds
that govern intertemporal transitions in the preventive model are applied to
bound the corrective output deviation from preventive dispatch **within the
same time period**. If a generator has no declared ramp limit, it is
unconstrained in the corrective stage (consistent with the preventive model).

This is a conservative planning assumption: it treats one scheduling period as
the available response window. If a generator's ramp limit is declared as
60 MW/period, the corrective output may differ from the preventive output by at
most 60 MW (up or down). This assumption is confirmed for Stage 5F.

### 3.3 Contingency parameters

For each contingency $k$:

| Symbol | Meaning |
|--------|---------|
| $\delta^G_{g,k}$ | 1 if generator $g$ is the failed component in contingency $k$ (i.e., $g = g_k$), else 0 |
| $\delta^L_{\ell,k}$ | 1 if line $\ell$ is the failed component in contingency $k$ (i.e., $\ell = \ell_k$), else 0 |

The modified limits under contingency $k$ are:

$$\bar{P}^k_{g,t} = \bar{P}_{g,t} \cdot (1 - \delta^G_{g,k})$$
$$F^k_{\ell,t} = F_{\ell,t} \cdot (1 - \delta^L_{\ell,k})$$

These reduce to zero for the failed component and are unchanged for all others.

---

## 4. Decision variables

### 4.1 Preventive-stage variables (shared across all contingencies)

| Variable | Domain | Meaning |
|----------|--------|---------|
| $p^0_{g,t}$ | $\mathbb{R}_{\geq 0}$ | Generator $g$ dispatch in period $t$, preventive (MW) |
| $f^0_{\ell,t}$ | $\mathbb{R}$ | Line $\ell$ flow in period $t$, preventive (positive = from→to, MW) |
| $u^0_{n,t}$ | $\mathbb{R}_{\geq 0}$ | Unmet demand at node $n$ in period $t$, preventive (MW) |
| $ch^0_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ charge power in period $t$, preventive (MW) |
| $dis^0_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ discharge power in period $t$, preventive (MW) |
| $e^0_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ state of charge after period $t$, preventive (MWh) |
| $z^0_{b,t}$ | $\{0,1\}$ | Battery exclusivity binary, preventive (exclusive mode only) |

### 4.2 Contingency-stage variables (per contingency $k \in \mathcal{K}$)

| Variable | Domain | Meaning |
|----------|--------|---------|
| $p^k_{g,t}$ | $\mathbb{R}_{\geq 0}$ | Generator $g$ corrective dispatch in period $t$ under contingency $k$ (MW) |
| $f^k_{\ell,t}$ | $\mathbb{R}$ | Line $\ell$ corrective flow in period $t$ under contingency $k$ (MW) |
| $u^k_{n,t}$ | $\mathbb{R}_{\geq 0}$ | Corrective unmet demand at node $n$ in period $t$ under contingency $k$ (MW) |
| $ch^k_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ corrective charge power in period $t$ under contingency $k$ (MW) |
| $dis^k_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ corrective discharge power in period $t$ under contingency $k$ (MW) |
| $e^k_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ corrective SoC after period $t$ under contingency $k$ (MWh) |
| $z^k_{b,t}$ | $\{0,1\}$ | Battery exclusivity binary, contingency $k$ (exclusive mode only) |

The preventive variables ($p^0$, $f^0$, $u^0$, $ch^0$, $dis^0$, $e^0$) appear
in both the preventive constraints and the corrective ramp constraints, enforcing
a single shared preventive schedule.

---

## 5. Constraints

### 5.1 Preventive-stage constraints

These constraints are identical to the existing multi-period model in
`scheduling.py` / `multiperiod.py`. No preventive constraint changes.

**Generator limits:**
$$0 \leq p^0_{g,t} \leq \bar{P}_{g,t} \quad \forall g \in \mathcal{G},\; t \in \mathcal{T}$$

**Line capacity:**
$$-F_{\ell,t} \leq f^0_{\ell,t} \leq F_{\ell,t} \quad \forall \ell \in \mathcal{L},\; t \in \mathcal{T}$$

**Preventive ramp limits** (between consecutive periods; from initial dispatch when declared):
$$p^0_{g,t} - p^0_{g,t-1} \leq RU_g \quad \forall g \in \mathcal{G}^{RU},\; t \in \mathcal{T} \setminus \{0\}$$
$$p^0_{g,t-1} - p^0_{g,t} \leq RD_g \quad \forall g \in \mathcal{G}^{RD},\; t \in \mathcal{T} \setminus \{0\}$$
$$p^0_{g,0} - p^{init}_g \leq RU_g, \quad p^{init}_g - p^0_{g,0} \leq RD_g \quad \text{when } p^{init}_g \text{ is declared}$$

**Battery charge/discharge power limits:**
$$0 \leq ch^0_{b,t} \leq \bar{P}^{ch}_b, \quad 0 \leq dis^0_{b,t} \leq \bar{P}^{dis}_b \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Battery SoC dynamics:**
$$e^0_{b,t} = e^0_{b,t-1} + \eta^{ch}_b \cdot ch^0_{b,t} \cdot dt - \frac{dis^0_{b,t} \cdot dt}{\eta^{dis}_b} \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

with $e^0_{b,-1} \triangleq E^0_b$.

**Battery SoC bounds:**
$$\underline{E}_b \leq e^0_{b,t} \leq \bar{E}_b \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Battery terminal SoC** (when `Battery.final_soc_mwh` is not `None`):
$$e^0_{b,T-1} = E^{final}_b$$

**Battery exclusivity** (exclusive mode only; `battery_mode="exclusive"`):
$$ch^0_{b,t} \leq \bar{P}^{ch}_b \cdot (1 - z^0_{b,t}), \quad dis^0_{b,t} \leq \bar{P}^{dis}_b \cdot z^0_{b,t} \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Unmet demand bounds (preventive):**
$$0 \leq u^0_{n,t} \leq D_{n,t} \quad \forall n \in \mathcal{N},\; t \in \mathcal{T}$$

In strict preventive mode: $u^0_{n,t} = 0$.

**Nodal power balance (preventive):**

$$\sum_{g:\, n(g)=n} p^0_{g,t} + \sum_{\ell:\, b(\ell)=n} f^0_{\ell,t} - \sum_{\ell:\, a(\ell)=n} f^0_{\ell,t} + \sum_{b:\, n(b)=n} \bigl(dis^0_{b,t} - ch^0_{b,t}\bigr) + u^0_{n,t} = D_{n,t}$$

$$\forall n \in \mathcal{N},\; t \in \mathcal{T}$$

---

### 5.2 Contingency-stage constraints

For each contingency $k \in \mathcal{K}$, using the modified limits
$\bar{P}^k_{g,t}$ and $F^k_{\ell,t}$ from Section 3.3.

**Generator limits under contingency:**
$$0 \leq p^k_{g,t} \leq \bar{P}^k_{g,t} \quad \forall g \in \mathcal{G},\; t \in \mathcal{T}$$

For the failed generator $g_k$ (if $k \in \mathcal{K}_G$): $\bar{P}^k_{g_k,t} = 0$, so
$p^k_{g_k,t} = 0$ is enforced by the upper bound alone.

**Line capacity under contingency:**
$$-F^k_{\ell,t} \leq f^k_{\ell,t} \leq F^k_{\ell,t} \quad \forall \ell \in \mathcal{L},\; t \in \mathcal{T}$$

**Corrective ramp limits (R1 — tripped generator is exempt):**

A generator trip is an instantaneous event; it cannot be constrained by a
ramp rate. Only **surviving generators** are subject to corrective ramp limits.

For generator-outage contingencies ($k \in \mathcal{K}_G$):
$$p^k_{g,t} - p^0_{g,t} \leq RU_g \quad \forall g \in \mathcal{G}^{RU},\; g \neq g_k,\; t \in \mathcal{T}$$
$$p^0_{g,t} - p^k_{g,t} \leq RD_g \quad \forall g \in \mathcal{G}^{RD},\; g \neq g_k,\; t \in \mathcal{T}$$

For line-outage contingencies ($k \in \mathcal{K}_L$): no generator is tripped,
so all ramp-limited generators are constrained:
$$p^k_{g,t} - p^0_{g,t} \leq RU_g \quad \forall g \in \mathcal{G}^{RU},\; t \in \mathcal{T}$$
$$p^0_{g,t} - p^k_{g,t} \leq RD_g \quad \forall g \in \mathcal{G}^{RD},\; t \in \mathcal{T}$$

> [!NOTE]
> This formulation does **not** couple consecutive corrective periods via
> ramp constraints (i.e., there is no $p^k_{g,t} - p^k_{g,t-1} \leq RU_g$).
> Only the deviation from the shared preventive dispatch is bounded. This is a
> planning-stage approximation: the corrective ramp at each period is bounded
> by the same ramp rate regardless of what the corrective dispatch was in the
> prior corrective period. This is an explicit design decision (confirmed for
> Stage 5F).

**Battery power limits under contingency:**
$$0 \leq ch^k_{b,t} \leq \bar{P}^{ch}_b, \quad 0 \leq dis^k_{b,t} \leq \bar{P}^{dis}_b \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Battery SoC dynamics under contingency:**
$$e^k_{b,t} = e^k_{b,t-1} + \eta^{ch}_b \cdot ch^k_{b,t} \cdot dt - \frac{dis^k_{b,t} \cdot dt}{\eta^{dis}_b} \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

with $e^k_{b,-1} \triangleq E^0_b$ (see Section 9.1 for the design decision on
initial SoC; see also Section 9.1 for what this does and does not model).

**Battery SoC bounds under contingency:**
$$\underline{E}_b \leq e^k_{b,t} \leq \bar{E}_b \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

The contingency stage does **not** enforce a terminal SoC constraint. This is
Assumption A2: contingency recovery is evaluated as a standalone horizon; the
battery's end-of-horizon SoC in a contingency scenario is a free outcome.

**Battery exclusivity under contingency** (exclusive mode only):
$$ch^k_{b,t} \leq \bar{P}^{ch}_b \cdot (1 - z^k_{b,t}), \quad dis^k_{b,t} \leq \bar{P}^{dis}_b \cdot z^k_{b,t} \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Unmet demand bounds (contingency):**
$$0 \leq u^k_{n,t} \leq D_{n,t} \quad \forall n \in \mathcal{N},\; t \in \mathcal{T},\; k \in \mathcal{K}$$

In strict mode: $u^k_{n,t} = 0 \; \forall n, t, k$.

**Nodal power balance (contingency):**

$$\sum_{g:\, n(g)=n} p^k_{g,t} + \sum_{\ell:\, b(\ell)=n} f^k_{\ell,t} - \sum_{\ell:\, a(\ell)=n} f^k_{\ell,t} + \sum_{b:\, n(b)=n} \bigl(dis^k_{b,t} - ch^k_{b,t}\bigr) + u^k_{n,t} = D_{n,t}$$

$$\forall n \in \mathcal{N},\; t \in \mathcal{T},\; k \in \mathcal{K}$$

---

### 5.3 Shared preventive schedule (coupling)

The preventive variables ($p^0, f^0, u^0, ch^0, dis^0, e^0$) are single sets
of decision variables that appear in the preventive constraints and in the
corrective ramp constraints of every contingency. They are **not duplicated**
per contingency. This is the mathematical expression of "one common preventive
schedule": if the solver changes $p^0_{g,t}$ it changes it simultaneously
for all contingencies.

No additional equality coupling constraints are needed. The model structure
itself enforces the non-anticipativity of the preventive plan.

---

## 6. Objective function

### 6.1 Preventive operating cost

$$C^{prev} = dt \left[\sum_{g \in \mathcal{G}} \sum_{t \in \mathcal{T}} c_g\, p^0_{g,t} + \lambda \sum_{n \in \mathcal{N}} \sum_{t \in \mathcal{T}} u^0_{n,t} + \sum_{b \in \mathcal{B}} \sum_{t \in \mathcal{T}} \kappa_b\, dis^0_{b,t}\right] + \varepsilon \cdot dt \sum_{b \in \mathcal{B}} \sum_{t \in \mathcal{T}} \bigl(ch^0_{b,t} + dis^0_{b,t}\bigr)$$

This matches the existing `scheduling.py` objective exactly:
- $dt \sum_{g,t} c_g p^0_{g,t}$: generation cost.
- $dt \cdot \lambda \sum_{n,t} u^0_{n,t}$: unmet-demand penalty.
- $dt \sum_{b,t} \kappa_b dis^0_{b,t}$: battery throughput cost (`throughput_cost_per_mwh`).
- $\varepsilon \cdot dt \sum_{b,t}(ch^0_{b,t} + dis^0_{b,t})$: tiebreak (`tiebreak_epsilon`).

### 6.2 Contingency cost (penalized mode)

For each contingency $k$, the corrective operating cost is:

$$C^k = dt \left[\sum_{g \in \mathcal{G}} \sum_{t \in \mathcal{T}} c_g\, p^k_{g,t} + \lambda \sum_{n \in \mathcal{N}} \sum_{t \in \mathcal{T}} u^k_{n,t} + \sum_{b \in \mathcal{B}} \sum_{t \in \mathcal{T}} \kappa_b\, dis^k_{b,t}\right] + \varepsilon \cdot dt \sum_{b \in \mathcal{B}} \sum_{t \in \mathcal{T}} \bigl(ch^k_{b,t} + dis^k_{b,t}\bigr)$$

The tiebreak $\varepsilon$ term is included for API and convention consistency
with the preventive model.

**Equal contingency weighting (R4 decision).** Stage 5F assigns equal
probability to every contingency. With $|\mathcal{K}|$ contingencies the
aggregate contingency cost is:

$$C^{cont} = \frac{1}{|\mathcal{K}|} \sum_{k \in \mathcal{K}} C^k$$

**Rationale for equal weights:** The N-1 security standard requires the system
to withstand any single credible contingency, not a probability-weighted subset.
Equal weighting is the direct expression of this standard.  
**Implications of equal weighting:** Every contingency, regardless of its
probability of occurrence, contributes equally to the objective. This means a
rare but easily-corrected contingency has the same objective weight as a
frequent, hard-to-recover one. If users have contingency probability estimates
they may substitute explicit weights $\pi_k \geq 0$ with $\sum_k \pi_k = 1$;
the formulation supports this without changing any constraint. For Stage 5F the
default is equal weights.

**No double-counting.** The preventive variables ($p^0_{g,t}$) and
corrective variables ($p^k_{g,t}$) are distinct. $C^{prev}$ sums over $p^0$
only; $C^{cont}$ sums over $p^k$ only. These are separate sets of decision
variables. No cost component appears in both.

### 6.3 Total objective

$$\min \quad C^{prev} + C^{cont}$$

### 6.4 Strict security mode

In strict mode, unmet demand is forced to zero in both stages:
$u^0_{n,t} = 0$ and $u^k_{n,t} = 0$ for all $n, t, k$.

The unmet-demand penalty terms vanish from both $C^{prev}$ and $C^{cont}$.
If the monolithic model is infeasible under these constraints, the system is
reported as **not N-1 secure** and no schedule is returned.

> [!IMPORTANT]
> **N-1 security is not assessed from the objective value.** An optimal solver
> status in penalized mode does not mean the system is N-1 secure. Security is
> assessed by the independent verifier checking $u^k_{n,t} \leq \text{tol}$
> for every $n, t, k$. The `n1_secure` field in the result is set by the
> verifier, not by the solver.

### 6.5 Cost decomposition reported to callers

| Field | Definition |
|-------|-----------|
| `preventive_generation_cost_usd` | $dt \sum_{g,t} c_g p^0_{g,t}$ |
| `preventive_unmet_penalty_usd` | $dt \cdot \lambda \sum_{n,t} u^0_{n,t}$ |
| `preventive_battery_throughput_usd` | $dt \sum_{b,t} \kappa_b dis^0_{b,t}$ |
| `preventive_tiebreak_usd` | $\varepsilon \cdot dt \sum_{b,t}(ch^0_{b,t} + dis^0_{b,t})$ |
| `preventive_operating_cost_usd` | Sum of above four |
| `contingency_generation_cost_usd` | $\frac{1}{|\mathcal{K}|} \sum_k dt \sum_{g,t} c_g p^k_{g,t}$ |
| `contingency_unmet_penalty_usd` | $\frac{1}{|\mathcal{K}|} \sum_k dt \cdot \lambda \sum_{n,t} u^k_{n,t}$ |
| `contingency_battery_throughput_usd` | $\frac{1}{|\mathcal{K}|} \sum_k dt \sum_{b,t} \kappa_b dis^k_{b,t}$ |
| `contingency_tiebreak_usd` | $\frac{1}{|\mathcal{K}|} \sum_k \varepsilon \cdot dt \sum_{b,t}(ch^k_{b,t} + dis^k_{b,t})$ |
| `total_objective_usd` | Solver objective value ($C^{prev} + C^{cont}$) |
| `n1_secure` | `True` iff verifier confirms $u^k_{n,t} \leq \text{tol}$ for all $n, t, k$ |
| `per_contingency` | List of per-contingency unmet energy and cost |

---

## 7. Security definitions

### 7.1 N-1 security (strict mode)

The system is declared **N-1 secure** if and only if:

1. The strict-mode monolithic model is feasible and reaches an optimal solution.
2. The independent verifier confirms $u^k_{n,t} \leq \text{tol}$ for all
   $n \in \mathcal{N}$, $t \in \mathcal{T}$, $k \in \mathcal{K}$.

Condition 1 alone is insufficient. The independent verifier (Section 8) must
pass. Solver termination status `optimal` is not equivalent to N-1 security.

### 7.2 N-1 security (penalized mode)

A contingency $k$ is **individually secure** if the verifier confirms
$u^k_{n,t} \leq \text{tol}$ for all $n, t$.

The system is **fully N-1 secure** only if every contingency is individually
secure. Even one insecure contingency makes the system not N-1 secure. The
`n1_secure` field is `False` if any contingency fails.

### 7.3 Partial security reporting

In penalized mode, the result distinguishes:
- Contingencies that are individually secure (zero unmet demand after correction).
- Contingencies with residual unmet demand and its magnitude.
- The total unmet energy summed across all contingencies and all periods.

This allows callers to identify which specific contingency is problematic,
rather than receiving only a pass/fail.

---

## 8. Independent verification requirements

The contingency verifier must operate **without Pyomo**. All checks derive
from the original input data (Network, Horizon, Battery objects) and the
returned solution arrays. It follows the same design philosophy as the
existing `validate_schedule` in `validation.py`.

### 8.1 Required checks

**Preventive-schedule checks** (run once, shared):

| Check name | Verifies |
|------------|---------|
| `prev_completeness` | All expected arrays are present and have correct length |
| `prev_generator_limits` | $0 \leq p^0_{g,t} \leq \bar{P}_{g,t}$ |
| `prev_line_capacity` | $|f^0_{\ell,t}| \leq F_{\ell,t}$ |
| `prev_ramp_limits` | Preventive ramp constraints hold between periods and from initial dispatch |
| `prev_power_balance` | Nodal balance at every $(n,t)$ |
| `prev_battery_power` | $0 \leq ch^0_{b,t} \leq \bar{P}^{ch}_b$; $0 \leq dis^0_{b,t} \leq \bar{P}^{dis}_b$ |
| `prev_battery_soc` | SoC dynamics, bounds, initial, and terminal conditions |
| `prev_unmet_bounds` | $0 \leq u^0_{n,t} \leq D_{n,t}$ |

**Per-contingency checks** (run for each $k$):

| Check name | Verifies |
|------------|---------|
| `contingency_identity` | Reported component id and type match the declared contingency |
| `corr_generator_limits` | $0 \leq p^k_{g,t} \leq \bar{P}^k_{g,t}$ |
| `corr_outage_zero` | Failed component carries zero output or zero flow |
| `corr_line_capacity` | $|f^k_{\ell,t}| \leq F^k_{\ell,t}$ |
| `corr_ramp_limits` | For surviving ramp-limited generators: $|p^k_{g,t} - p^0_{g,t}| \leq$ ramp limit; failed generator is exempt |
| `corr_power_balance` | Nodal balance at every $(n,t)$ |
| `corr_battery_power` | Battery power limits under contingency |
| `corr_battery_soc` | SoC dynamics from $E^0_b$, bounds, no terminal constraint |
| `corr_unmet_bounds` | $0 \leq u^k_{n,t} \leq D_{n,t}$ |
| `corr_no_unmet_strict` | $u^k_{n,t} = 0$ when strict mode is active |
| `corr_objective` | Per-contingency cost recomputed from data matches reported value |

Each check produces a `Check` object (same structure as `validation.py`):
`name`, `passed`, `max_violation`, `detail`, `severity`, `category`.

### 8.2 Structured result types

```python
@dataclass
class ContingencyVerification:
    contingency_id: str       # e.g. "gen:gas-north" or "line:line-north-central"
    contingency_type: str     # "generator" or "line"
    affected_id: str          # failed component id
    checks: list[Check]
    passed: bool              # all(c.passed for c in checks)
    unmet_energy_mwh: float   # dt * sum_{n,t} u^k_{n,t}
    n1_secure: bool           # unmet_energy_mwh <= tol


@dataclass
class SCPSVerificationReport:
    preventive_checks: list[Check]         # shared preventive checks
    contingency_results: list[ContingencyVerification]
    n1_secure: bool                        # all contingencies secure
    passed: bool                           # all checks passed
    n_contingencies: int
    n_secure: int                          # count of individually secure contingencies
    total_unmet_energy_mwh: float          # sum over all contingencies
```

---

## 9. Design decisions and confirmed assumptions

### 9.1 Battery SoC initial condition for the corrective stage (R3)

**Confirmed design: planning-stage joint optimisation from shared initial state.**

In Stage 5F, every contingency stage starts from the same initial SoC $E^0_b$
as the preventive stage. The formulation is a **joint optimisation** that asks:

> *Starting from the same initial conditions ($D_{n,t}$, $\bar{P}_{g,t}$,
> $F_{\ell,t}$, $E^0_b$), can we find a single preventive schedule such that,
> when any one component is removed, the remaining equipment starting from
> $E^0_b$ can still serve all demand?*

This is a **planning-stage evaluation**, not a model of an outage occurring
during the execution of the preventive trajectory. It does not simulate the
battery SoC at period $t_0$ when a fault occurs at period $t_0 > 0$ with the
battery having already acted for $t_0$ periods.

**What this does NOT model:**

An operational scenario in which the preventive schedule runs for, say,
3 periods and then a generator trips. In that case the battery SoC available
for recovery would be $e^0_{b,2}$ (the end-of-period-2 preventive SoC), not
$E^0_b$. Stage 5F does not model this mid-trajectory fault. That would require
a scenario-tree formulation with the fault occurring at different points in the
horizon, which is outside scope.

**Consequence:** The Stage 5F battery SoC available for contingency recovery
is the planning-time initial SoC, not the in-service SoC at the moment of
the fault. The formulation may be optimistic about battery availability in
the latter periods of the horizon. This is an explicit, documented limitation.

**Rejected alternative (coupled SoC):** Coupling the contingency battery SoC
to the preventive trajectory ($e^k_{b,t-1} = e^0_{b,t-1}$ for all $t$) would
add $|\mathcal{B}| \cdot T \cdot |\mathcal{K}|$ equality constraints, turning
the corrective battery into a continuation of the preventive one. This reduces
to the mid-trajectory fault model, which is outside Stage 5F scope.

### 9.2 Corrective ramp coupling between periods

Corrective ramp constraints bound $|p^k_{g,t} - p^0_{g,t}|$ independently at
each period $t$. There are no corrective-vs-corrective intertemporal ramp
constraints ($p^k_{g,t} - p^k_{g,t-1}$). This means the corrective dispatch
at each period is bounded relative to the preventive dispatch at the same
period, not relative to what the corrective dispatch was in the prior period.
This is a planning-stage approximation. Confirmed for Stage 5F.

### 9.3 Implementation strategy

Monolithic Pyomo model: one set of preventive variables, one set of corrective
variables per contingency, all constraints in one model, one solver call.
Simple to implement and audit. May be slow for large $|\mathcal{K}|$. 
Decomposition is deferred to a later stage.

### 9.4 Contingency scope

N-1 only: exactly one component fails at a time. N-2 and cascading failures
are explicitly out of scope for Stage 5F.

### 9.5 Battery exclusivity in MIP mode

When `battery_mode="exclusive"`, binary variables $z^k_{b,t}$ must be added
per contingency. This increases MIP size as $O(|\mathcal{K}|)$. LP relaxation
of corrective sub-problems as a scalability option is deferred to 5F.5.

---

## 10. Test strategy

### 10.1 Hand-calculated small-network cases

Each case uses $T=1$ period, $dt=1$ h unless otherwise stated. All arithmetic
is worked from first principles. No case uses a solver to derive the expected result.

---

**Case 1 — Two nodes, one generator, one line (islanding)**

Network:
- Node A: generator $g_1$, capacity 100 MW, \$10/MWh. Demand $D_A = 80$ MW.
- Node B: demand $D_B = 20$ MW. No local generation.
- Line $\ell_1$: A→B, capacity 30 MW.
- No batteries. Single period ($T=1$, $dt=1$).

Preventive dispatch (minimize $10 p^0_{g_1}$):
- $g_1$ must cover $D_A = 80$ MW locally plus any export.
- B needs 20 MW via line. $p^0_{g_1} = 80 + 20 = 100$ MW (≤ 100 cap ✓).
  $f^0_{\ell_1} = 20$ MW (≤ 30 cap ✓). $u^0 = 0$ everywhere. ✓

Contingency $k = $ outage of $\ell_1$:
- $F^k_{\ell_1} = 0$: no flow to B.
- B has no local generation and no line supply: $p^k_{g_1}$ can only serve A.
  B balance: $0 + 0 + u^k_B = D_B = 20$ MW → $u^k_B = 20$ MW (full demand unmet).

**Expected result: NOT N-1 secure. Shortfall = 20 MW at B per period.**

---

**Case 2 — Two nodes, two generators, one line (corrected)**

Network:
- Node A: generator $g_1$, capacity 100 MW, \$10/MWh. Demand $D_A = 60$ MW.
- Node B: generator $g_2$, capacity 50 MW, \$20/MWh. Demand $D_B = 60$ MW.
- Line $\ell_1$: A→B, capacity 50 MW. No batteries. No ramp limits.

Preventive dispatch (minimize $10 p^0_{g_1} + 20 p^0_{g_2}$):

Optimal: maximise cheap $g_1$. At $g_1 = 100$ MW: A balance gives export
$= 100 - 60 = 40$ MW over $\ell_1$ (≤ 50 cap ✓). B balance:
$40 + p^0_{g_2} = 60 \Rightarrow p^0_{g_2} = 20$ MW (≤ 50 cap ✓).  
Preventive cost = $10 \times 100 + 20 \times 20 = \$1\,400$/MWh$\cdot$h.
$u^0 = 0$ everywhere. ✓

Contingency $k = $ outage of $g_2$ (generator contingency):

$g_2$ is the failed generator; it is exempt from corrective ramp (no ramps
declared anyway). $\bar{P}^k_{g_2} = 0$, so $p^k_{g_2} = 0$.

Corrective dispatch for $g_1$ at A:
- A balance: $p^k_{g_1} - 60 - f^k_{\ell_1} + u^k_A = 0$. With $u^k_A = 0$:
  $p^k_{g_1} = 60 + f^k_{\ell_1}$.
- $g_1$ is at its capacity upper bound: $p^k_{g_1} \leq 100$ → $f^k_{\ell_1} \leq 40$.
- Line capacity: $f^k_{\ell_1} \leq 50$. Binding constraint: $f^k_{\ell_1} \leq 40$
  (limited by $g_1$'s capacity, not the line).
- B balance: $f^k_{\ell_1} + u^k_B = 60$ → $u^k_B = 60 - 40 = 20$ MW.

**Expected result: NOT N-1 secure. Shortfall = 20 MW at B per period.**

Reason: $g_1$ is already at full capacity (100 MW); it can supply only
$100 - 60 = 40$ MW to B. The line capacity (50 MW) is not the binding
constraint — $g_1$'s generation capacity is. With $g_2$ out, 20 MW of B's
demand cannot be served.

> [!NOTE]
> The original Revision 1 spec incorrectly stated this case was N-1 secure.
> The error was: assuming $g_1$ could supply 60 MW of export while serving
> 60 MW of local demand — which would require $g_1 = 120$ MW, exceeding
> its 100 MW capacity.

---

**Case 3 — Ramp-constrained corrective recovery**

Network:
- Node A: generator $g_1$, capacity 200 MW, \$10/MWh, $RU_{g_1} = 10$ MW.
  Initial dispatch $p^{init}_{g_1} = 80$ MW. Demand $D_A = 60$ MW.
- Node B: generator $g_2$, capacity 60 MW, \$20/MWh, no ramp. Demand $D_B = 60$ MW.
- Line $\ell_1$: A→B, capacity 200 MW (not the binding constraint in this case).
- No batteries. $T=1$, $dt=1$.

Preventive dispatch (with ramp from initial 80 MW, $RU = 10$):
- $g_1 \leq 80 + 10 = 90$ MW. Optimal: $g_1 = 90$ (cheapest).
- A export: $90 - 60 = 30$ MW to B via line (≤ 200 ✓).
- B balance: $30 + p^0_{g_2} = 60$ → $p^0_{g_2} = 30$ MW (≤ 60 ✓).
- Preventive cost = $10 \times 90 + 20 \times 30 = \$1\,500$/MWh$\cdot$h. ✓

Contingency $k = $ outage of $g_2$:

$g_2$ is the failed generator; exempt from corrective ramp. $p^k_{g_2} = 0$.
Corrective ramp on $g_1$: $p^k_{g_1} \leq p^0_{g_1} + RU_{g_1} = 90 + 10 = 100$ MW.
Also $p^k_{g_1} \leq 200$ (cap). Binding: $p^k_{g_1} \leq 100$.
A export: $100 - 60 = 40$ MW to B. B balance: $40 + u^k_B = 60$ → $u^k_B = 20$ MW.

**Expected result: NOT N-1 secure. Shortfall = 20 MW at B per period.**

Without ramp constraint: $g_1$ could supply $200 - 60 = 140$ MW to B ≥ 60.
The ramp limit ($RU = 10$) is the cause of the shortfall here, not the line
or $g_1$'s nameplate capacity.

---

**Case 4 — Battery-assisted recovery**

Same network as Case 3, plus a battery at node B:
- Battery $b_1$ at B: capacity 60 MWh, $\bar{P}^{dis}_{b_1} = 30$ MW,
  $\bar{P}^{ch}_{b_1} = 30$ MW, $\eta^{ch} = \eta^{dis} = 1.0$, $E^0_{b_1} = 60$ MWh,
  $\underline{E}_{b_1} = 0$ MWh. No terminal SoC constraint.

Preventive: same as Case 3. Battery idle in preventive ($ch^0 = dis^0 = 0$).

Contingency $k = $ outage of $g_2$:

Corrective: $g_1^k \leq 100$ (ramp-limited). A exports 40 MW to B.
B balance: $40 + dis^k_{b_1} + u^k_B = 60$.
Battery can discharge up to $\bar{P}^{dis} = 30$ MW. To eliminate unmet demand:
$dis^k_{b_1} = 20$ MW. SoC after: $E^0 - 20 \times dt = 60 - 20 = 40$ MWh (≥ 0 ✓).
B gets $40 + 20 = 60$ MW = $D_B$. $u^k_B = 0$.

**Expected result: N-1 secure with battery assistance.**
Battery discharges 20 MW in the corrective period. SoC falls from 60 MWh to 40 MWh.

Variant — insufficient battery energy: If $E^0_{b_1} = 10$ MWh and
$\underline{E}_{b_1} = 0$ MWh: maximum discharge energy = 10 MWh over 1 hour
→ can only deliver 10 MW. B gets $40 + 10 = 50 < 60$ → $u^k_B = 10$ MW.
**NOT N-1 secure with this initial SoC.**

---

### 10.2 Property-based tests

Tests in `tests/test_security.py` must verify:

- Preventive dispatch ($p^0$, $f^0$, $e^0$) is identical across all returned
  contingency evaluations.
- Strict mode infeasibility propagates to `n1_secure = False`; no fabricated
  schedule is returned.
- In penalized mode, one insecure contingency produces `n1_secure = False`
  even when all other contingencies are secure.
- The independent verifier detects: corrupted generator dispatch (above cap),
  corrupted line flow (above cap), corrupted nodal balance, the failed
  generator reporting non-zero output, a surviving generator violating corrective
  ramp limits, incorrect contingency identity, incorrect objective value.
- In strict mode, the verifier flags any $u^k_{n,t} > \text{tol}$ as a failure.

### 10.3 Existing test suite

The 295 existing tests must continue to pass. Stage 5F adds new tests only in
`tests/test_security.py` and `tests/test_security_validation.py`.

---

## 11. Files to be created or modified in later milestones

### New files (5F.2 and later)

| File | Milestone | Purpose |
|------|-----------|---------|
| `gridmind/security.py` | 5F.2–5F.3 | `Contingency`, `ContingencySet`, `SCPSProblem`, `SCPSResult`, `solve_security_constrained` |
| `tests/test_security.py` | 5F.3+ | Optimization and security-mode tests |
| `tests/test_security_validation.py` | 5F.4 | Verifier tests with corrupted outputs |
| `benchmarks/security_bench.py` | 5F.6 | Performance benchmarks |
| `STAGE5F_ACCEPTANCE_REPORT.md` | 5F.6 | Final acceptance report |

### Files modified in later milestones

| File | Change |
|------|--------|
| `gridmind/validation.py` | Add `ContingencyVerification`, `SCPSVerificationReport`, `verify_contingency_schedule` |
| `gridmind/__init__.py` | Export new public symbols |
| `README.md` | Document Stage 5F formulation |

### Files that must NOT be modified

| File | Reason |
|------|--------|
| All files on `stage-5e-work-in-progress` | Frozen per preservation rules |
| `gridmind/optimizer.py` | Single-period engine untouched |
| `benchmarks/baseline.json` | Historical artifact |
| Any existing test file | No deletion or weakening |

---

## 12. Performance evaluation plan (5F.6)

Benchmarks measure:

| Metric | Method |
|--------|--------|
| Model construction time | `time.perf_counter()` around `build_scps_model` |
| Solver time | As reported by solver |
| Total time | Call entry to result return |
| Variable count | `model.nvariables()` after build |
| Constraint count | `model.nconstraints()` after build |
| Contingency count | $|\mathcal{K}|$ |
| Solver identity | `resolve_solver()` name |
| Feasibility | `SCPSResult.status` |
| Verification outcome | `SCPSVerificationReport.passed` |

Benchmark network sizes (reproducible synthetic networks):

| Size label | Nodes | Generators | Lines | Batteries | N-1 contingencies |
|------------|-------|------------|-------|-----------|-------------------|
| Small | 3 | 3 | 3 | 0 | 6 |
| Medium | 6 | 6 | 8 | 2 | 14 |
| Large | 10 | 10 | 15 | 4 | 25 |

> [!CAUTION]
> The "large" benchmark is for local measurement only. CI runs only "small"
> and "medium" sizes. Results stored in `benchmarks/runs/stage5f_*/`
> (gitignored; not committed).

---

## 13. Assumptions, limitations, and scope boundary

### 13.1 Transportation-flow security vs. electrical grid N-1 security

GridMind uses a **lossless transportation-flow (network-flow) abstraction**.
A real electrical grid operates under Kirchhoff's laws: line flows are
determined by line reactances and voltage angles, not by optimisation choices.
After a contingency, power automatically redistributes according to the laws
of physics — potentially overloading other lines (cascading failure). GridMind
does not model any of this.

**What Stage 5F's security criterion means in this abstraction:**

The SCPS model asks whether, for a given network topology with one component
removed, an optimiser can find a redispatch of generators and batteries such
that all nodal power balances are satisfied within generation and line capacity
bounds. Flows are free optimisation variables, not physics-determined values.

**What this does NOT imply:**

- It does not imply that the real electrical system would be stable after the
  contingency.
- It does not capture voltage stability, angular stability, or frequency
  response after a trip.
- It does not model the automatic (physics-driven) power rerouting that occurs
  before any corrective action is taken.
- A network that passes this transportation-flow N-1 check may still be
  electrically insecure. A network that fails this check is certainly insecure
  (insufficient capacity even under the most favourable flow assumptions).

**Consequence for results:** Stage 5F security results are necessary conditions
for security (insufficient capacity to serve demand → definitely insecure) but
not sufficient conditions. They must not be presented as electrical grid security
assessments.

### 13.2 Additional limitations

1. **No N-k (k > 1) contingencies.** Single-component outages only.
2. **No unit commitment.** Generators on-line in the preventive schedule remain
   on-line in the corrective stage (except the failed one). Corrective startup
   or shutdown is not modelled.
3. **No frequency or inertia.** Frequency response after a trip is not evaluated.
4. **Corrective ramp (A1).** Corrective ramp bounds $|p^k_{g,t} - p^0_{g,t}|$
   independently per period, not as a consecutive-period corrective ramp rate.
   This is a planning-stage approximation.
5. **Battery SoC (A2 / planning-stage joint optimisation).** Contingency
   batteries start from $E^0_b$, not from the in-service SoC at the moment of
   the fault. This may overstate available battery energy in later horizon
   periods if the preventive schedule depletes the battery.
6. **Scalability.** Monolithic model grows as
   $O(|\mathcal{K}| \cdot T \cdot (|\mathcal{G}| + |\mathcal{L}| + |\mathcal{B}|))$.
7. **MIP size in exclusive mode.** Binary variables scale with
   $|\mathcal{K}| \cdot |\mathcal{B}| \cdot T$.

---

## 14. Appendix: integration with existing modules

| Module | Stage 5F use |
|--------|-------------|
| `gridmind/network.py` | `Network`, `Generator`, `Line`, `Node` — used unchanged |
| `gridmind/multiperiod.py` | `ScheduleProblem`, `Horizon`, `Battery`, `constant_horizon` — used unchanged |
| `gridmind/scheduling.py` | Not reused directly; `ScheduleResult` naming conventions referenced |
| `gridmind/solver.py` | `resolve_solver`, `SolverUnavailableError` — reused unchanged |
| `gridmind/validation.py` | `Check` — reused; new types added additivley |

No existing public API is removed or modified. All Stage 5F additions are
strictly additive.

---

## Stage 5F.1 Completion Report (Revision 2)

**Deliverable:** This revised specification document (`STAGE5F_SPEC.md`).

**Production files changed:** None.

**Decisions changed in Revision 2:**

| # | Original | Revised |
|---|----------|---------|
| R1 — Corrective ramp | Applied to all generators including the tripped one | Tripped generator is **exempt** from corrective ramp; constraint applies only to surviving generators |
| R2 — Case 2 outcome | Incorrectly stated N-1 secure | Corrected to **NOT secure**, 20 MW shortfall at B, with full arithmetic workings |
| R3 — Battery SoC framing | Described as "operational interpretation (N-1 is checked at the planning stage, not a sequential fault at t=5)" without distinguishing what it does not model | Now explicitly states this is a **planning-stage joint optimisation from shared initial conditions** and clearly states what it does NOT model (mid-trajectory fault) |
| R4 — Objective weighting | $1/\|\mathcal{K}\|$ stated but not justified; no decision on probabilities; tiebreak omitted from corrective cost | Equal-weight decision confirmed and justified; implications of equal weighting stated; tiebreak added to corrective cost for consistency; no-double-counting shown explicitly |
| R5 — API verification | Parameters listed without source references | All parameters now linked to their exact source field in existing data classes |
| R6 — Test cases | Cases 3 and 4 had imprecise network parameters; Case 4 was under-specified | All cases redesigned with fully specified networks, step-by-step arithmetic, and unambiguous expected outcomes |
| R7 — Test baseline | Placeholder text "See test run results appended below" with no actual results | Replaced with actual recorded results |
| R8 — Scope boundary | Limitations section existed but no explicit statement distinguishing transportation-flow from electrical security | New Section 13 added with explicit scope boundary |

**Baseline test results (recorded 2026-11-03, this machine):**

Environment: CPython 3.15.0, Windows 11, Pyomo installed, pandas blocked by
AppLocker DLL policy, highspy not installed (CMake build failure).

```
Non-pandas tests (network, scenarios, currency, stats, robustness,
failure-handling, stochastic model-structure, control, scalability):
    154 passed, 70 skipped

Tests requiring pandas (test_multiperiod, test_optimizer,
test_validation_baseline, CLI tests, stochastic CLI):
    3 collection errors (import failure at pandas C extension load),
    5 test failures (same root cause)

Root cause of all failures: ImportError: DLL load failed while importing
offsets: An Application Control policy has blocked this file.
This is a pre-existing machine-level AppLocker constraint.
No GridMind source file was modified.
```

**Open questions resolved:**
- Corrective ramp for tripped generator: **Resolved — tripped generator is exempt.**
- Battery SoC design: **Resolved — planning-stage joint optimisation from shared initial state; mid-trajectory fault is out of scope.**
- Objective weighting: **Resolved — equal weights (1/|K|) confirmed; explicit probabilities deferred.**
- Corrective generation cost: **Resolved — included, with tiebreak, for consistency.**
- Double-counting: **Resolved — none; preventive and corrective variables are distinct.**

**Remaining open question for review:**
- Exclusive-mode MIP: add binaries per contingency, or LP-relax corrective sub-problems?

**Next step:** 5F.2 — Contingency representation.  
**Do not proceed without explicit review approval.**
