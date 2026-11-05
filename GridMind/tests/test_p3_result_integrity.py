"""Focused tests for Phase P3: Result Integrity and Reproducibility.

Covers:
- ExecutionMetadata creation and dataclass integrity
- Backward-compatible metadata defaults across SolveResult, ScheduleResult, SCPSResult
- Automatic metadata capture in solve_dispatch, solve_schedule, solve_security_constrained
- Deterministic problem hashing across problem types
- Solver options recorded in provenance metadata
- Robust JSON serialization of results (safe with NaN, +Inf, -Inf, and non-optimal failures)
- Result completeness and consistency (no fabricated values on failure)
"""
from __future__ import annotations

import json
import math
import pytest

from gridmind.network import Generator, Line, Network, Node
from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.optimizer import SolveResult, solve_dispatch
from gridmind.provenance import ExecutionMetadata, capture_execution_metadata, compute_problem_hash, sanitize_for_serialization
from gridmind.scheduling import ScheduleResult, solve_schedule
from gridmind.security import Contingency, ContingencySet, SCPSProblem, SCPSResult, solve_security_constrained
from gridmind.solver import SolverOptions, resolve_solver, solver_available


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def simple_network() -> Network:
    return Network(
        name="test_p3_net",
        nodes=[Node("n1", "Node 1", demand_mw=50.0), Node("n2", "Node 2", demand_mw=30.0)],
        generators=[
            Generator("g1", "G1", "n1", "gas", 100.0, 25.0),
            Generator("g2", "G2", "n2", "solar", 50.0, 10.0, renewable=True),
        ],
        lines=[Line("l1", "L1", "n1", "n2", 100.0)],
    )


@pytest.fixture
def simple_schedule_problem(simple_network: Network) -> ScheduleProblem:
    hor = Horizon(
        periods=2,
        step_hours=1.0,
        demand_mw=({"n1": 40.0, "n2": 20.0}, {"n1": 50.0, "n2": 25.0}),
        availability_mw=({"g1": 100.0, "g2": 50.0}, {"g1": 100.0, "g2": 50.0}),
        generator_online=({"g1": True, "g2": True}, {"g1": True, "g2": True}),
        line_in_service=({"l1": True}, {"l1": True}),
    )
    batt = Battery("b1", "n1", 50.0, 20.0, 20.0, 0.9, 0.9, initial_soc_mwh=25.0)
    return ScheduleProblem(network=simple_network, horizon=hor, batteries=[batt])


@pytest.fixture
def simple_scps_problem(simple_schedule_problem: ScheduleProblem) -> SCPSProblem:
    cset = ContingencySet([
        Contingency("c_g1", "generator", "g1"),
        Contingency("c_l1", "line", "l1"),
    ])
    return SCPSProblem(problem=simple_schedule_problem, contingency_set=cset)


# -----------------------------------------------------------------------------
# 1. ExecutionMetadata Unit Tests
# -----------------------------------------------------------------------------
def test_execution_metadata_construction_and_to_dict():
    meta = ExecutionMetadata(
        timestamp_utc="2026-11-05T00:00:00Z",
        gridmind_version="0.2.0",
        solver_name="appsi_highs",
        solver_version="1.15.1",
        problem_hash="abc123def456",
        solver_options={"time_limit_seconds": 60.0, "threads": 2},
    )
    d = meta.to_dict()
    assert d["timestamp_utc"] == "2026-11-05T00:00:00Z"
    assert d["gridmind_version"] == "0.2.0"
    assert d["solver_name"] == "appsi_highs"
    assert d["solver_version"] == "1.15.1"
    assert d["problem_hash"] == "abc123def456"
    assert d["solver_options"] == {"time_limit_seconds": 60.0, "threads": 2}

    roundtrip = ExecutionMetadata.from_dict(d)
    assert roundtrip == meta


def test_problem_hash_deterministic(simple_network, simple_schedule_problem, simple_scps_problem):
    h1 = compute_problem_hash(simple_network)
    h2 = compute_problem_hash(simple_network)
    assert h1 == h2
    assert len(h1) == 64

    h_sched1 = compute_problem_hash(simple_schedule_problem)
    h_sched2 = compute_problem_hash(simple_schedule_problem)
    assert h_sched1 == h_sched2
    assert len(h_sched1) == 64
    assert h_sched1 != h1

    h_scps1 = compute_problem_hash(simple_scps_problem)
    h_scps2 = compute_problem_hash(simple_scps_problem)
    assert h_scps1 == h_scps2
    assert len(h_scps1) == 64
    assert h_scps1 != h_sched1


# -----------------------------------------------------------------------------
# 2. Backward Compatibility for Result Constructors
# -----------------------------------------------------------------------------
def test_result_dataclass_metadata_defaults():
    sr = SolveResult(
        status="optimal",
        solver="appsi_highs",
        termination_condition="optimal",
        message="ok",
        runtime_seconds=0.01,
    )
    assert sr.metadata is None
    d = sr.to_dict()
    assert "metadata" in d
    assert d["metadata"] is None

    sched_res = ScheduleResult(
        status="optimal",
        solver="appsi_highs",
        termination_condition="optimal",
        message="ok",
        runtime_seconds=0.02,
    )
    assert sched_res.metadata is None
    d_sched = sched_res.to_dict()
    assert "metadata" in d_sched
    assert d_sched["metadata"] is None

    scps_res = SCPSResult(
        status="optimal",
        solver="appsi_highs",
        termination_condition="optimal",
        message="ok",
        runtime_seconds=0.05,
    )
    assert scps_res.metadata is None
    d_scps = scps_res.to_dict()
    assert "metadata" in d_scps
    assert d_scps["metadata"] is None


