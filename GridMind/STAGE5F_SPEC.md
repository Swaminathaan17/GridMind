# Stage 5F — Security-Constrained Preventive Scheduling
## Mathematical Specification (Milestone 5F.1)

**Branch:** `stage-5f-work-in-progress`  
**Prepared:** 2026-11-03  
**Status:** DRAFT — awaiting review before implementation begins  
**Depends on:** Stage 5E (accepted 2026-11-03)

---

> [!IMPORTANT]
> This document is the **sole deliverable** of Milestone 5F.1. No production
> source code is modified in this milestone. All decisions documented here are
> subject to review before 5F.2 begins.

---

## 1. Problem definition

GridMind's existing multi-period scheduler (Stage 5B–5E) finds a single
least-cost schedule assuming all generators and lines remain in service. In
practice, any single component can fail unexpectedly. An N-1 security criterion
requires that the system remain operable after the loss of any one component.

**Security-Constrained Preventive Scheduling (SCPS)** answers the question:

> *Can we find one common base operating schedule (the "preventive" plan) such
> that, for every single-component outage, the system can restore supply through
> corrective redispatch of the remaining components?*

This is a two-level program:

- **Preventive (base) stage** — choose a common schedule valid under normal
  conditions.
- **Contingency (corrective) stage** — for each N-1 scenario, choose
  corrective actions within the limits of the remaining on-line equipment.

The design deliberately restricts itself to the **transportation-flow
abstraction** already present in GridMind. No voltage angles, reactance, or
AC/DC power-flow physics are added.

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
| $dt$ | Duration of each period (hours) |
| $n(g)$ | Node at which generator $g$ is located |
| $n(b)$ | Node at which battery $b$ is located |
| $a(\ell), b(\ell)$ | From-node and to-node of line $\ell$ |

Each contingency $k \in \mathcal{K}$ is identified by a tuple
$(type_k, id_k)$ where $type_k \in \{generator, line\}$ and $id_k$ is the
component id that fails.

---

## 3. Parameters

### 3.1 Network parameters (inherited from `Network` / `Horizon`)

| Symbol | Meaning |
|--------|---------|
| $D_{n,t}$ | Demand at node $n$ in period $t$ (MW) |
| $\bar{P}_{g,t}$ | Generation upper bound for generator $g$ at period $t$ (MW) — equals $\min(\text{capacity}_g, \text{availability}_{g,t}) \cdot \text{online}_{g,t}$ |
| $F_{\ell,t}$ | Thermal capacity of line $\ell$ at period $t$ (MW) — equals $0$ when $\text{in\_service}_{\ell,t} = \text{False}$ |
| $c_g$ | Generator marginal cost (USD/MWh) |
| $\lambda$ | Unmet-demand penalty (USD/MWh) |
| $\bar{P}^{ch}_b, \bar{P}^{dis}_b$ | Battery charge/discharge power limits (MW) |
| $\underline{E}_b, \bar{E}_b$ | Battery SoC minimum and maximum (MWh) |
| $E^0_b$ | Initial SoC of battery $b$ before period $0$ (MWh) |
| $\eta^{ch}_b, \eta^{dis}_b$ | Charge/discharge efficiencies |
| $\kappa_b$ | Battery throughput cost (USD/MWh discharged) |
| $\varepsilon$ | Tiebreak epsilon (USD/MWh) |

### 3.2 Corrective ramp parameters

The contingency corrective model must bound the change in generator output
between the preventive plan and the corrective dispatch. GridMind's `Generator`
already carries `ramp_up_mw` and `ramp_down_mw`. We **reuse** these
ramp limits for the corrective action:

| Symbol | Meaning |
|--------|---------|
| $RU_g$ | Max upward ramp of generator $g$ (MW/period) |
| $RD_g$ | Max downward ramp of generator $g$ (MW/period) |

**Assumption A1 — Corrective ramp scale.** The same per-period ramp bounds
that apply between consecutive time steps in the preventive model are used
to limit corrective redispatch from the preventive output level within the
same period. This is a conservative assumption (one period to respond).
If a generator has no declared ramp limit (`None`), it is treated as
unconstrained in the corrective stage (consistent with the preventive model).

