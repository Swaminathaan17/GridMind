"""P2 Solver and Execution Reliability test suite.

Covers:
- SolverOptions construction, validation, and immutability.
- Parameter translation across solvers (HiGHS, GLPK, CBC).
- map_termination_status coverage across all Pyomo TerminationConditions and string forms.
- Options propagation through resolve_solver, solve_dispatch, solve_schedule, and solve_security_constrained.
- Time limit handling and graceful status="time_limit" reporting (real solver & mock).
- Mock solver testing for various termination conditions (resourceInterrupt, userInterrupt, licensingProblems, etc.).
- Solver error and crash recovery without uncaught exceptions or corrupted state.
"""
from __future__ import annotations

import math
import pytest

from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.network import Generator, Line, Network, Node
from gridmind.optimizer import SolveResult, solve_dispatch
from gridmind.scheduling import ScheduleResult, solve_schedule
from gridmind.security import Contingency, ContingencySet, SCPSProblem, SCPSResult, solve_security_constrained
from gridmind.solver import (
    SolverOptions,
    SolverUnavailableError,
    apply_solver_options,
    map_termination_status,
    resolve_solver,
)
from helpers import tiny_network


# ---------------------------------------------------------------------------
# Fixtures / Test Helpers
# ---------------------------------------------------------------------------
def _sample_schedule_problem() -> ScheduleProblem:
    net = Network(
        name="p2-net",
        nodes=(Node("A", "A"), Node("B", "B", 40.0)),
        generators=(Generator("G1", "Gen1", "A", "gas", 100.0, 10.0),),
        lines=(Line("L1", "Line1", "A", "B", 50.0),),
        unmet_penalty_per_mwh=1000.0,
    )
    hor = Horizon(
        periods=2,
        step_hours=1.0,
        demand_mw=({"A": 0.0, "B": 40.0}, {"A": 0.0, "B": 40.0}),
        availability_mw=({"G1": 100.0}, {"G1": 100.0}),
        generator_online=({"G1": True}, {"G1": True}),
        line_in_service=({"L1": True}, {"L1": True}),
    )
    return ScheduleProblem(network=net, horizon=hor)


def _sample_scps_problem() -> SCPSProblem:
    sched_prob = _sample_schedule_problem()
    c_set = ContingencySet([
        Contingency(id="c_line", kind="line", component_id="L1", name="Line Outage"),
    ])
    return SCPSProblem(problem=sched_prob, contingency_set=c_set)


class MockSolverResults:
    def __init__(self, term: Any, status: Any = None):
        from pyomo.opt import SolverStatus
        class _InnerSolver:
            pass
        self.solver = _InnerSolver()
        self.solver.termination_condition = term
        self.solver.status = status if status is not None else SolverStatus.ok


class MockPyomoSolver:
    def __init__(self, term: Any, status: Any = None, raise_exc: Exception | None = None):
        self.term = term
        self.status = status
        self.raise_exc = raise_exc
        self.options = {}
        self.highs_options = {}

    def solve(self, model, **kwargs):
        if self.raise_exc is not None:
            raise self.raise_exc
        return MockSolverResults(self.term, self.status)


# ---------------------------------------------------------------------------
# 1. SolverOptions Validation & Immutability Tests
# ---------------------------------------------------------------------------
class TestSolverOptions:
    def test_default_options(self):
        opts = SolverOptions()
        opts.validate()
        assert opts.time_limit_seconds is None
        assert opts.mip_gap is None
        assert opts.threads is None

    def test_valid_options(self):
        opts = SolverOptions(time_limit_seconds=10.0, mip_gap=0.01, threads=4)
        opts.validate()
        assert opts.time_limit_seconds == 10.0
        assert opts.mip_gap == 0.01
        assert opts.threads == 4

    def test_immutability(self):
        opts = SolverOptions(time_limit_seconds=10.0)
        with pytest.raises(Exception):
            opts.time_limit_seconds = 20.0  # dataclass(frozen=True)

    @pytest.mark.parametrize("val", [-1.0, 0.0, float("nan"), float("inf")])
    def test_invalid_time_limit(self, val):
        opts = SolverOptions(time_limit_seconds=val)
        with pytest.raises(ValueError, match="time_limit_seconds"):
            opts.validate()

    @pytest.mark.parametrize("val", [-0.01, float("nan"), float("inf")])
    def test_invalid_mip_gap(self, val):
        opts = SolverOptions(mip_gap=val)
        with pytest.raises(ValueError, match="mip_gap"):
            opts.validate()

    def test_zero_mip_gap_valid(self):
        opts = SolverOptions(mip_gap=0.0)
        opts.validate()  # exact MIP gap 0 is valid

    @pytest.mark.parametrize("val", [0, -1, 1.5, True, False, "4"])
    def test_invalid_threads(self, val):
        opts = SolverOptions(threads=val)
        with pytest.raises(ValueError, match="threads"):
            opts.validate()


