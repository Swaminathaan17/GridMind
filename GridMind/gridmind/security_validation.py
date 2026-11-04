"""Independent verification engine for Security-Constrained Preventive Scheduling (SCPS).

Deliberately does NOT use Pyomo: re-evaluates all constraints, dynamics,
and objective components directly from original Network and Horizon data using
primitive Python arithmetic, so solver defects or formulation oversights cannot
silently pass.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from gridmind.validation import Check, ValidationReport, validate_schedule

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gridmind.security import Contingency, ContingencySchedule, SCPSProblem, SCPSResult

DEFAULT_VERIFY_TOL = 1e-5
DEFAULT_OBJ_REL_TOL = 1e-5


@dataclass
class ContingencyValidationReport:
    """Independent verification findings for a single contingency recourse schedule."""
    contingency_id: str
    passed: bool
    is_secure: bool                          # True iff max(unmet_energy) <= tol and physical_feasible
    unmet_energy_mwh: float
    physical_feasible: bool
    checks: list[Check] = field(default_factory=list)
    recomputed_redispatch_cost_usd: float = 0.0
    recomputed_unmet_penalty_usd: float = 0.0
    recomputed_battery_throughput_usd: float = 0.0
    recomputed_tiebreak_usd: float = 0.0
    recomputed_incremental_cost_usd: float = 0.0

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contingency_id": self.contingency_id,
            "passed": self.passed,
            "is_secure": self.is_secure,
            "unmet_energy_mwh": self.unmet_energy_mwh,
            "physical_feasible": self.physical_feasible,
            "checks": [vars(c).copy() for c in self.checks],
            "recomputed_redispatch_cost_usd": self.recomputed_redispatch_cost_usd,
            "recomputed_unmet_penalty_usd": self.recomputed_unmet_penalty_usd,
            "recomputed_battery_throughput_usd": self.recomputed_battery_throughput_usd,
            "recomputed_tiebreak_usd": self.recomputed_tiebreak_usd,
            "recomputed_incremental_cost_usd": self.recomputed_incremental_cost_usd,
        }


@dataclass
class SCPSValidationReport:
    """Comprehensive independent verification report for a monolithic SCPS solution."""
    passed: bool                             # True iff preventive and all contingencies pass
    physical_feasible: bool                  # All physical balance/flow/dispatch constraints satisfied
    model_valid: bool                        # Objective and cost breakdowns match recomputed values
    n1_secure: bool                          # True iff preventive valid AND all contingencies are secure
    preventive_report: ValidationReport      # Standard multiperiod ValidationReport for base schedule
    contingency_reports: dict[str, ContingencyValidationReport] = field(default_factory=dict)
    recomputed_preventive_cost_usd: float = 0.0
    recomputed_contingency_cost_usd: float = 0.0
    recomputed_total_objective_usd: float = 0.0
    checks: list[Check] = field(default_factory=list)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "warning" and not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "physical_feasible": self.physical_feasible,
            "model_valid": self.model_valid,
            "n1_secure": self.n1_secure,
            "preventive_report": self.preventive_report.to_dict() if self.preventive_report else None,
            "contingency_reports": {k: r.to_dict() for k, r in self.contingency_reports.items()},
            "recomputed_preventive_cost_usd": self.recomputed_preventive_cost_usd,
            "recomputed_contingency_cost_usd": self.recomputed_contingency_cost_usd,
            "recomputed_total_objective_usd": self.recomputed_total_objective_usd,
            "checks": [vars(c).copy() for c in self.checks],
        }


def _is_finite_series(series: Mapping[str, Sequence[float]]) -> bool:
    for row in series.values():
        for val in row:
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                return False
    return True


def verify_contingency_schedule(
    scps_problem: SCPSProblem,
    contingency: Contingency,
    sched: ContingencySchedule,
    p_dispatch: Mapping[str, Sequence[float]],
    p_charge: Mapping[str, Sequence[float]],
    p_discharge: Mapping[str, Sequence[float]],
    *,
    tol: float = DEFAULT_VERIFY_TOL,
    obj_rel_tol: float = DEFAULT_OBJ_REL_TOL,
    allow_unmet: bool = True,
    battery_mode: str = "lp",
) -> ContingencyValidationReport:
    """Independently verify a single contingency recourse schedule against problem physics."""
    checks: list[Check] = []
    cid = contingency.id
    prob = scps_problem.problem
    net = prob.network
    hor = prob.horizon
    T = list(range(scps_problem.periods))
    dt = scps_problem.step_hours
    penalty = net.unmet_penalty_per_mwh
    eps = prob.tiebreak_epsilon

    gens = [g.id for g in net.generators]
    lines = [l.id for l in net.lines]
    nodes = [n.id for n in net.nodes]
    batts = [b.id for b in prob.batteries]

    # 1. Structure, completeness & finite values
    missing = (
        [g for g in gens if g not in sched.dispatch]
        + [l for l in lines if l not in sched.flows]
        + [n for n in nodes if n not in sched.unmet]
        + [b for b in batts if not (b in sched.charge and b in sched.discharge and b in sched.soc)]
    )
    checks.append(Check(
        f"{cid}:completeness", not missing, float(len(missing)),
        f"missing values for: {missing}" if missing else ""
    ))
    if missing:
        return ContingencyValidationReport(
            contingency_id=cid, passed=False, is_secure=False, unmet_energy_mwh=float("inf"),
            physical_feasible=False, checks=checks
        )

    bad_len = (
        [g for g in gens if len(sched.dispatch[g]) != len(T)]
        + [l for l in lines if len(sched.flows[l]) != len(T)]
        + [n for n in nodes if len(sched.unmet[n]) != len(T)]
        + [b for b in batts if not (len(sched.charge[b]) == len(sched.discharge[b]) == len(sched.soc[b]) == len(T))]
    )
    checks.append(Check(
        f"{cid}:horizon_length", not bad_len, float(len(bad_len)),
        f"wrong number of periods for: {bad_len}" if bad_len else ""
    ))
    if bad_len:
        return ContingencyValidationReport(
            contingency_id=cid, passed=False, is_secure=False, unmet_energy_mwh=float("inf"),
            physical_feasible=False, checks=checks
        )

    finite_ok = (
        _is_finite_series(sched.dispatch)
        and _is_finite_series(sched.flows)
        and _is_finite_series(sched.unmet)
        and _is_finite_series(sched.charge)
        and _is_finite_series(sched.discharge)
        and _is_finite_series(sched.soc)
    )
    checks.append(Check(
        f"{cid}:finite_values", finite_ok, 0.0 if finite_ok else 1.0,
        "all numerical values must be finite numbers"
    ))
    if not finite_ok:
        return ContingencyValidationReport(
            contingency_id=cid, passed=False, is_secure=False, unmet_energy_mwh=float("inf"),
            physical_feasible=False, checks=checks
        )

    # 2. Outaged element zero output / zero flow
    if contingency.kind == "generator":
        gid = contingency.component_id
        outage_viol = max((abs(sched.dispatch[gid][t]) for t in T), default=0.0)
        checks.append(Check(
            f"{cid}:outaged_generator_zero_output",
            outage_viol <= tol, outage_viol,
            f"tripped generator {gid} output must equal 0.0 MW"
        ))
    elif contingency.kind == "line":
        lid = contingency.component_id
        outage_viol = max((abs(sched.flows[lid][t]) for t in T), default=0.0)
        checks.append(Check(
            f"{cid}:outaged_line_zero_flow",
            outage_viol <= tol, outage_viol,
            f"tripped line {lid} flow must equal 0.0 MW"
        ))

    # 3. Surviving generator capacities and corrective ramp limits
    gen_cap_viol = 0.0
    corr_ramp_viol = 0.0
    for g_obj in net.generators:
        gid = g_obj.id
        is_tripped = (contingency.kind == "generator" and contingency.component_id == gid)
        if not is_tripped:
            for t in T:
                limit = hor.generation_limit(g_obj, t)
                gen_cap_viol = max(gen_cap_viol, -sched.dispatch[gid][t], sched.dispatch[gid][t] - limit)
                # Corrective ramp relative to preventive dispatch at the same period
                if g_obj.ramp_up_mw is not None:
                    corr_ramp_viol = max(corr_ramp_viol, sched.dispatch[gid][t] - p_dispatch[gid][t] - g_obj.ramp_up_mw)
                if g_obj.ramp_down_mw is not None:
                    corr_ramp_viol = max(corr_ramp_viol, p_dispatch[gid][t] - sched.dispatch[gid][t] - g_obj.ramp_down_mw)

    checks.append(Check(
        f"{cid}:surviving_generator_limits",
        gen_cap_viol <= tol, gen_cap_viol,
        "0 <= p^k[g,t] <= min(capacity, availability) * online[g,t] for surviving generators"
    ))
    checks.append(Check(
        f"{cid}:corrective_ramp_limits",
        corr_ramp_viol <= tol, corr_ramp_viol,
        "|p^k[g,t] - p^0[g,t]| within ramp limits for surviving generators (tripped generator is exempt)"
    ))

    # 4. Surviving line capacities
    line_cap_viol = 0.0
    for l_obj in net.lines:
        lid = l_obj.id
        is_tripped = (contingency.kind == "line" and contingency.component_id == lid)
        if not is_tripped:
            for t in T:
                limit = hor.line_limit(l_obj, t)
                line_cap_viol = max(line_cap_viol, abs(sched.flows[lid][t]) - limit)

    checks.append(Check(
        f"{cid}:surviving_line_capacities",
        line_cap_viol <= tol, line_cap_viol,
        "|f^k[l,t]| <= capacity[l,t] for surviving lines"
    ))

    # 5. Unmet demand bounds
    unmet_viol = max(
        (max(-sched.unmet[n][t], sched.unmet[n][t] - hor.demand(n, t)) for n in nodes for t in T),
        default=0.0
    )
    checks.append(Check(
        f"{cid}:unmet_bounds",
        unmet_viol <= tol, unmet_viol,
        "0 <= u^k[n,t] <= demand[n,t]"
    ))
    if not allow_unmet:
        strict_viol = max((sched.unmet[n][t] for n in nodes for t in T), default=0.0)
        checks.append(Check(
            f"{cid}:strict_mode_zero_unmet",
            strict_viol <= tol, strict_viol,
            "strict security mode forbids recourse unmet demand"
        ))

    # 6. Recourse nodal power balance
    balance_viol = 0.0
    for n in nodes:
        for t in T:
            gen_at = sum(sched.dispatch[g][t] for g in gens if net.generator(g).node == n)
            inflow = sum(sched.flows[l][t] for l in lines if net.line(l).to_node == n)
            outflow = sum(sched.flows[l][t] for l in lines if net.line(l).from_node == n)
            dis_at = sum(sched.discharge[b][t] for b in batts if prob.battery(b).node == n)
            ch_at = sum(sched.charge[b][t] for b in batts if prob.battery(b).node == n)
            res = gen_at + inflow - outflow + dis_at - ch_at + sched.unmet[n][t] - hor.demand(n, t)
            balance_viol = max(balance_viol, abs(res))

    checks.append(Check(
        f"{cid}:power_balance",
        balance_viol <= tol, balance_viol,
        "generation + inflow - outflow + discharge - charge + unmet = demand under contingency topology"
    ))

    # 7. Battery recourse dynamics (shared initial SoC E0; NO terminal SoC in contingency)
    batt_viol = soc_viol = sim_viol = 0.0
    for b_obj in prob.batteries:
        bid = b_obj.id
        prev = b_obj.initial_soc_mwh  # SHARED INITIAL SOC
        for t in T:
            batt_viol = max(
                batt_viol,
                sched.charge[bid][t] - b_obj.max_charge_mw,
                sched.discharge[bid][t] - b_obj.max_discharge_mw,
                -sched.charge[bid][t],
                -sched.discharge[bid][t],
            )
            expected = prev + b_obj.eta_charge * sched.charge[bid][t] * dt - sched.discharge[bid][t] * dt / b_obj.eta_discharge
            soc_viol = max(
                soc_viol,
                -sched.soc[bid][t],
                sched.soc[bid][t] - b_obj.capacity_mwh,
                abs(sched.soc[bid][t] - expected),
            )
            sim_viol = max(sim_viol, min(sched.charge[bid][t], sched.discharge[bid][t]))
            prev = sched.soc[bid][t]
        soc_viol = max(soc_viol, b_obj.min_soc_mwh - min(sched.soc[bid], default=b_obj.min_soc_mwh))

    checks.append(Check(
        f"{cid}:battery_power_limits",
        batt_viol <= tol, batt_viol,
        "0 <= charge <= max_charge; 0 <= discharge <= max_discharge"
    ))
    checks.append(Check(
        f"{cid}:battery_soc_dynamics",
        soc_viol <= tol, soc_viol,
        "recourse SoC begins from shared E0; respects bounds and dynamic efficiency; exempt from terminal SoC"
    ))
    if battery_mode == "exclusive":
        checks.append(Check(
            f"{cid}:battery_exclusivity",
            sim_viol <= tol, sim_viol,
            "min(charge, discharge) == 0 in exclusive mode",
            category="exclusivity"
        ))
    else:
        checks.append(Check(
            f"{cid}:battery_exclusivity",
            True, sim_viol,
            "simultaneous charge/discharge advisory in LP mode",
            severity="warning", category="exclusivity"
        ))

    # 8. Independent Incremental Recourse Cost Recomputation
    recomp_redisp = dt * sum(net.generator(g).cost_per_mwh * (sched.dispatch[g][t] - p_dispatch[g][t]) for g in gens for t in T)
    recomp_unmet = dt * penalty * sum(sched.unmet[n][t] for n in nodes for t in T)
    recomp_batt = dt * sum(prob.battery(b).throughput_cost_per_mwh * (sched.discharge[b][t] - p_discharge[b][t]) for b in batts for t in T)
    recomp_tb = eps * dt * sum((sched.charge[b][t] + sched.discharge[b][t]) - (p_charge[b][t] + p_discharge[b][t]) for b in batts for t in T)
    recomp_incr_total = recomp_redisp + recomp_unmet + recomp_batt + recomp_tb

    # Consistency checks against reported costs in ContingencySchedule
    def check_cost(name: str, recomputed: float, reported: float | None) -> None:
        if reported is not None:
            diff = abs(recomputed - reported)
            scale = max(1.0, abs(reported), abs(recomputed))
            rel = diff / scale
            checks.append(Check(
                f"{cid}:{name}_consistency",
                rel <= obj_rel_tol or diff <= tol, diff,
                f"recomputed {recomputed:.4f} vs reported {reported:.4f}",
                category="model"
            ))

    check_cost("redispatch_cost", recomp_redisp, sched.redispatch_cost_usd)
    check_cost("unmet_penalty", recomp_unmet, sched.unmet_penalty_cost_usd)
    check_cost("battery_throughput", recomp_batt, sched.battery_throughput_cost_usd)
    check_cost("tiebreak", recomp_tb, sched.tiebreak_cost_usd)
    check_cost("incremental_cost", recomp_incr_total, sched.incremental_cost_usd)

    # 9. Independent Unmet Energy & Security Determination
    recomp_unmet_energy = dt * sum(sum(sched.unmet[n]) for n in nodes)
    check_cost("unmet_energy", recomp_unmet_energy, sched.unmet_energy_mwh)

    phys_checks = [c for c in checks if c.category == "physical"]
    excl_checks = [c for c in checks if c.category == "exclusivity" and c.severity == "error"]
    is_phys_feasible = all(c.passed for c in phys_checks) and all(c.passed for c in excl_checks)
    is_sec = is_phys_feasible and (recomp_unmet_energy <= tol)

    all_passed = all(c.passed for c in checks if c.severity == "error")

    return ContingencyValidationReport(
        contingency_id=cid,
        passed=all_passed,
        is_secure=is_sec,
        unmet_energy_mwh=recomp_unmet_energy,
        physical_feasible=is_phys_feasible,
        checks=checks,
        recomputed_redispatch_cost_usd=recomp_redisp,
        recomputed_unmet_penalty_usd=recomp_unmet,
        recomputed_battery_throughput_usd=recomp_batt,
        recomputed_tiebreak_usd=recomp_tb,
        recomputed_incremental_cost_usd=recomp_incr_total,
    )


def verify_scps_result(
    scps_problem: SCPSProblem,
    scps_result: SCPSResult,
    *,
    tol: float = DEFAULT_VERIFY_TOL,
    obj_rel_tol: float = DEFAULT_OBJ_REL_TOL,
    battery_mode: str | None = None,
) -> SCPSValidationReport:
    """Independently verify an SCPSResult against the original SCPSProblem definition."""
    top_checks: list[Check] = []
    bat_mode = battery_mode or getattr(scps_result, "battery_mode", "lp")
    allow_unmet = not getattr(scps_result, "strict", False)

    # 1. Handle non-optimal or failed results safely
    if scps_result.status != "optimal":
        top_checks.append(Check(
            "optimization_success", False, 1.0,
            f"cannot verify non-optimal solution (status='{scps_result.status}', termination='{scps_result.termination_condition}')",
            severity="error"
        ))
        empty_prev = ValidationReport(passed=False, physical_feasible=False, model_valid=False)
        return SCPSValidationReport(
            passed=False, physical_feasible=False, model_valid=False, n1_secure=False,
            preventive_report=empty_prev, contingency_reports={}, checks=top_checks
        )

    prev_res = scps_result.preventive_result
    if prev_res is None or not prev_res.dispatch:
        top_checks.append(Check(
            "preventive_result_present", False, 1.0,
            "preventive_result is missing or empty in SCPSResult", severity="error"
        ))
        empty_prev = ValidationReport(passed=False, physical_feasible=False, model_valid=False)
        return SCPSValidationReport(
            passed=False, physical_feasible=False, model_valid=False, n1_secure=False,
            preventive_report=empty_prev, contingency_reports={}, checks=top_checks
        )

    # 2. Phase 1: Validate preventive schedule using standard multiperiod validator
    prev_report = validate_schedule(
        problem=scps_problem.problem,
        dispatch=prev_res.dispatch,
        flows=prev_res.flows,
        unmet=prev_res.unmet,
        charge=prev_res.charge,
        discharge=prev_res.discharge,
        soc=prev_res.soc,
        reported_objective=prev_res.objective,
        tol=tol,
        allow_unmet=allow_unmet,
        battery_mode=bat_mode,
    )
    for c in prev_report.checks:
        top_checks.append(Check(f"preventive:{c.name}", c.passed, c.max_violation, c.detail, c.severity, c.category))

    # 3. Independent Preventive Cost Recomputation
    net = scps_problem.network
    prob = scps_problem.problem
    dt = scps_problem.step_hours
    penalty = net.unmet_penalty_per_mwh
    eps = prob.tiebreak_epsilon
    T = list(range(scps_problem.periods))
    gens = [g.id for g in net.generators]
    nodes = [n.id for n in net.nodes]
    batts = [b.id for b in prob.batteries]

    recomp_prev_gen = dt * sum(net.generator(g).cost_per_mwh * prev_res.dispatch[g][t] for g in gens for t in T)
    recomp_prev_unmet = dt * penalty * sum(prev_res.unmet[n][t] for n in nodes for t in T)
    recomp_prev_batt = dt * sum(prob.battery(b).throughput_cost_per_mwh * prev_res.discharge[b][t] for b in batts for t in T)
    recomp_prev_tb = eps * dt * sum(prev_res.charge[b][t] + prev_res.discharge[b][t] for b in batts for t in T)
    recomp_prev_op_cost = recomp_prev_gen + recomp_prev_unmet + recomp_prev_batt + recomp_prev_tb

    def check_top_cost(name: str, recomputed: float, reported: float | None) -> None:
        if reported is not None:
            diff = abs(recomputed - reported)
            scale = max(1.0, abs(reported), abs(recomputed))
            rel = diff / scale
            top_checks.append(Check(
                f"preventive_{name}_consistency",
                rel <= obj_rel_tol or diff <= tol, diff,
                f"recomputed {recomputed:.4f} vs reported {reported:.4f}",
                category="model"
            ))

    check_top_cost("generation_cost", recomp_prev_gen, scps_result.preventive_generation_cost_usd)
    check_top_cost("unmet_penalty", recomp_prev_unmet, scps_result.preventive_unmet_penalty_usd)
    check_top_cost("battery_throughput", recomp_prev_batt, scps_result.preventive_battery_throughput_usd)
    check_top_cost("tiebreak", recomp_prev_tb, scps_result.preventive_tiebreak_usd)
    check_top_cost("operating_cost", recomp_prev_op_cost - recomp_prev_tb, scps_result.preventive_operating_cost_usd)

    # 4. Phase 2: Validate all contingencies independently
    c_reports: dict[str, ContingencyValidationReport] = {}
    prob_conts = [c.id for c in scps_problem.contingency_set]
    res_conts = set(scps_result.contingencies.keys())

    missing_conts = [cid for cid in prob_conts if cid not in res_conts]
    top_checks.append(Check(
        "contingency_completeness", not missing_conts, float(len(missing_conts)),
        f"missing contingency schedules in result for: {missing_conts}" if missing_conts else ""
    ))

    for c_obj in scps_problem.contingency_set:
        cid = c_obj.id
        if cid in scps_result.contingencies:
            c_sched = scps_result.contingencies[cid]
            c_rep = verify_contingency_schedule(
                scps_problem=scps_problem,
                contingency=c_obj,
                sched=c_sched,
                p_dispatch=prev_res.dispatch,
                p_charge=prev_res.charge,
                p_discharge=prev_res.discharge,
                tol=tol,
                obj_rel_tol=obj_rel_tol,
                allow_unmet=allow_unmet,
                battery_mode=bat_mode,
            )
            c_reports[cid] = c_rep
            for c in c_rep.checks:
                top_checks.append(c)

    # 5. Phase 3: Aggregation & Total Objective Consistency
    K_len = len(scps_problem.contingency_set)
    if K_len > 0 and len(c_reports) == K_len:
        recomp_avg_incr = (1.0 / K_len) * sum(r.recomputed_incremental_cost_usd for r in c_reports.values())
        recomp_total_obj = recomp_prev_op_cost + recomp_avg_incr

        check_top_cost("contingency_incremental_cost", recomp_avg_incr, scps_result.contingency_cost_usd)
        check_top_cost("total_objective", recomp_total_obj, scps_result.total_objective_usd)
    else:
        recomp_avg_incr = float("nan")
        recomp_total_obj = float("nan")
        top_checks.append(Check("contingency_aggregation", False, 1.0, "cannot aggregate incomplete contingency schedules"))

    # 6. Overall Physical Feasibility, Model Validity, and N-1 Security Synthesis
    phys_checks = [c for c in top_checks if c.category == "physical"]
    excl_checks = [c for c in top_checks if c.category == "exclusivity" and c.severity == "error"]
    model_checks = [c for c in top_checks if c.category == "model"]

    is_physical = all(c.passed for c in phys_checks) and all(c.passed for c in excl_checks)
    is_model_valid = all(c.passed for c in model_checks)

    # N-1 security requires preventive physical feasibility AND zero unmet demand across all contingencies
    all_conts_secure = (len(c_reports) == K_len) and all(r.is_secure for r in c_reports.values())
    prev_unmet_energy = dt * sum(sum(prev_res.unmet[n]) for n in nodes)
    is_n1_secure = is_physical and (prev_unmet_energy <= tol) and all_conts_secure

    all_passed = all(c.passed for c in top_checks if c.severity == "error")

    return SCPSValidationReport(
        passed=all_passed,
        physical_feasible=is_physical,
        model_valid=is_model_valid,
        n1_secure=is_n1_secure,
        preventive_report=prev_report,
        contingency_reports=c_reports,
        recomputed_preventive_cost_usd=recomp_prev_op_cost - recomp_prev_tb,
        recomputed_contingency_cost_usd=recomp_avg_incr,
        recomputed_total_objective_usd=recomp_total_obj,
        checks=top_checks,
    )