> [!NOTE]
> This assumption is documented for review. An alternative is to treat
> corrective ramp limits as a fixed multiple of the preventive ramp (e.g., 1.5×
> for emergency response). We do not invent such a multiplier without explicit
> user confirmation.

### 3.3 Contingency parameters

For each contingency $k$:

| Symbol | Meaning |
|--------|---------|
| $\delta^G_{g,k}$ | 1 if generator $g$ is the failed element in contingency $k$, else 0 |
| $\delta^L_{\ell,k}$ | 1 if line $\ell$ is the failed element in contingency $k$, else 0 |

---

## 4. Decision variables

### 4.1 Preventive-stage variables (common across all contingencies)

| Variable | Domain | Meaning |
|----------|--------|---------|
| $p^0_{g,t}$ | $\mathbb{R}_{\geq 0}$ | Generator $g$ output in period $t$ (preventive), MW |
| $f^0_{\ell,t}$ | $\mathbb{R}$ | Line $\ell$ flow in period $t$ (preventive), MW (positive = from→to) |
| $u^0_{n,t}$ | $\mathbb{R}_{\geq 0}$ | Unmet demand at node $n$ in period $t$ (preventive), MW |
| $ch^0_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ charge power in period $t$ (preventive), MW |
| $dis^0_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ discharge power in period $t$ (preventive), MW |
| $e^0_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ SoC after period $t$ (preventive), MWh |
| $z^0_{b,t}$ | $\{0,1\}$ | Battery exclusivity binary (exclusive mode only) |

### 4.2 Contingency-stage variables (per contingency $k$)

| Variable | Domain | Meaning |
|----------|--------|---------|
| $p^k_{g,t}$ | $\mathbb{R}_{\geq 0}$ | Generator $g$ corrective output in period $t$ under contingency $k$, MW |
| $f^k_{\ell,t}$ | $\mathbb{R}$ | Line $\ell$ corrective flow in period $t$ under contingency $k$, MW |
| $u^k_{n,t}$ | $\mathbb{R}_{\geq 0}$ | Corrective unmet demand at node $n$ in period $t$ under contingency $k$, MW |
| $ch^k_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ corrective charge power in period $t$ under contingency $k$, MW |
| $dis^k_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ corrective discharge power in period $t$ under contingency $k$, MW |
| $e^k_{b,t}$ | $\mathbb{R}_{\geq 0}$ | Battery $b$ corrective SoC after period $t$ under contingency $k$, MWh |
| $z^k_{b,t}$ | $\{0,1\}$ | Contingency battery exclusivity binary (exclusive mode only) |

> [!NOTE]
> The preventive schedule is **shared** (superscript $0$). Each contingency
> $k$ has its own corrective recourse (superscript $k$). Battery SoC initial
> conditions for the corrective stage are taken from the **same** initial SoC
> as the preventive stage (see Section 5.3 — discussion in Section 9.1).

---

## 5. Constraints

### 5.1 Preventive-stage constraints

These are identical to the existing multi-period model in `scheduling.py` and
`multiperiod.py`.

**Generator limits:**
$$0 \leq p^0_{g,t} \leq \bar{P}_{g,t} \quad \forall g \in \mathcal{G},\; t \in \mathcal{T}$$

**Line capacity:**
$$-F_{\ell,t} \leq f^0_{\ell,t} \leq F_{\ell,t} \quad \forall \ell \in \mathcal{L},\; t \in \mathcal{T}$$

**Preventive ramp limits** (between consecutive periods, and from initial dispatch when declared):
$$p^0_{g,t} - p^0_{g,t-1} \leq RU_g \quad \forall g \in \mathcal{G}^{RU},\; t \in \mathcal{T} \setminus \{0\}$$
$$p^0_{g,t-1} - p^0_{g,t} \leq RD_g \quad \forall g \in \mathcal{G}^{RD},\; t \in \mathcal{T} \setminus \{0\}$$
$$p^0_{g,0} - p^{init}_g \leq RU_g, \quad p^{init}_g - p^0_{g,0} \leq RD_g \quad \text{when } p^{init}_g \text{ declared}$$

