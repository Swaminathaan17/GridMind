import os

import pytest

from gridmind.optimizer import ENV_SOLVER, ENV_SOLVER_PATH, solve_dispatch
from gridmind.solver import (MIP_SOLVER_CANDIDATES, SolverUnavailableError,
                             _MIP_PROBE_CACHE, _probe_mip, resolve_solver,
                             solver_available, solver_inventory)
from helpers import tiny_network


def _patch_probe_values(monkeypatch, x, y, status=None, term=None, load_ok=True):
    """Wire a fake solver into _probe_mip.

    The fake sets the binary variable values during ``solve`` (as a real solver
    interface would populate a solution snapshot) and we neutralise the model's
    solution-loading call, so the probe exercises its status/termination/value
    checks without depending on Pyomo's exact results-container internals."""
    from pyomo.core.base.PyomoModel import ModelSolutions
    from pyomo.opt import SolverStatus, TerminationCondition

    status = status or SolverStatus.ok
    term = term or TerminationCondition.optimal

    class _Solver:
        pass

    class Results:
        pass

    Results.solver = _Solver()
    Results.solver.status = status
    Results.solver.termination_condition = term

    class FakeSolver:
        def solve(self, model, *a, **k):
            if x is not None:
                model.x.set_value(x)
            if y is not None:
                model.y.set_value(y)
            return Results()

    monkeypatch.setattr("gridmind.solver._make", lambda name: FakeSolver())
    if load_ok:
        monkeypatch.setattr(ModelSolutions, "load_from", lambda self, *a, **k: None)
    else:
        def boom(self, *a, **k):
            raise RuntimeError("backend cannot load solutions")
        monkeypatch.setattr(ModelSolutions, "load_from", boom)