# -----------------------------------------------------------------------------
# 3. Solver Integration: Metadata Capture
# -----------------------------------------------------------------------------
@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_solve_dispatch_captures_metadata(simple_network):
    opts = SolverOptions(time_limit_seconds=30.0, threads=1)
    res = solve_dispatch(simple_network, options=opts)
    assert res.ok
    assert res.metadata is not None
    assert isinstance(res.metadata, ExecutionMetadata)
    assert res.metadata.solver_name
    assert res.metadata.gridmind_version
    assert len(res.metadata.problem_hash) == 64
    assert res.metadata.solver_options.get("time_limit_seconds") == 30.0
    assert res.metadata.solver_options.get("threads") == 1

    d = res.to_dict()
    assert d["metadata"] is not None
    assert d["metadata"]["solver_options"]["time_limit_seconds"] == 30.0
    # Must dump cleanly to JSON
    json_str = json.dumps(d)
    assert isinstance(json_str, str)


@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_solve_schedule_captures_metadata(simple_schedule_problem):
    opts = SolverOptions(mip_gap=0.01)
    res = solve_schedule(simple_schedule_problem, options=opts)
    assert res.ok
    assert res.metadata is not None
    assert res.metadata.solver_options.get("mip_gap") == 0.01

    d = res.to_dict()
    assert d["metadata"]["solver_name"] == res.solver
    json_str = json.dumps(d)
    assert isinstance(json_str, str)


@pytest.mark.skipif(not solver_available(), reason="No solver available")
def test_solve_scps_captures_metadata(simple_scps_problem):
    opts = SolverOptions(time_limit_seconds=45.0)
    res = solve_security_constrained(simple_scps_problem, options=opts)
    assert res.ok
    assert res.metadata is not None
    assert res.metadata.solver_options.get("time_limit_seconds") == 45.0
    assert res.preventive_result is not None
    assert res.preventive_result.metadata is not None

    d = res.to_dict()
    assert d["metadata"]["problem_hash"]
    assert d["preventive_result"]["metadata"]["problem_hash"]
    json_str = json.dumps(d)
    assert isinstance(json_str, str)


# -----------------------------------------------------------------------------
# 4. Serialization Hardening: Non-finite Floats & Failure Paths
# -----------------------------------------------------------------------------
def test_sanitize_for_serialization_handles_specials():
    payload = {
        "regular": 42.5,
        "nan_val": float("nan"),
        "inf_val": float("inf"),
        "ninf_val": float("-inf"),
        "nested_dict": {"a": float("nan"), "b": [float("inf"), float("-inf"), 1.0]},
    }
    clean = sanitize_for_serialization(payload)
    assert clean["regular"] == 42.5
    assert clean["nan_val"] == "NaN"
    assert clean["inf_val"] == "Infinity"
    assert clean["ninf_val"] == "-Infinity"
    assert clean["nested_dict"]["a"] == "NaN"
    assert clean["nested_dict"]["b"] == ["Infinity", "-Infinity", 1.0]

    # Verify standard json.dumps without parse errors
    dumped = json.dumps(clean)
    parsed = json.loads(dumped)
    assert parsed["nan_val"] == "NaN"
    assert parsed["inf_val"] == "Infinity"
    assert parsed["nested_dict"]["b"][0] == "Infinity"


def test_failed_and_infeasible_results_serialize_safely():
    # Construct an infeasible SolveResult
    sr = SolveResult(
        status="infeasible",
        solver="appsi_highs",
        termination_condition="infeasible",
        message="model is infeasible",
        runtime_seconds=0.005,
        total_demand_mw=100.0,
        objective=None,
        dispatch={},
        flows={},
        unmet={},
        served={},
        metadata=ExecutionMetadata(
            timestamp_utc="2026-11-05T01:00:00Z",
            gridmind_version="0.2.0",
            solver_name="appsi_highs",
            solver_version="1.15.1",
            problem_hash="deadbeef" * 8,
            solver_options={"special_float": float("nan")},
        ),
    )
    d = sr.to_dict()
    assert d["ok"] is False
    assert d["objective"] is None
    assert d["metadata"]["solver_options"]["special_float"] == "NaN"

    # json.dumps must succeed without error
    dumped = json.dumps(d)
    loaded = json.loads(dumped)
    assert loaded["status"] == "infeasible"
    assert loaded["metadata"]["solver_options"]["special_float"] == "NaN"


def test_schedule_and_scps_non_optimal_serialization():
    sched = ScheduleResult(
        status="time_limit",
        solver="appsi_highs",
        termination_condition="maxTimeLimit",
        message="time limit reached",
        runtime_seconds=10.0,
        metadata=ExecutionMetadata(
            timestamp_utc="2026-11-05T01:00:00Z",
            gridmind_version="0.2.0",
            solver_name="appsi_highs",
            solver_version="1.15.1",
            problem_hash="0123" * 16,
        ),
    )
    d_sched = sched.to_dict()
    assert d_sched["ok"] is False
    assert json.dumps(d_sched)

    scps = SCPSResult(
        status="error",
        solver="appsi_highs",
        termination_condition="solver_exception",
        message="crash",
        runtime_seconds=0.1,
        metadata=ExecutionMetadata(
            timestamp_utc="2026-11-05T01:00:00Z",
            gridmind_version="0.2.0",
            solver_name="appsi_highs",
            solver_version="1.15.1",
            problem_hash="5678" * 16,
        ),
    )
    d_scps = scps.to_dict()
    assert d_scps["ok"] is False
    assert json.dumps(d_scps)