**Battery charge/discharge power:**
$$0 \leq ch^0_{b,t} \leq \bar{P}^{ch}_b, \quad 0 \leq dis^0_{b,t} \leq \bar{P}^{dis}_b \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Battery SoC dynamics:**
$$e^0_{b,t} = e^0_{b,t-1} + \eta^{ch}_b \cdot ch^0_{b,t} \cdot dt - \frac{dis^0_{b,t} \cdot dt}{\eta^{dis}_b} \quad \forall b, t$$
(with $e^0_{b,-1} = E^0_b$)

**Battery SoC bounds:**
$$\underline{E}_b \leq e^0_{b,t} \leq \bar{E}_b \quad \forall b \in \mathcal{B},\; t \in \mathcal{T}$$

**Battery terminal SoC** (when declared):
$$e^0_{b,T-1} = E^{final}_b \quad \forall b \text{ with declared final SoC}$$

**Battery exclusivity** (exclusive mode only):
$$ch^0_{b,t} \leq \bar{P}^{ch}_b (1 - z^0_{b,t}), \quad dis^0_{b,t} \leq \bar{P}^{dis}_b z^0_{b,t} \quad \forall b, t$$

**Unmet demand bounds (preventive):**
$$0 \leq u^0_{n,t} \leq D_{n,t} \quad \forall n \in \mathcal{N},\; t \in \mathcal{T}$$

In strict mode: $u^0_{n,t} = 0$.

**Nodal power balance (preventive):**
$$\sum_{g: n(g)=n} p^0_{g,t} + \sum_{\ell: b(\ell)=n} f^0_{\ell,t} - \sum_{\ell: a(\ell)=n} f^0_{\ell,t} + \sum_{b: n(b)=n} (dis^0_{b,t} - ch^0_{b,t}) + u^0_{n,t} = D_{n,t}$$
$$\forall n \in \mathcal{N},\; t \in \mathcal{T}$$

---

### 5.2 Contingency-stage constraints

For each contingency $k \in \mathcal{K}$, the following hold. Let:

$$\bar{P}^k_{g,t} = \bar{P}_{g,t} \cdot (1 - \delta^G_{g,k}) \quad (\text{failed generator output is forced to 0})$$
$$F^k_{\ell,t} = F_{\ell,t} \cdot (1 - \delta^L_{\ell,k}) \quad (\text{failed line capacity is forced to 0})$$

**Generator limits under contingency:**
$$0 \leq p^k_{g,t} \leq \bar{P}^k_{g,t} \quad \forall g \in \mathcal{G},\; t \in \mathcal{T}$$

**Line capacity under contingency:**
$$-F^k_{\ell,t} \leq f^k_{\ell,t} \leq F^k_{\ell,t} \quad \forall \ell \in \mathcal{L},\; t \in \mathcal{T}$$

**Corrective ramp limits** (bounding deviation from preventive dispatch):
$$p^k_{g,t} - p^0_{g,t} \leq RU_g \quad \forall g \in \mathcal{G}^{RU},\; t \in \mathcal{T}$$
$$p^0_{g,t} - p^k_{g,t} \leq RD_g \quad \forall g \in \mathcal{G}^{RD},\; t \in \mathcal{T}$$

These apply at the **same time period** — the corrective dispatch at $t$ is
bounded relative to the preventive dispatch at $t$.

**Battery power limits under contingency:**
$$0 \leq ch^k_{b,t} \leq \bar{P}^{ch}_b, \quad 0 \leq dis^k_{b,t} \leq \bar{P}^{dis}_b$$

**Battery SoC dynamics under contingency:**
$$e^k_{b,t} = e^k_{b,t-1} + \eta^{ch}_b \cdot ch^k_{b,t} \cdot dt - \frac{dis^k_{b,t} \cdot dt}{\eta^{dis}_b}$$
(with $e^k_{b,-1} = E^0_b$ — same initial SoC as preventive; see Section 9.1)