def _with_env(**env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_missing_solver_executable_reports_unavailable_not_results():
    saved = _with_env(**{ENV_SOLVER_PATH: os.path.join("no", "such", "glpsol.exe")})
    try:
        res = solve_dispatch(tiny_network())
    finally:
        _restore(saved)
    assert res.status == "solver_unavailable" and not res.ok
    assert res.objective is None and res.dispatch == {} and res.flows == {}


def test_unknown_solver_name_reports_unavailable():
    saved = _with_env(**{ENV_SOLVER: "definitely_not_a_solver"})
    try:
        res = solve_dispatch(tiny_network())
    finally:
        _restore(saved)
    assert res.status == "solver_unavailable" and res.objective is None


def test_invalid_network_is_an_error_not_a_crash():
    from dataclasses import replace
    from gridmind.network import Line
    net = tiny_network()
    bad = replace(net, lines=(Line("AB", "A-B", "A", "ZZZ", 10.0),))
    res = solve_dispatch(bad)
    assert res.status in ("error", "solver_unavailable") and res.objective is None


# ------------------------------------------------------ solver capability ----
def test_mip_candidates_declared():
    assert MIP_SOLVER_CANDIDATES == ("glpk", "cbc", "appsi_highs")


def test_inventory_preserves_keys_and_adds_supports_mip():
    for entry in solver_inventory():
        assert {"solver", "available", "detail"} <= set(entry)
        assert "supports_mip" in entry
        if not entry["available"]:
            assert entry["supports_mip"] is None
        else:
            assert isinstance(entry["supports_mip"], bool)


def test_unavailable_solver_reports_supports_mip_none():
    saved = _with_env(**{ENV_SOLVER_PATH: os.path.join("no", "such", "glpsol.exe")})
    try:
        inv = solver_inventory()
    finally:
        _restore(saved)
    assert inv and inv[0]["available"] is False
    assert inv[0]["supports_mip"] is None


def test_explicit_invalid_solver_mip_raises_without_fallback():
    saved = _with_env(**{ENV_SOLVER: "definitely_not_a_solver"})
    try:
        with pytest.raises(SolverUnavailableError):
            resolve_solver(mip=True)
        ok, msg = solver_available(mip=True)
        assert ok is False and "definitely_not_a_solver" in msg
    finally:
        _restore(saved)


def test_probe_caches_failures(monkeypatch):
    _MIP_PROBE_CACHE.pop("__fake__", None)
    calls = {"n": 0}

    def fake_make(name):
        calls["n"] += 1
        raise SolverUnavailableError("no such executable")

    monkeypatch.setattr("gridmind.solver._make", fake_make)
    assert _probe_mip("__fake__")[0] is False
    assert _probe_mip("__fake__")[0] is False
    assert calls["n"] == 1  # second call served from cache
    _MIP_PROBE_CACHE.pop("__fake__", None)


def test_probe_rejects_nonintegral(monkeypatch):
    """A solver that returns the LP relaxation (non-integral x=y=0.75) must fail
    the probe even with an apparently optimal termination."""
    _patch_probe_values(monkeypatch, 0.75, 0.75)
    _MIP_PROBE_CACHE.pop("__fake2__", None)
    ok, reason = _probe_mip("__fake2__")
    assert ok is False and "non-integral" in reason
    _MIP_PROBE_CACHE.pop("__fake2__", None)


def test_probe_rejects_bad_termination(monkeypatch):
    from pyomo.opt import TerminationCondition
    _patch_probe_values(monkeypatch, 1.0, 0.0, term=TerminationCondition.infeasible)
    _MIP_PROBE_CACHE.pop("__fake3__", None)
    ok, reason = _probe_mip("__fake3__")
    assert ok is False and "not optimal" in reason
    _MIP_PROBE_CACHE.pop("__fake3__", None)


def test_probe_rejects_wrong_objective(monkeypatch):
    """Integral point but LP-optimal objective (-1.5) is still rejected."""
    import pyomo.environ as pyo
    from pyomo.core.base.objective import Objective
    _patch_probe_values(monkeypatch, 1.0, 0.0)
    real_value = pyo.value

    def fake_value(expr, exception=False):
        try:
            if getattr(expr, "ctype", None) is Objective:
                return -1.5
        except Exception:
            pass
        return real_value(expr, exception=exception)

    monkeypatch.setattr(pyo, "value", fake_value)
    _MIP_PROBE_CACHE.pop("__fake5__", None)
    ok, reason = _probe_mip("__fake5__")
    assert ok is False and "objective" in reason
    _MIP_PROBE_CACHE.pop("__fake5__", None)


def test_probe_rejects_missing_values(monkeypatch):
    _patch_probe_values(monkeypatch, None, None)
    _MIP_PROBE_CACHE.pop("__fake6__", None)
    ok, reason = _probe_mip("__fake6__")
    assert ok is False
    _MIP_PROBE_CACHE.pop("__fake6__", None)


def test_probe_rejects_solution_loading_failure(monkeypatch):
    _patch_probe_values(monkeypatch, 1.0, 0.0, load_ok=False)
    _MIP_PROBE_CACHE.pop("__fake7__", None)
    ok, reason = _probe_mip("__fake7__")
    assert ok is False and "load solution" in reason
    _MIP_PROBE_CACHE.pop("__fake7__", None)


def test_probe_success_caches(monkeypatch):
    _patch_probe_values(monkeypatch, 1.0, 0.0)
    _MIP_PROBE_CACHE.pop("__fake4__", None)
    ok, reason = _probe_mip("__fake4__")
    assert ok is True
    assert "__fake4__" in _MIP_PROBE_CACHE
    _MIP_PROBE_CACHE.pop("__fake4__", None)


@pytest.mark.skipif(not solver_available(mip=True)[0],
                    reason=f"no verified MIP solver: {solver_available(mip=True)[1]}")
def test_real_mip_solver_reaches_integer_optimum():
    name, _ = resolve_solver(mip=True)
    ok, reason = _probe_mip(name)
    assert ok, reason


def test_solve_schedule_exclusive_invokes_mip_resolution(monkeypatch):
    """Exclusive mode must request a MIP-capable solver (``resolve_solver(mip=True)``);
    when no MIP solver is available it reports ``solver_unavailable`` rather than
    silently falling back to the LP path."""
    from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
    from gridmind.network import Generator, Network, Node
    from gridmind.scheduling import solve_schedule

    net = Network(name="x", nodes=(Node("b", "b"),),
                  generators=(Generator("g", "g", "b", "gas", 100.0, 1.0),),
                  lines=(), unmet_penalty_per_mwh=1000.0)
    hor = Horizon(periods=1, step_hours=1.0, demand_mw=({"b": 10.0},),
                  availability_mw=({"g": 100.0},), generator_online=({"g": True},),
                  line_in_service=({},))
    prob = ScheduleProblem(network=net, horizon=hor,
                           batteries=(Battery("b1", "b", 20.0, 10.0, 10.0),))
    seen = {}
    import gridmind.scheduling as sched

    def spy(mip=False):
        seen["mip"] = mip
        # Deterministically simulate "no MIP-capable solver" regardless of what
        # is installed, so this test checks the no-fallback contract, not the
        # machine's solver inventory.
        raise SolverUnavailableError("simulated: no MIP-capable solver available")

    monkeypatch.setattr(sched, "resolve_solver", spy)
    res = solve_schedule(prob, battery_mode="exclusive")
    assert seen.get("mip") is True
    assert res.status == "solver_unavailable"
    assert res.battery_mode == "exclusive"
    assert res.objective is None and res.charge == {}
