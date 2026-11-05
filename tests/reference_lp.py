"""Independent reference LP built directly with scipy.optimize.linprog (HiGHS).
Shares NO code with gridmind.optimizer; used to cross-check Pyomo/GLPK objectives."""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

from gridmind.network import Network


def reference_solve(network: Network, allow_unmet: bool = True):
    G, L, N = network.generators, network.lines, network.nodes
    nG, nL, nN = len(G), len(L), len(N)
    c = np.concatenate([[g.cost_per_mwh for g in G], np.zeros(nL),
                        np.full(nN, network.unmet_penalty_per_mwh)]) * network.interval_hours
    A = np.zeros((nN, nG + nL + nN))
    b = np.array([n.demand_mw for n in N])
    idx = {n.id: i for i, n in enumerate(N)}
    for j, g in enumerate(G):
        A[idx[g.node], j] += 1
    for k, l in enumerate(L):
        A[idx[l.to_node], nG + k] += 1
        A[idx[l.from_node], nG + k] -= 1
    for i in range(nN):
        A[i, nG + nL + i] = 1
    bounds = ([(0, g.effective_limit_mw) for g in G]
              + [(-l.effective_capacity_mw, l.effective_capacity_mw) for l in L]
              + [(0, n.demand_mw if allow_unmet else 0.0) for n in N])
    res = linprog(c, A_eq=A, b_eq=b, bounds=bounds, method="highs")
    return res