**Battery SoC bounds and terminal SoC under contingency:**
$$\underline{E}_b \leq e^k_{b,t} \leq \bar{E}_b$$

> [!NOTE]
> The contingency model **does not enforce a terminal SoC**. Contingency
> recovery is a short-duration event; requiring the battery to return to its
> final SoC target in every contingency would be overly restrictive and
> physically unrealistic. This is an explicit assumption (A2).

**Battery exclusivity under contingency** (exclusive mode only):
$$ch^k_{b,t} \leq \bar{P}^{ch}_b (1 - z^k_{b,t}), \quad dis^k_{b,t} \leq \bar{P}^{dis}_b z^k_{b,t}$$

**Unmet demand bounds (contingency):**
$$0 \leq u^k_{n,t} \leq D_{n,t} \quad \forall n, t, k$$

**Nodal power balance (contingency):**
$$\sum_{g: n(g)=n} p^k_{g,t} + \sum_{\ell: b(\ell)=n} f^k_{\ell,t} - \sum_{\ell: a(\ell)=n} f^k_{\ell,t} + \sum_{b: n(b)=n} (dis^k_{b,t} - ch^k_{b,t}) + u^k_{n,t} = D_{n,t}$$
$$\forall n \in \mathcal{N},\; t \in \mathcal{T},\; k \in \mathcal{K}$$

---

### 5.3 Coupling constraint (non-anticipativity)

The preventive schedule is **shared** — it is the same regardless of which
contingency occurs. This is enforced by construction: the preventive-stage
variables $p^0, f^0, u^0, ch^0, dis^0, e^0$ appear in the preventive
constraints and are referenced (but not duplicated) in the corrective ramp
constraints.

There is no equality coupling needed between preventive and contingency battery
SoC, because the contingency SoC sequence begins from the same $E^0_b$
independently. See Section 9.1 for the design decision.

---

## 6. Objective function

The objective is the **preventive operating cost** plus a **contingency
penalty term**. Corrective redispatch cost is included to avoid hiding expensive
contingency responses.

$$\min \quad C^{prev} + C^{cont}$$

### 6.1 Preventive operating cost

$$C^{prev} = dt \left[ \sum_{g \in \mathcal{G}} \sum_{t \in \mathcal{T}} c_g p^0_{g,t} + \lambda \sum_{n \in \mathcal{N}} \sum_{t \in \mathcal{T}} u^0_{n,t} + \sum_{b \in \mathcal{B}} \sum_{t \in \mathcal{T}} \kappa_b dis^0_{b,t} \right] + \varepsilon \cdot dt \sum_{b,t} (ch^0_{b,t} + dis^0_{b,t})$$

This is **identical** to the existing multi-period objective (matching
`scheduling.py` exactly).

### 6.2 Contingency cost (penalized mode)

In **penalized security mode**, corrective unmet demand is penalized:

$$C^{cont} = \frac{1}{|\mathcal{K}|} \sum_{k \in \mathcal{K}} dt \left[ \sum_{g,t} c_g p^k_{g,t} + \lambda \sum_{n,t} u^k_{n,t} + \sum_{b,t} \kappa_b dis^k_{b,t} \right]$$

The $1/|\mathcal{K}|$ normalization prevents the objective from growing
unboundedly with the number of contingencies and keeps costs dimensionally
consistent (all USD).

> [!NOTE]
> Including corrective generation cost prevents the optimizer from ignoring
> the cost of corrective redispatch. It is averaged across contingencies to
> avoid distorting the preventive problem when contingency counts are large.
> The alternative (only penalize unmet demand) is a design option for review.

### 6.3 Strict security mode

In **strict security mode**, contingency unmet demand is forced to zero:
$u^k_{n,t} = 0 \; \forall n, t, k$.

The objective becomes:
$$\min \quad C^{prev} + C^{cont,\text{strict}}$$