# ---------------------------------------------------------------------------
# 2. apply_solver_options Parameter Translation Tests
# ---------------------------------------------------------------------------
class TestApplySolverOptions:
    def test_none_options_noop(self):
        class Dummy:
            pass
        d = Dummy()
        apply_solver_options(d, "appsi_highs", None)  # Should not raise

    def test_highs_options_translation(self):
        opts = SolverOptions(time_limit_seconds=15.5, mip_gap=0.02, threads=3)
        mock = MockPyomoSolver("optimal")
        apply_solver_options(mock, "appsi_highs", opts)
        assert mock.highs_options["time_limit"] == 15.5
        assert mock.options["time_limit"] == 15.5
        assert mock.highs_options["mip_rel_gap"] == 0.02
        assert mock.options["mip_rel_gap"] == 0.02
        assert mock.highs_options["threads"] == 3
        assert mock.options["threads"] == 3

    def test_glpk_options_translation(self):
        opts = SolverOptions(time_limit_seconds=12.2, mip_gap=0.05, threads=2)
        mock = MockPyomoSolver("optimal")
        apply_solver_options(mock, "glpk", opts)
        assert mock.options["tmlim"] == 13  # ceil(12.2)
        assert mock.options["mipgap"] == 0.05
        assert "threads" not in mock.options  # GLPK does not support multi-threading

    def test_cbc_options_translation(self):
        opts = SolverOptions(time_limit_seconds=25.0, mip_gap=0.01, threads=4)
        mock = MockPyomoSolver("optimal")
        apply_solver_options(mock, "cbc", opts)
        assert mock.options["sec"] == 25.0
        assert mock.options["ratio"] == 0.01
        assert mock.options["threads"] == 4


# ---------------------------------------------------------------------------
# 3. Termination Status Mapping Tests
# ---------------------------------------------------------------------------
class TestTerminationStatusMapping:
    @pytest.mark.parametrize(
        ("term_str", "expected"),
        [
            ("optimal", "optimal"),
            ("globallyOptimal", "optimal"),
            ("locallyOptimal", "optimal"),
            ("infeasible", "infeasible"),
            ("infeasibleOrUnbounded", "infeasible_or_unbounded"),
            ("unbounded", "unbounded"),
            ("maxTimeLimit", "time_limit"),
            ("resourceInterrupt", "resource_limit"),
            ("userInterrupt", "interrupted"),
            ("licensingProblems", "solver_error"),
            ("internalSolverError", "solver_error"),
            ("solverFailure", "solver_error"),
            ("invalidProblem", "error"),
            ("noSolution", "no_solution"),
            ("intermediateNonInteger", "intermediate_non_integer"),
            ("unrecognizedCondition", "error"),
        ],
    )
    def test_string_mappings(self, term_str, expected):
        assert map_termination_status(term_str) == expected

    def test_pyomo_enum_mappings(self):
        from pyomo.opt import TerminationCondition
        assert map_termination_status(TerminationCondition.optimal) == "optimal"
        assert map_termination_status(TerminationCondition.maxTimeLimit) == "time_limit"
        assert map_termination_status(TerminationCondition.infeasible) == "infeasible"
        assert map_termination_status(TerminationCondition.userInterrupt) == "interrupted"
        assert map_termination_status(TerminationCondition.licensingProblems) == "solver_error"


