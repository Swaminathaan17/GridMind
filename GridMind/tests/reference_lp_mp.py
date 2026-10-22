"""Independent multi-period reference LP built with scipy.optimize.linprog (HiGHS).

Shares NO code with gridmind.scheduling / gridmind.optimizer; it re-derives the
formulation straight from the ScheduleProblem data so the Pyomo model can be
cross-checked on small instances. Objective and constraints mirror the
documented maths: generation cost + unmet penalty + battery throughput cost,
plus the same epsilon tie-break term.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import linprog

from gridmind.multiperiod import ScheduleProblem


def reference_solve_mp(problem: ScheduleProblem, allow_unmet: bool = True) -> dict[str, Any]:
    """Solve the multi-period LP independently.

    Returns a dict with status, objective (incl. tie-break), operating_cost_usd,
    dispatch, flows, unmet, charge, discharge, soc. Non-optimal outcomes carry
    no solution values.
    """
    net, hor, dt = problem.network, problem.horizon, problem.step_hours
    T = list(range(problem.periods))
    G = [g.id for g in net.generators]
    L = [l.id for l in net.lines]
    N = [n.id for n in net.nodes]
    B = [b.id for b in problem.batteries]
    nG, nL, nN, nB, nT = len(G), len(L), len(N), len(B), len(T)

    # variable layout: p (nG*nT), f (nL*nT), u (nN*nT), ch (nB*nT), dis (nB*nT), e (nB*nT)
    def sl(idx: int, t: int) -> int:
        return idx * nT + t

    off_f = nG * nT
    off_u = off_f + nL * nT
    off_ch = off_u + nN * nT
    off_dis = off_ch + nB * nT
    off_e = off_dis + nB * nT
    nvar = off_e + nB * nT

    penalty = net.unmet_penalty_per_mwh
    thr = {b.id: b.throughput_cost_per_mwh for b in problem.batteries}
    eps = problem.tiebreak_epsilon
    c = np.zeros(nvar)
    for j, g in enumerate(G):
        for t in T:
            c[sl(j, t)] = dt * net.generator(g).cost_per_mwh
    for t in T:
        for i in range(nN):
            c[off_u + sl(i, t)] = dt * penalty
        for k in range(nB):
            c[off_ch + sl(k, t)] = eps * dt
            c[off_dis + sl(k, t)] = dt * thr[B[k]] + eps * dt

    # bounds
    bounds: list[tuple[float | None, float | None]] = [(0.0, None)] * nvar
    for j, g in enumerate(G):
        for t in T:
            bounds[sl(j, t)] = (0.0, hor.generation_limit(net.generator(g), t))
    for k, l in enumerate(L):
        for t in T:
            cap = hor.line_limit(net.line(l), t)
            bounds[off_f + sl(k, t)] = (-cap, cap)
    for i, n in enumerate(N):
        for t in T:
            bounds[off_u + sl(i, t)] = (0.0, hor.demand(n, t) if allow_unmet else 0.0)
    for k, b in enumerate(problem.batteries):
        for t in T:
            bounds[off_ch + sl(k, t)] = (0.0, b.max_charge_mw)
            bounds[off_dis + sl(k, t)] = (0.0, b.max_discharge_mw)
            bounds[off_e + sl(k, t)] = (b.min_soc_mwh, b.capacity_mwh)

    # equality constraints: node balance + SoC dynamics (+ terminal SoC)
    gens_at = {n: [g.id for g in net.generators if g.node == n] for n in N}
    into = {n: [l.id for l in net.lines if l.to_node == n] for n in N}
    out_of = {n: [l.id for l in net.lines if l.from_node == n] for n in N}
    batts_at = {n: [b.id for b in problem.batteries if b.node == n] for n in N}
    with_final = [(k, b) for k, b in enumerate(problem.batteries) if b.final_soc_mwh is not None]

    n_eq = nN * nT + nB * nT + len(with_final)
    A_eq = np.zeros((n_eq, nvar))
    b_eq = np.zeros(n_eq)
    row = 0
    for t in T:
        for i, n in enumerate(N):
            for gid in gens_at[n]:
                A_eq[row, sl(G.index(gid), t)] += 1.0
            for lid in into[n]:
                A_eq[row, off_f + sl(L.index(lid), t)] += 1.0
            for lid in out_of[n]:
                A_eq[row, off_f + sl(L.index(lid), t)] -= 1.0
            for bid in batts_at[n]:
                k = B.index(bid)
                A_eq[row, off_dis + sl(k, t)] += 1.0
                A_eq[row, off_ch + sl(k, t)] -= 1.0
            A_eq[row, off_u + sl(i, t)] += 1.0
            b_eq[row] = hor.demand(n, t)
            row += 1
    for k, b in enumerate(problem.batteries):
        for t in T:
            A_eq[row, off_e + sl(k, t)] += 1.0
            if t == 0:
                b_eq[row] = b.initial_soc_mwh
            else:
                A_eq[row, off_e + sl(k, t - 1)] -= 1.0
            A_eq[row, off_ch + sl(k, t)] -= b.eta_charge * dt
            A_eq[row, off_dis + sl(k, t)] += dt / b.eta_discharge
            row += 1
    for k, b in with_final:
        A_eq[row, off_e + sl(k, nT - 1)] = 1.0
        b_eq[row] = b.final_soc_mwh
        row += 1

    # ramp inequalities: A_ub x <= b_ub (all other bounds are variable bounds)
    rows: list[np.ndarray] = []
    rhs: list[float] = []

    def add(entries: dict[int, float], bound: float) -> None:
        vec = np.zeros(nvar)
        for col, val in entries.items():
            vec[col] = val
        rows.append(vec)
        rhs.append(bound)

    for g_obj in net.generators:
        j = G.index(g_obj.id)
        for t in T[1:]:
            if g_obj.ramp_up_mw is not None:
                add({sl(j, t): 1.0, sl(j, t - 1): -1.0}, g_obj.ramp_up_mw)
            if g_obj.ramp_down_mw is not None:
                add({sl(j, t): -1.0, sl(j, t - 1): 1.0}, g_obj.ramp_down_mw)
        init = problem.initial_dispatch_mw.get(g_obj.id)
        if init is not None:
            if g_obj.ramp_up_mw is not None:
                add({sl(j, 0): 1.0}, g_obj.ramp_up_mw + init)
            if g_obj.ramp_down_mw is not None:
                add({sl(j, 0): -1.0}, g_obj.ramp_down_mw - init)

    A_ub = np.array(rows) if rows else None
    b_ub = np.array(rhs) if rhs else None
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    out: dict[str, Any] = {"status": "optimal" if res.success else "failed",
                           "message": res.message}
    if not res.success or res.x is None:
        return out
    x = res.x

    dispatch = {g: [float(x[sl(j, t)]) for t in T] for j, g in enumerate(G)}
    flows = {l: [float(x[off_f + sl(k, t)]) for t in T] for k, l in enumerate(L)}
    unmet = {n: [float(x[off_u + sl(i, t)]) for t in T] for i, n in enumerate(N)}
    charge = {b: [float(x[off_ch + sl(k, t)]) for t in T] for k, b in enumerate(B)}
    discharge = {b: [float(x[off_dis + sl(k, t)]) for t in T] for k, b in enumerate(B)}
    soc = {b: [float(x[off_e + sl(k, t)]) for t in T] for k, b in enumerate(B)}

    gen_cost = dt * sum(net.generator(g).cost_per_mwh * v for g, s in dispatch.items() for v in s)
    pen_cost = dt * penalty * sum(v for s in unmet.values() for v in s)
    thr_cost = dt * sum(thr[b] * v for b, s in discharge.items() for v in s)
    out.update({"objective": float(res.fun),
                "operating_cost_usd": gen_cost + pen_cost + thr_cost,
                "generation_cost_usd": gen_cost, "unmet_penalty_cost_usd": pen_cost,
                "battery_throughput_cost_usd": thr_cost,
                "dispatch": dispatch, "flows": flows, "unmet": unmet,
                "charge": charge, "discharge": discharge, "soc": soc})
    return out