where $C^{cont,\text{strict}}$ retains the corrective generation cost term but
no unmet-demand penalty (since unmet demand is forbidden). If the model is
infeasible, the system is reported as **not N-1 secure** — infeasibility is
not disguised as a result.

### 6.4 Cost decomposition reported to callers

| Field | Content |
|-------|---------|
| `preventive_generation_cost_usd` | $dt \sum_{g,t} c_g p^0_{g,t}$ |
| `preventive_unmet_penalty_usd` | $dt \cdot \lambda \sum_{n,t} u^0_{n,t}$ |
| `preventive_battery_throughput_usd` | $dt \sum_{b,t} \kappa_b dis^0_{b,t}$ |
| `preventive_tiebreak_usd` | $\varepsilon \cdot dt \sum_{b,t}(ch^0+dis^0)$ |
| `preventive_operating_cost_usd` | Sum of the above four |
| `contingency_generation_cost_usd` | $C^{cont}$ generation component |
| `contingency_unmet_penalty_usd` | $C^{cont}$ penalty component |
| `contingency_battery_throughput_usd` | $C^{cont}$ battery component |
| `total_objective_usd` | Solver objective value |
| `n1_secure` | `True` if no contingency has unmet demand > tolerance |

---

## 7. Security definitions

### 7.1 N-1 security (strict)

The system is **N-1 secure** if and only if: the strict-mode model is
feasible AND, after solving, independent verification confirms
$u^k_{n,t} = 0$ for all $n, t, k$.

A solver status of `optimal` alone is insufficient. The independent verifier
must pass.

### 7.2 N-1 security (penalized)

The system is **partially secure** for a given contingency $k$ if:
$u^k_{n,t} = 0$ for all $n, t$. The system is **fully N-1 secure** only
if all contingencies are partially secure.

The `n1_secure` field in the result reflects the post-verification assessment,
not the solver status.

---

## 8. Independent verification requirements

The contingency verifier (`verify_contingency_schedule`) must operate
**without** Pyomo. It derives every check from the original input data and the
returned solution arrays.

### Required checks per contingency

| Check name | Description |
|------------|-------------|
| `contingency_identity` | Component id and type match the declared contingency |
| `preventive_generator_limits` | $0 \leq p^0_{g,t} \leq \bar{P}_{g,t}$ for all $g, t$ |
| `preventive_line_capacity` | $|f^0_{\ell,t}| \leq F_{\ell,t}$ for all $\ell, t$ |
| `preventive_power_balance` | Nodal balance at every $(n,t)$ |
| `preventive_ramp_limits` | Preventive ramp constraints satisfied |
| `corrective_generator_limits` | $0 \leq p^k_{g,t} \leq \bar{P}^k_{g,t}$ |
| `corrective_line_capacity` | $|f^k_{\ell,t}| \leq F^k_{\ell,t}$ |
| `corrective_outage_respected` | Failed component output/flow is zero |
| `corrective_power_balance` | Nodal balance at every $(n,t)$ |
| `corrective_ramp_limits` | $|p^k_{g,t} - p^0_{g,t}| \leq$ ramp limit |
| `battery_power_limits` | Charge/discharge within bounds (both stages) |
| `battery_soc_dynamics` | SoC step equation holds for both stages |
| `battery_soc_bounds` | SoC within $[\underline{E}_b, \bar{E}_b]$ |
| `battery_exclusivity` | No simultaneous charge/discharge (exclusive mode) |
| `unmet_demand_bounds` | $0 \leq u^k_{n,t} \leq D_{n,t}$ |
| `no_unmet_in_strict_mode` | All $u^k_{n,t} = 0$ when strict |
| `objective_recomputation` | Reported objective matches recomputed value |

Each check returns a named `Check` object (same structure as existing
`validation.py`): `name`, `passed`, `max_violation`, `detail`, `severity`,
`category`.

### Structured result