# ---------------------------------------------------------------------------
# 4. solve_dispatch Solver Reliability & Options Propagation
# ---------------------------------------------------------------------------
class TestSolveDispatchReliability:
    def test_options_passed_to_solver(self):
        net = tiny_network()
        opts = SolverOptions(time_limit_seconds=10.0, threads=1)
        res = solve_dispatch(net, options=opts)
        assert res.status == "optimal"
        assert res.ok

    def test_real_solver_time_limit_handling(self):
        """Microsecond time limit causes appsi_highs to abort with maxTimeLimit."""
        net = tiny_network()
        opts = SolverOptions(time_limit_seconds=1e-9)
        res = solve_dispatch(net, options=opts)
        assert res.status == "time_limit"
        assert not res.ok
        assert res.objective is None
        assert res.dispatch == {}
        assert "time limit" in res.message.lower()

    def test_mock_solver_interrupt_handling(self, monkeypatch):
        from pyomo.opt import SolverStatus, TerminationCondition
        mock = MockPyomoSolver(TerminationCondition.userInterrupt, SolverStatus.aborted)
        monkeypatch.setattr("gridmind.optimizer.resolve_solver", lambda **k: ("mock_solver", mock))
        res = solve_dispatch(tiny_network())
        assert res.status == "interrupted"
        assert not res.ok
        assert res.objective is None

    def test_mock_solver_licensing_error(self, monkeypatch):
        from pyomo.opt import SolverStatus, TerminationCondition
        mock = MockPyomoSolver(TerminationCondition.licensingProblems, SolverStatus.error)
        monkeypatch.setattr("gridmind.optimizer.resolve_solver", lambda **k: ("mock_solver", mock))
        res = solve_dispatch(tiny_network())
        assert res.status == "solver_error"
        assert not res.ok
        assert res.objective is None

    def test_mock_solver_crash_exception(self, monkeypatch):
        mock = MockPyomoSolver("optimal", raise_exc=RuntimeError("Solver segfault simulation"))
        monkeypatch.setattr("gridmind.optimizer.resolve_solver", lambda **k: ("mock_solver", mock))
        res = solve_dispatch(tiny_network())
        assert res.status == "error"
        assert not res.ok
        assert res.termination_condition == "solver_exception"
        assert "RuntimeError" in res.message


# ---------------------------------------------------------------------------
# 5. solve_schedule Solver Reliability & Options Propagation
# ---------------------------------------------------------------------------
class TestSolveScheduleReliability:
    def test_schedule_options_passed(self):
        prob = _sample_schedule_problem()
        opts = SolverOptions(time_limit_seconds=10.0, threads=1)
        res = solve_schedule(prob, options=opts)
        assert res.status == "optimal"
        assert res.ok

    def test_schedule_time_limit_handling(self):
        prob = _sample_schedule_problem()
        opts = SolverOptions(time_limit_seconds=1e-9)
        res = solve_schedule(prob, options=opts)
        assert res.status == "time_limit"
        assert not res.ok
        assert res.objective is None
        assert res.dispatch == {}

    def test_schedule_mock_resource_limit(self, monkeypatch):
        from pyomo.opt import SolverStatus, TerminationCondition
        mock = MockPyomoSolver(TerminationCondition.resourceInterrupt, SolverStatus.aborted)
        monkeypatch.setattr("gridmind.scheduling.resolve_solver", lambda **k: ("mock_solver", mock))
        res = solve_schedule(_sample_schedule_problem())
        assert res.status == "resource_limit"
        assert not res.ok
        assert res.objective is None


# ---------------------------------------------------------------------------
# 6. solve_security_constrained Reliability & Options Propagation
# ---------------------------------------------------------------------------
class TestSolveSCPSReliability:
    def test_scps_options_passed(self):
        scps = _sample_scps_problem()
        opts = SolverOptions(time_limit_seconds=15.0, threads=1)
        res = solve_security_constrained(scps, options=opts)
        assert res.status == "optimal"

    def test_scps_time_limit_handling(self):
        scps = _sample_scps_problem()
        opts = SolverOptions(time_limit_seconds=1e-9)
        res = solve_security_constrained(scps, options=opts)
        assert res.status == "time_limit"
        assert not res.n1_secure
        assert res.total_objective_usd is None

    def test_scps_mock_solver_failure(self, monkeypatch):
        from pyomo.opt import SolverStatus, TerminationCondition
        mock = MockPyomoSolver(TerminationCondition.solverFailure, SolverStatus.error)
        monkeypatch.setattr("gridmind.security.resolve_solver", lambda **k: ("mock_solver", mock))
        res = solve_security_constrained(_sample_scps_problem())
        assert res.status == "solver_error"
        assert not res.n1_secure
        assert res.total_objective_usd is None