```python
@dataclass
class ContingencyVerification:
    contingency_id: str        # e.g. "gen:gas-north" or "line:line-north-central"
    contingency_type: str      # "generator" or "line"
    affected_id: str           # the failed component id
    checks: list[Check]
    passed: bool               # all(c.passed for c in checks)
    unmet_energy_mwh: float    # total unmet energy in this contingency
    n1_secure: bool            # unmet_energy_mwh <= tolerance

@dataclass
class SCPSVerificationReport:
    preventive_checks: list[Check]   # checks on the preventive schedule alone
    contingency_results: list[ContingencyVerification]
    n1_secure: bool                  # all contingencies secure
    passed: bool                     # all checks passed (preventive + contingency)
    n_contingencies: int
    n_secure: int                    # number of individually secure contingencies
    total_unmet_energy_mwh: float    # sum across all contingencies
```

---

## 9. Design decisions and unresolved questions

### 9.1 Battery SoC initial condition for corrective stage

**Decision options:**

A. **Independent restart** (selected for 5F.1): Each contingency's corrective
   stage starts from the same initial SoC $E^0_b$ as the preventive stage.
   This treats the contingency as an alternative scenario from $t=0$, not a
   continuation of the preventive trajectory. Simple to implement, conservative
   on battery availability.

B. **Coupled**: The corrective stage inherits the preventive SoC trajectory
   $e^0_{b,t}$. Requires a linking constraint $e^k_{b,-1} = E^0_b$ and
   $e^k_{b,t'} = e^0_{b,t'}$ for $t' < t_{contingency}$.

Option A is selected as it avoids coupling constraints and is a valid
operational interpretation (N-1 is checked at the planning stage, not a
sequential fault at $t = 5$). This must be confirmed at review.

### 9.2 Multi-period vs single-period corrective model

The corrective stage inherits the **full time horizon** $\mathcal{T}$ from
the preventive stage. An alternative is to evaluate each period independently
(no intertemporal coupling in the corrective stage). The full-horizon approach
is more accurate and is consistent with the existing multi-period engine.

### 9.3 Implementation strategy: monolithic vs decomposed

**Option A — Monolithic single model**: Build one Pyomo model with all
preventive + contingency variables and constraints. Simple to implement; may
become slow for large $|\mathcal{K}|$.

**Option B — Decomposed (Benders-style)**: Solve preventive first, then check
each contingency independently, fixing $p^0_{g,t}$. Scalable; complexity is
deferred. Loses joint optimality (preventive cost is optimized without
contingency feedback in LP relaxation).

**Recommendation for 5F.3**: Start with Option A (monolithic). The existing
Pyomo model infrastructure makes this straightforward. Measure performance in
5F.6 and defer decomposition to a later stage if needed.

### 9.4 Contingency scope

Initial scope: **N-1 only** (exactly one component fails at a time). N-2 or
common-mode failures are explicitly out of scope for 5F.

### 9.5 Battery corrective exclusivity in MIP mode

When `battery_mode="exclusive"`, binary variables $z^k_{b,t}$ must be added
per contingency as well. This increases MIP size linearly with $|\mathcal{K}|$.
A relaxation option (LP for contingency sub-problems even in exclusive
preventive mode) may be needed for scalability. Deferred to 5F.5.

---

## 10. Test strategy

### 10.1 Hand-calculable small-network cases

**Case 1 — Two nodes, one generator, one line (trivial)**
- Node A: generator $g_1$, 100 MW, \$10/MWh. Demand 80 MW.
- Node B: Demand 20 MW. No local generation.
- Line $\ell_1$: A→B, 30 MW capacity.
- Single contingency: outage of $\ell_1$.
- Expected: preventive $p^0_{g_1} = 100$, $f^0_{\ell_1} = 20$. After line
  outage, B is islanded with no generation → system is **not** N-1 secure.

**Case 2 — Two nodes, two generators, one line**
- Node A: $g_1$, 100 MW, \$10/MWh. Demand 60 MW.
- Node B: $g_2$, 50 MW, \$20/MWh. Demand 60 MW.
- Line $\ell_1$: A→B, 50 MW.
- Contingency: outage of $g_2$.
- Expected corrective action: $p^0_{g_1}=60+x$ where $x$ covers shortfall at
  B (up to 50 MW via line) → need $x=60$ from $g_2$'s outage; $g_1$ ramps to
  100 MW (capacity bound), then line at 40 MW. B fully served. N-1 secure.

**Case 3 — Ramp-constrained corrective recovery**
- Same as Case 2 but $g_1$ has ramp_up=10 MW and preventive $p^0_{g_1} = 60$.
- Contingency: outage of $g_2$. $g_1$ can only reach 70 MW.
- Line can transfer 10 MW to B; B demand 60 MW → shortfall 50 MW.
- Expected: **not N-1 secure** with strict mode; penalized mode reports 50 MW
  unmet per period.

**Case 4 — Battery-assisted recovery**
- Case 3 + battery at B: 100 MWh, 50 MW discharge, initial SoC 60 MWh.
- After $g_2$ outage: battery discharges 50 MW to B, line delivers 10 MW.
- B demand 60 MW = 10 + 50 → fully served. N-1 secure (for period 0 at least).

### 10.2 Property-based tests

- Preventive schedule must be identical across all contingency solves.
- Strict mode infeasibility must propagate to `n1_secure = False`.
- One failed contingency must not be hidden by successful ones.
- Corrupted generator dispatch must fail the verifier.
- Corrupted line flow must fail the verifier.
- Corrupted nodal balance must fail the verifier.
- Wrong contingency identity must fail the verifier.

### 10.3 Existing test suite

All 295 existing tests must continue to pass. Stage 5F adds tests in a new
file: `tests/test_security.py`.

---

## 11. Files to be created or modified in later milestones

### New files (5F.2+)

| File | Milestone | Purpose |
|------|-----------|---------|
| `gridmind/security.py` | 5F.2–5F.3 | `Contingency`, `ContingencySet`, `SCPSProblem`, `SCPSResult`, `solve_security_constrained` |
| `tests/test_security.py` | 5F.3+ | All security-constrained tests |
| `tests/test_security_validation.py` | 5F.4 | Verifier-specific tests (corrupted outputs) |
| `benchmarks/security_bench.py` | 5F.6 | Performance benchmarks |
| `STAGE5F_ACCEPTANCE_REPORT.md` | 5F.6 | Final acceptance report |

### Modified files (5F.3+)

| File | Change |
|------|--------|
| `gridmind/validation.py` | Add `verify_contingency_schedule`, `SCPSVerificationReport` |
| `gridmind/__init__.py` | Export new public API |
| `gridmind/cli.py` | Add `scps` sub-command (optional, deferred) |
| `README.md` | Document Stage 5F formulation and commands |

### Files that must NOT be modified

| File | Reason |
|------|--------|
| All files on `stage-5e-work-in-progress` | Frozen per preservation rules |
| `gridmind/optimizer.py` | Single-period engine untouched |
| `benchmarks/baseline.json` | Historical artifact |
| Existing test files | No deletion or weakening |

---

## 12. Performance evaluation plan (5F.6)

Benchmarks will measure:

| Metric | Method |
|--------|--------|
| Model construction time | `time.perf_counter()` around `build_scps_model` |
| Solver execution time | As returned by solver |
| Total execution time | From call entry to result return |
| Variable count | `model.nvariables()` after build |
| Constraint count | `model.nconstraints()` after build |
| Number of contingencies | $|\mathcal{K}|$ |
| Solver name and version | From `resolve_solver` |
| Feasibility status | `SCPSResult.status` |
| Verification pass/fail | `SCPSVerificationReport.passed` |

Benchmark network sizes (reproducible synthetic networks):

| Size | Nodes | Generators | Lines | Batteries | Contingencies |
|------|-------|------------|-------|-----------|---------------|
| Small | 3 | 3 | 3 | 0 | 6 |
| Medium | 6 | 6 | 8 | 2 | 14 |
| Large | 10 | 10 | 15 | 4 | 25 |

> [!CAUTION]
> Do not run "large" benchmarks in CI. The large case is intended for local
> benchmarking only. CI runs only "small" and "medium".

Results stored in `benchmarks/runs/stage5f_*/` (gitignored).

---

## 13. Known limitations

1. **No AC power flow.** The model is a lossless transportation-flow
   abstraction. No voltage angles, susceptances, or Kirchhoff's voltage law.
   The contingency model does not capture power rerouting governed by line
   impedances. This is explicitly documented in the existing GridMind codebase
   and is not introduced by Stage 5F.

2. **No N-k (k > 1) contingencies.** Only single-component outages are
   modeled. Common-mode failures, double outages, and cascading failures are
   out of scope.

3. **No unit commitment.** Generators cannot be started or shut down in the
   corrective stage. All generators that are on-line in the preventive schedule
   remain on-line in corrective (except the failed one).

4. **No frequency or inertia.** The model does not check frequency stability
   or rate-of-change-of-frequency after a trip.

5. **Corrective ramp assumption (A1).** Using the same per-period ramp limits
   for same-period corrective action is conservative. Actual contingency
   response may be faster. This simplification is documented and subject to
   review.

6. **Battery SoC coupling assumption (A2).** The contingency battery SoC starts
   from the same initial state as the preventive model (independent restart),
   not from the end-of-preventive-period SoC. This is a planning-stage
   abstraction. Actual corrective recovery would use the end-state SoC.

7. **Scalability.** The monolithic formulation grows as
   $O(|\mathcal{K}| \cdot T \cdot (|\mathcal{G}| + |\mathcal{L}| + |\mathcal{B}|))$
   variables and constraints. For large $|\mathcal{K}|$ this may be slow.
   Decomposition is deferred to a future milestone.

8. **MIP size in exclusive mode.** Binary variables are added per
   (battery, period, contingency), making the MIP $|\mathcal{K}|$ times
   larger. May require LP relaxation for the corrective sub-problems.

---

## 14. Appendix: integration points with existing modules

| Existing module | Stage 5F use |
|-----------------|-------------|
| `gridmind/network.py` | `Network`, `Generator`, `Line`, `Node` — reused unchanged |
| `gridmind/multiperiod.py` | `ScheduleProblem`, `Horizon`, `Battery`, `constant_horizon` — reused unchanged |
| `gridmind/scheduling.py` | `build_schedule_model` is NOT directly reused (monolithic model is new); `ScheduleResult` structure is referenced for naming conventions |
| `gridmind/solver.py` | `resolve_solver`, `SolverUnavailableError` — reused unchanged |
| `gridmind/validation.py` | `Check`, `ValidationReport` — reused; new `verify_contingency_schedule` added |

No existing public API is removed or changed. All new code is additive.

---

## Stage 5F.1 Completion Report

**Deliverable:** This specification document (`STAGE5F_SPEC.md`).

**Files changed:** None (documentation only).

**Mathematical decisions made:**
1. Two-level preventive + corrective formulation with shared preventive schedule.
2. Corrective ramp limits reuse existing `ramp_up_mw`/`ramp_down_mw` parameters.
3. Battery corrective stage starts from initial SoC (assumption A2).
4. Monolithic Pyomo model recommended for 5F.3 initial implementation.
5. Objective includes both preventive and contingency cost components, with $1/|\mathcal{K}|$ normalization.
6. Two security modes: strict (zero unmet demand required) and penalized.
7. N-1 security is decided by the **independent verifier**, not by solver status alone.

**Open questions for review:**
- Assumption A1: corrective ramp as same-period bound vs. a faster emergency limit.
- Assumption A2: independent battery restart vs. coupled SoC from preventive trajectory.
- Objective normalization factor: $1/|\mathcal{K}|$ vs. equal weight per contingency.
- Whether to include corrective generation cost or only penalize unmet demand.
- Exclusive-mode MIP size management for large $|\mathcal{K}|$.

**Test baseline:** See test run results appended below (run after milestone 5F.1 is written).

**Next step:** 5F.2 — Contingency representation. **Do not proceed without explicit review approval.**
