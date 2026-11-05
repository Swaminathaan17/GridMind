"""Focused tests for Stage 5F.2: Contingency & SCPS Problem Representation."""
from __future__ import annotations

import pytest

from gridmind.network import Generator, Line, Network, Node, load_network
from gridmind.multiperiod import constant_horizon
from gridmind.security import Contingency, ContingencySet, SCPSProblem


@pytest.fixture
def small_network() -> Network:
    """A minimal 2-node, 2-generator, 1-line network for unit tests."""
    nodes = (
        Node(id="node-A", name="Node A", demand_mw=60.0),
        Node(id="node-B", name="Node B", demand_mw=60.0),
    )
    generators = (
        Generator(id="gen-1", name="Gen 1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),
        Generator(id="gen-2", name="Gen 2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, online=True),
    )
    lines = (
        Line(id="line-1", name="Line 1", from_node="node-A", to_node="node-B", capacity_mw=50.0, in_service=True),
    )
    net = Network(name="small-test-net", nodes=nodes, generators=generators, lines=lines)
    net.validate()
    return net


# -----------------------------------------------------------------------------
# Contingency Unit Tests
# -----------------------------------------------------------------------------

def test_contingency_valid(small_network: Network) -> None:
    c_gen = Contingency(id="c1", kind="generator", component_id="gen-1", name="Gen 1 outage")
    c_gen.validate(small_network)
    assert c_gen.id == "c1"
    assert c_gen.kind == "generator"
    assert c_gen.component_id == "gen-1"

    c_line = Contingency(id="c2", kind="line", component_id="line-1")
    c_line.validate(small_network)
    assert c_line.id == "c2"
    assert c_line.kind == "line"


def test_contingency_invalid_kind(small_network: Network) -> None:
    c = Contingency(id="c_bad", kind="bus", component_id="node-A")
    with pytest.raises(ValueError, match="Invalid contingency kind"):
        c.validate(small_network)


def test_contingency_unknown_component(small_network: Network) -> None:
    c_gen_missing = Contingency(id="c_missing_g", kind="generator", component_id="gen-999")
    with pytest.raises(KeyError, match="unknown generator"):
        c_gen_missing.validate(small_network)

    c_line_missing = Contingency(id="c_missing_l", kind="line", component_id="line-999")
    with pytest.raises(KeyError, match="unknown line"):
        c_line_missing.validate(small_network)


def test_contingency_empty_id(small_network: Network) -> None:
    c1 = Contingency(id="", kind="generator", component_id="gen-1")
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        c1.validate(small_network)

    c2 = Contingency(id="c2", kind="generator", component_id="")
    with pytest.raises(ValueError, match="component_id must be a non-empty string"):
        c2.validate(small_network)


def test_contingency_serialization(small_network: Network) -> None:
    c = Contingency(id="c_test", kind="generator", component_id="gen-1", name="Test Gen Outage")
    data = c.to_dict()
    assert data == {
        "id": "c_test",
        "kind": "generator",
        "component_id": "gen-1",
        "name": "Test Gen Outage",
    }
    restored = Contingency.from_dict(data)
    assert restored == c
    restored.validate(small_network)


# -----------------------------------------------------------------------------
# ContingencySet Unit Tests
# -----------------------------------------------------------------------------

def test_contingency_set_validation(small_network: Network) -> None:
    c1 = Contingency(id="c1", kind="generator", component_id="gen-1")
    c2 = Contingency(id="c2", kind="generator", component_id="gen-2")
    c3 = Contingency(id="c3", kind="line", component_id="line-1")

    c_set = ContingencySet(contingencies=(c1, c2, c3))
    assert len(c_set) == 3
    c_set.validate(small_network)


def test_contingency_set_duplicate_id(small_network: Network) -> None:
    c1 = Contingency(id="dup_id", kind="generator", component_id="gen-1")
    c2 = Contingency(id="dup_id", kind="generator", component_id="gen-2")
    c_set = ContingencySet(contingencies=(c1, c2))

    with pytest.raises(ValueError, match="Duplicate contingency id 'dup_id'"):
        c_set.validate(small_network)


def test_contingency_set_duplicate_component_outage(small_network: Network) -> None:
    c1 = Contingency(id="c1", kind="generator", component_id="gen-1")
    c2 = Contingency(id="c2", kind="generator", component_id="gen-1")
    c_set = ContingencySet(contingencies=(c1, c2))

    with pytest.raises(ValueError, match="Duplicate outage for generator 'gen-1'"):
        c_set.validate(small_network)


def test_contingency_set_auto_n_1(small_network: Network) -> None:
    auto_set = ContingencySet.auto_n_1(small_network)
    assert len(auto_set) == 3  # 2 generators + 1 line
    ids = {c.id for c in auto_set}
    assert ids == {"gen:gen-1", "gen:gen-2", "line:line-1"}
    auto_set.validate(small_network)

    gens_only = ContingencySet.auto_n_1(small_network, include_generators=True, include_lines=False)
    assert len(gens_only) == 2
    assert {c.id for c in gens_only} == {"gen:gen-1", "gen:gen-2"}

    lines_only = ContingencySet.auto_n_1(small_network, include_generators=False, include_lines=True)
    assert len(lines_only) == 1
    assert {c.id for c in lines_only} == {"line:line-1"}


def test_contingency_set_auto_n_1_filters_offline_components() -> None:
    nodes = (Node(id="n1", name="N1", demand_mw=10.0),)
    generators = (
        Generator(id="g_online", name="G1", node="n1", fuel="gas", capacity_mw=50.0, cost_per_mwh=10.0, online=True),
        Generator(id="g_offline", name="G2", node="n1", fuel="solar", capacity_mw=30.0, cost_per_mwh=5.0, online=False),
    )
    lines = (
        Line(id="l_inservice", name="L1", from_node="n1", to_node="n1", capacity_mw=100.0, in_service=True),
    )
    # self-loop fails Network validation, so create a second dummy node
    nodes = (
        Node(id="n1", name="N1", demand_mw=10.0),
        Node(id="n2", name="N2", demand_mw=0.0),
    )
    lines = (
        Line(id="l_inservice", name="L1", from_node="n1", to_node="n2", capacity_mw=100.0, in_service=True),
        Line(id="l_outservice", name="L2", from_node="n1", to_node="n2", capacity_mw=100.0, in_service=False),
    )
    net = Network(name="filter-net", nodes=nodes, generators=generators, lines=lines)
    net.validate()

    auto_set = ContingencySet.auto_n_1(net)
    assert len(auto_set) == 2
    assert {c.id for c in auto_set} == {"gen:g_online", "line:l_inservice"}


def test_contingency_set_serialization(small_network: Network) -> None:
    c_set = ContingencySet.auto_n_1(small_network)
    data = c_set.to_dict()
    restored = ContingencySet.from_dict(data)
    assert restored == c_set
    restored.validate(small_network)


# -----------------------------------------------------------------------------
# SCPSProblem Unit Tests
# -----------------------------------------------------------------------------

def test_scps_problem_valid(small_network: Network) -> None:
    base_problem = constant_horizon(small_network, periods=4, step_hours=1.0)
    c_set = ContingencySet.auto_n_1(small_network)

    scps_prob = SCPSProblem(problem=base_problem, contingency_set=c_set, notes="SCPS test problem")
    scps_prob.validate()

    assert scps_prob.periods == 4
    assert scps_prob.step_hours == 1.0
    assert scps_prob.network == small_network


def test_scps_problem_empty_contingency_set_rejected(small_network: Network) -> None:
    base_problem = constant_horizon(small_network, periods=2, step_hours=1.0)
    empty_set = ContingencySet(contingencies=())

    scps_prob = SCPSProblem(problem=base_problem, contingency_set=empty_set)
    with pytest.raises(ValueError, match="requires a non-empty contingency_set for N-1 security assessment"):
        scps_prob.validate()


def test_scps_problem_serialization(small_network: Network) -> None:
    base_problem = constant_horizon(small_network, periods=2, step_hours=1.0)
    c_set = ContingencySet.auto_n_1(small_network)

    scps_prob = SCPSProblem(problem=base_problem, contingency_set=c_set, notes="serialized test")
    data = scps_prob.to_dict()

    restored = SCPSProblem.from_dict(data)
    assert restored.notes == "serialized test"
    assert restored.periods == scps_prob.periods
    assert restored.step_hours == scps_prob.step_hours
    assert len(restored.contingency_set) == len(scps_prob.contingency_set)
    restored.validate()


def test_sample_network_full_auto_n1() -> None:
    """Validate auto_n_1 against the sample network (8-node test network)."""
    net = load_network()
    c_set = ContingencySet.auto_n_1(net)

    online_gens = sum(1 for g in net.generators if g.online)
    inservice_lines = sum(1 for l in net.lines if l.in_service)
    assert len(c_set) == online_gens + inservice_lines
    c_set.validate(net)


# -----------------------------------------------------------------------------
# Stage 5F.3: Model Construction and Mathematical Formulation Tests
# -----------------------------------------------------------------------------

def test_case_1_model_construction() -> None:
    """Case 1 (Line Outage & Bottleneck): verify Pyomo model structure.

    Node A (D=100 MW), Node B (D=20 MW).
    G1 at A (cap=100 MW, c=10), G2 at B (cap=50 MW, c=20).
    L1 (A->B, cap=40 MW), L2 (A->B, cap=40 MW).
    Contingency: L1 outage.
    """
    import pyomo.environ as pyo
    from gridmind.security import build_scps_model

    nodes = (Node(id="node-A", name="A", demand_mw=100.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=40.0, in_service=True),
        Line(id="l2", name="L2", from_node="node-A", to_node="node-B", capacity_mw=40.0, in_service=True),
    )
    net = Network(name="case-1-net", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    c_set = ContingencySet(contingencies=(c_l1,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    m = build_scps_model(scps_prob, allow_unmet=True)

    # 1. Decision variables
    assert hasattr(m, "p0") and hasattr(m, "pk")
    assert hasattr(m, "f0") and hasattr(m, "fk")
    assert hasattr(m, "u0") and hasattr(m, "uk")

    # 2. In contingency c_l1, flow on l1 is forced to 0.0
    c_ub = m.line_ub_k["c_l1", "l1", 0]
    c_lb = m.line_lb_k["c_l1", "l1", 0]
    assert c_ub.body.to_string() == "fk[c_l1,l1,0]"
    assert c_ub.lower == 0.0 and c_ub.upper == 0.0
    assert c_lb.body.to_string() == "fk[c_l1,l1,0]"
    assert c_lb.lower == 0.0 and c_lb.upper == 0.0

    # Line l2 survives and retains its 40 MW capacity
    c_l2_ub = m.line_ub_k["c_l1", "l2", 0]
    assert c_l2_ub.upper == 40.0

    # 3. Penalized mode: unmet bounds <= demand
    assert m.unmet_ub0["node-A", 0].upper == 100.0
    assert m.unmet_ub0["node-B", 0].upper == 20.0
    assert m.unmet_ub_k["c_l1", "node-A", 0].upper == 100.0
    assert m.unmet_ub_k["c_l1", "node-B", 0].upper == 20.0


def test_case_2_model_construction_generator_trip_and_ramp_exemption() -> None:
    """Case 2 (Generator Outage & Radial Island / Bottleneck):

    Node A (D=100 MW), Node B (D=60 MW).
    G1 at A (cap=100 MW, c=10, ramp=10 MW/h).
    G2 at B (cap=50 MW, c=20, ramp=10 MW/h).
    Line L1 (A->B, cap=50 MW).
    Contingency: G2 outage.

    Verifies:
    - Failed G2 is forced to 0 MW.
    - Failed G2 is EXEMPT from corrective ramp constraints.
    - Surviving G1 is subject to corrective ramp limits (relative to p0).
    """
    import pyomo.environ as pyo
    from gridmind.security import build_scps_model

    nodes = (Node(id="node-A", name="A", demand_mw=100.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, ramp_up_mw=10.0, ramp_down_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, ramp_up_mw=10.0, ramp_down_mw=10.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=50.0, in_service=True),
    )
    net = Network(name="case-2-net", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    c_set = ContingencySet(contingencies=(c_g2,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    m = build_scps_model(scps_prob, allow_unmet=True)

    # 1. Failed G2 output forced to 0.0
    c_g2_limit = m.gen_ub_k["c_g2", "g2", 0]
    assert c_g2_limit.lower == 0.0 and c_g2_limit.upper == 0.0
    assert c_g2_limit.body.to_string() == "pk[c_g2,g2,0]"

    # 2. Surviving G1 output bounded by capacity
    c_g1_limit = m.gen_ub_k["c_g2", "g1", 0]
    assert c_g1_limit.upper == 100.0

    # 3. Corrective ramp index sets: G2 MUST BE EXEMPT, G1 MUST BE INCLUDED
    assert ("c_g2", "g1", 0) in m.corr_ramp_up
    assert ("c_g2", "g1", 0) in m.corr_ramp_down
    assert ("c_g2", "g2", 0) not in m.corr_ramp_up
    assert ("c_g2", "g2", 0) not in m.corr_ramp_down

    # Check ramp constraint expressions for G1: pk - p0 <= 10.0 and p0 - pk <= 10.0
    up_c = m.corr_ramp_up["c_g2", "g1", 0]
    assert up_c.upper == 10.0
    assert "pk[c_g2,g1,0]" in str(up_c.body) and "p0[g1,0]" in str(up_c.body)


def test_case_3_model_construction_multi_period_ramping() -> None:
    """Case 3 (Multi-Period Ramping Constraint & Preventive Positioning):

    Node A (D=[90, 110] MW), Node B (D=[30, 30] MW).
    G1 at A (cap=100 MW, c=10, ramp=10 MW/h).
    G2 at B (cap=50 MW, c=20, ramp=10 MW/h).
    Line L1 (A->B, cap=50 MW).
    Contingency: G2 outage.

    Verifies multi-period ramp constraints and corrective ramp link across all t in T.
    """
    from gridmind.multiperiod import Horizon, ScheduleProblem
    from gridmind.security import build_scps_model

    nodes = (Node(id="node-A", name="A", demand_mw=90.0), Node(id="node-B", name="B", demand_mw=30.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, ramp_up_mw=10.0, ramp_down_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, ramp_up_mw=10.0, ramp_down_mw=10.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=50.0, in_service=True),
    )
    net = Network(name="case-3-net", nodes=nodes, generators=generators, lines=lines)

    # 2 periods with varying demand
    demand_steps = (
        {"node-A": 90.0, "node-B": 30.0},
        {"node-A": 110.0, "node-B": 30.0},
    )
    avail_steps = ({"g1": 100.0, "g2": 50.0}, {"g1": 100.0, "g2": 50.0})
    gen_online_steps = ({"g1": True, "g2": True}, {"g1": True, "g2": True})
    line_serv_steps = ({"l1": True}, {"l1": True})

    hor = Horizon(
        periods=2,
        step_hours=1.0,
        demand_mw=demand_steps,
        availability_mw=avail_steps,
        generator_online=gen_online_steps,
        line_in_service=line_serv_steps,
    )
    prob = ScheduleProblem(network=net, horizon=hor)

    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    c_set = ContingencySet(contingencies=(c_g2,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    m = build_scps_model(scps_prob)

    # Inter-period preventive ramp constraints exist for t=1
    assert ("g1", 1) in m.ramp_up0
    assert ("g1", 1) in m.ramp_down0
    assert ("g2", 1) in m.ramp_up0
    assert ("g2", 1) in m.ramp_down0

    # Corrective ramp constraints exist for surviving g1 at both t=0 and t=1
    assert ("c_g2", "g1", 0) in m.corr_ramp_up
    assert ("c_g2", "g1", 1) in m.corr_ramp_up
    assert ("c_g2", "g1", 0) in m.corr_ramp_down
    assert ("c_g2", "g1", 1) in m.corr_ramp_down

    # Failed g2 has NO corrective ramp constraints at any period
    assert ("c_g2", "g2", 0) not in m.corr_ramp_up
    assert ("c_g2", "g2", 1) not in m.corr_ramp_up


def test_battery_soc_contingency_independence() -> None:
    """Verify Battery shared initial state and absence of final SoC constraint in recourse."""
    import pyomo.environ as pyo
    from gridmind.multiperiod import Battery, constant_horizon
    from gridmind.security import build_scps_model

    nodes = (Node(id="node-A", name="A", demand_mw=100.0),)
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=150.0, cost_per_mwh=10.0, online=True),
    )
    batts = (
        Battery(id="bat-1", node="node-A", capacity_mwh=60.0, max_charge_mw=20.0, max_discharge_mw=20.0,
                initial_soc_mwh=30.0, final_soc_mwh=35.0, min_soc_mwh=10.0),
    )
    net = Network(name="bat-net", nodes=nodes, generators=generators, lines=())
    prob = constant_horizon(net, periods=3, step_hours=1.0, batteries=batts)

    c1 = Contingency(id="c_g1", kind="generator", component_id="g1")
    c_set = ContingencySet(contingencies=(c1,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    m = build_scps_model(scps_prob)

    # 1. Initial SoC shared
    assert pyo.value(m.bat_soc0["bat-1"]) == 30.0

    # 2. Preventive terminal SoC constraint exists
    assert "bat-1" in m.soc_final0
    assert m.soc_final0["bat-1"].lower == 35.0 and m.soc_final0["bat-1"].upper == 35.0

    # 3. Recourse terminal SoC constraint DOES NOT EXIST
    assert not hasattr(m, "soc_final_k")

    # 4. Recourse dynamic SoC constraint starts from initial SoC at t=0
    # ek[k, b, 0] == bat_soc0[b] + ...
    assert ("c_g1", "bat-1", 0) in m.soc_dyn_k
    assert ("c_g1", "bat-1", 1) in m.soc_dyn_k
    assert ("c_g1", "bat-1", 2) in m.soc_dyn_k


def test_strict_vs_penalized_model_construction() -> None:
    """Verify strict mode sets u0 == 0 and uk == 0, whereas penalized mode bounds u <= demand."""
    from gridmind.security import build_scps_model

    nodes = (Node(id="node-A", name="A", demand_mw=80.0),)
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    net = Network(name="net-strict", nodes=nodes, generators=generators, lines=())
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c1 = Contingency(id="c_g1", kind="generator", component_id="g1")
    c_set = ContingencySet(contingencies=(c1,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    # Strict mode (allow_unmet=False)
    m_strict = build_scps_model(scps_prob, allow_unmet=False)
    assert m_strict.unmet_ub0["node-A", 0].lower == 0.0 and m_strict.unmet_ub0["node-A", 0].upper == 0.0
    assert m_strict.unmet_ub_k["c_g1", "node-A", 0].lower == 0.0 and m_strict.unmet_ub_k["c_g1", "node-A", 0].upper == 0.0

    # Penalized mode (allow_unmet=True)
    m_pen = build_scps_model(scps_prob, allow_unmet=True)
    assert m_pen.unmet_ub0["node-A", 0].upper == 80.0
    assert m_pen.unmet_ub_k["c_g1", "node-A", 0].upper == 80.0


def test_battery_exclusive_mode_variables() -> None:
    """Verify battery_mode='exclusive' adds binary variables z0 and zk."""
    import pyomo.environ as pyo
    from gridmind.multiperiod import Battery, constant_horizon
    from gridmind.security import build_scps_model

    nodes = (Node(id="node-A", name="A", demand_mw=50.0),)
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    batts = (Battery(id="bat-1", node="node-A", capacity_mwh=50.0, max_charge_mw=10.0, max_discharge_mw=10.0, initial_soc_mwh=20.0),)
    net = Network(name="net-exclusive", nodes=nodes, generators=generators, lines=())
    prob = constant_horizon(net, periods=2, step_hours=1.0, batteries=batts)
    c1 = Contingency(id="c_g1", kind="generator", component_id="g1")
    c_set = ContingencySet(contingencies=(c1,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    # LP mode
    m_lp = build_scps_model(scps_prob, battery_mode="lp")
    assert not hasattr(m_lp, "z0")
    assert not hasattr(m_lp, "zk")

    # Exclusive mode
    m_ex = build_scps_model(scps_prob, battery_mode="exclusive")
    assert hasattr(m_ex, "z0")
    assert hasattr(m_ex, "zk")
    assert hasattr(m_ex, "exclusive_charge0")
    assert hasattr(m_ex, "exclusive_charge_k")


# -----------------------------------------------------------------------------
# Stage 5F.3: Solver Execution, Solution Extraction & Hand-Calculated Cases
# -----------------------------------------------------------------------------

def test_solve_security_constrained_solver_unavailable() -> None:
    """When resolve_solver raises SolverUnavailableError, return solver_unavailable status."""
    import unittest.mock as mock
    from gridmind.security import solve_security_constrained
    from gridmind.solver import SolverUnavailableError

    nodes = (Node(id="node-A", name="A", demand_mw=50.0),)
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    net = Network(name="net-unavail", nodes=nodes, generators=generators, lines=())
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c1 = Contingency(id="c_g1", kind="generator", component_id="g1")
    c_set = ContingencySet(contingencies=(c1,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    with mock.patch("gridmind.security.resolve_solver", side_effect=SolverUnavailableError("no solver")):
        res = solve_security_constrained(scps_prob)
        assert res.status == "solver_unavailable"
        assert res.n1_secure is False
        assert "no solver" in res.message


def test_solve_security_constrained_infeasible() -> None:
    """When solver returns infeasible termination condition, SCPSResult reports infeasible."""
    import unittest.mock as mock
    from pyomo.opt import SolverStatus, TerminationCondition
    from gridmind.security import solve_security_constrained

    nodes = (Node(id="node-A", name="A", demand_mw=50.0),)
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    net = Network(name="net-inf", nodes=nodes, generators=generators, lines=())
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c1 = Contingency(id="c_g1", kind="generator", component_id="g1")
    c_set = ContingencySet(contingencies=(c1,))
    scps_prob = SCPSProblem(problem=prob, contingency_set=c_set)

    class InfeasibleSolver:
        def solve(self, model, load_solutions=False, tee=False):
            class _Solver: pass
            class Results: pass
            r = Results()
            r.solver = _Solver()
            r.solver.status = SolverStatus.warning
            r.solver.termination_condition = TerminationCondition.infeasible
            return r

    with mock.patch("gridmind.security.resolve_solver", return_value=("fake_inf", InfeasibleSolver())):
        res = solve_security_constrained(scps_prob, allow_unmet=False)
        assert res.status == "infeasible"
        assert res.termination_condition == "infeasible"
        assert res.n1_secure is False
        assert res.strict is True


def test_hand_calculated_case_1_line_islanding() -> None:
    """Revision 3 Case 1: Two nodes, one generator, one line (islanding).

    Network:
    - Node A: generator g1, capacity 100 MW, $10/MWh. Demand D_A = 80 MW.
    - Node B: demand D_B = 20 MW. No local generation.
    - Line l1: A->B, capacity 30 MW.
    - No batteries. Single period (T=1, dt=1.0).

    Preventive dispatch:
      p0[g1] = 100 MW (serves 80 MW local + 20 MW export).
      f0[l1] = 20 MW.
      u0 = 0 everywhere. Preventive cost = 10 * 100 = $1,000.

    Contingency c_l1 (line l1 outage):
      f^k[l1] = 0 MW.
      B has no local generation and no line supply -> u^k_B = 20 MW.
      Shortfall = 20 MW at B.
      Expected: NOT N-1 secure.
    """
    from gridmind.security import solve_security_constrained

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),
    )
    net = Network(name="case-1-islanding", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    # 1. Penalized mode solve
    res_pen = solve_security_constrained(scps_prob, allow_unmet=True)
    assert res_pen.status == "optimal"
    assert res_pen.n1_secure is False  # Invariant: Case 1 is NOT N-1 secure!
    assert res_pen.preventive_result.dispatch["g1"] == [100.0]
    assert res_pen.preventive_result.flows["l1"] == [20.0]
    assert res_pen.preventive_generation_cost_usd == 1000.0

    sched = res_pen.contingencies["c_l1"]
    assert sched.is_secure is False
    assert sched.flows["l1"] == [0.0]
    assert sched.unmet["node-B"] == [20.0]
    assert sched.unmet_energy_mwh == 20.0
    assert sched.unmet_penalty_cost_usd == 20.0 * 1000.0

    # 2. Strict mode solve: must be infeasible
    res_strict = solve_security_constrained(scps_prob, allow_unmet=False)
    assert res_strict.status == "infeasible"
    assert res_strict.n1_secure is False
    assert res_strict.strict is True


def test_hand_calculated_case_2_generator_outage_bottleneck() -> None:
    """Revision 3 Case 2: Two nodes, two generators, one line (corrected).

    Network:
    - Node A: generator g1, capacity 100 MW, $10/MWh. Demand D_A = 60 MW.
    - Node B: generator g2, capacity 50 MW, $20/MWh. Demand D_B = 60 MW.
    - Line l1: A->B, capacity 50 MW. No batteries. No ramp limits.

    Preventive dispatch:
      Cheapest: g1=100 MW, exports 40 MW to B over l1 (<= 50 cap).
      B needs 60 MW: 40 MW import + 20 MW from g2.
      Preventive cost = 10*100 + 20*20 = $1,400.

    Contingency c_g2 (outage of g2):
      g2 is tripped: p^k[g2] = 0 MW.
      g1 at Node A can supply at most 100 MW, of which 60 MW serves local demand A.
      Max export over l1 is 100 - 60 = 40 MW (limited by g1 capacity, not line rating).
      Node B demand is 60 MW -> u^k_B = 60 - 40 = 20 MW.
      Expected: NOT N-1 secure. Shortfall = 20 MW at B.
    """
    from gridmind.security import solve_security_constrained

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=50.0, in_service=True),
    )
    net = Network(name="case-2-bottleneck", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    # 1. Penalized mode solve
    res_pen = solve_security_constrained(scps_prob, allow_unmet=True)
    assert res_pen.status == "optimal"
    assert res_pen.n1_secure is False  # Invariant: Case 2 is NOT N-1 secure!

    # In the joint optimization, corrective unmet penalty dominates, yielding optimal corrective f=40 MW
    sched = res_pen.contingencies["c_g2"]
    assert sched.is_secure is False
    assert sched.dispatch["g2"] == [0.0]
    assert sched.dispatch["g1"] == [100.0]
    assert sched.flows["l1"] == [40.0]
    assert sched.unmet["node-B"] == [20.0]
    assert sched.unmet_energy_mwh == 20.0
    assert sched.unmet_penalty_cost_usd == 20.0 * 1000.0

    # 2. Strict mode solve: must be infeasible
    res_strict = solve_security_constrained(scps_prob, allow_unmet=False)
    assert res_strict.status == "infeasible"
    assert res_strict.n1_secure is False


def test_hand_calculated_case_3_ramp_constrained_recovery() -> None:
    """Revision 3 Case 3: Ramp-constrained corrective recovery.

    Network:
    - Node A: generator g1, capacity 200 MW, $10/MWh, RU = 10 MW/h.
      Initial dispatch p_init[g1] = 80 MW. Demand D_A = 60 MW.
    - Node B: generator g2, capacity 60 MW, $20/MWh, no ramp limit. Demand D_B = 60 MW.
    - Line l1: A->B, capacity 200 MW.
    - No batteries. T=1, dt=1.0.

    Preventive dispatch:
      g1 ramp from 80: p0[g1] <= 80 + 10 = 90 MW.
      Optimal p0[g1] = 90 MW, exports 90 - 60 = 30 MW over l1.
      B balance: 30 + p0[g2] = 60 -> p0[g2] = 30 MW.
      Preventive cost = 10*90 + 20*30 = $1,500.

    Contingency c_g2 (outage of g2):
      g2 is tripped: p^k[g2] = 0 MW (exempt from ramp down).
      Surviving g1 corrective ramp: p^k[g1] <= p0[g1] + 10 = 90 + 10 = 100 MW.
      Total generation = 100 MW for 120 MW total demand -> shortfall = 20 MW.
      Expected: NOT N-1 secure.
    """
    from gridmind.multiperiod import replace
    from gridmind.security import solve_security_constrained

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),
    )
    net = Network(name="case-3-ramp", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    # 1. Penalized mode solve
    res_pen = solve_security_constrained(scps_prob, allow_unmet=True)
    assert res_pen.status == "optimal"
    assert res_pen.n1_secure is False  # Invariant: Case 3 is NOT N-1 secure!
    assert res_pen.preventive_result.dispatch["g1"] == [90.0]
    assert res_pen.preventive_result.dispatch["g2"] == [30.0]
    assert res_pen.preventive_generation_cost_usd == 1500.0

    sched = res_pen.contingencies["c_g2"]
    assert sched.is_secure is False
    assert sched.dispatch["g2"] == [0.0]
    assert sched.dispatch["g1"] == [100.0]
    assert sched.unmet_energy_mwh == 20.0
    assert sched.unmet_penalty_cost_usd == 20.0 * 1000.0

    # 2. Strict mode solve: must be infeasible
    res_strict = solve_security_constrained(scps_prob, allow_unmet=False)
    assert res_strict.status == "infeasible"
    assert res_strict.n1_secure is False


def test_hand_calculated_case_4_battery_assisted_recovery() -> None:
    """Revision 3 Case 4: Battery-assisted recovery.

    Same network as Case 3, plus a battery at node B:
    - Battery b1 at B: capacity 60 MWh, max_charge = max_discharge = 30 MW,
      eta_ch = eta_dis = 1.0, initial SoC E0 = 60 MWh, min SoC = 0 MWh.
      No terminal SoC constraint.

    Preventive:
      g1 dispatch ramped from 80 to 90 MW.
    Contingency c_g2:
      g1 corrective ramp: p^k[g1] <= 100 MW.
      Battery b1 discharges 20 MW (or up to 30 MW) at Node B to eliminate shortfall.
      Corrective SoC falls from initial 60 MWh to 40 MWh (or 30 MWh).
      Expected: N-1 secure with battery assistance!
    """
    from gridmind.multiperiod import Battery, replace
    from gridmind.security import solve_security_constrained

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),
    )
    batts = (
        Battery(id="b1", node="node-B", capacity_mwh=60.0, max_charge_mw=30.0, max_discharge_mw=30.0,
                initial_soc_mwh=60.0, min_soc_mwh=0.0, eta_charge=1.0, eta_discharge=1.0),
    )
    net = Network(name="case-4-battery", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0, batteries=batts)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    # 1. Full solve with 60 MWh battery: must be N-1 secure
    res = solve_security_constrained(scps_prob, allow_unmet=True)
    assert res.status == "optimal"
    assert res.n1_secure is True  # Invariant: Case 4 IS N-1 secure!
    sched = res.contingencies["c_g2"]
    assert sched.is_secure is True
    assert sched.unmet_energy_mwh == 0.0

    # 2. Strict mode must also be optimal and N-1 secure
    res_strict = solve_security_constrained(scps_prob, allow_unmet=False)
    assert res_strict.status == "optimal"
    assert res_strict.n1_secure is True

    # 3. Verify exact 20 MW discharge and SoC 60 -> 40 MWh trajectory from Section 10.1
    # When g1 is at 100 MW corrective dispatch (40 MW export to B), B needs exactly 20 MW battery discharge
    from gridmind.security import build_scps_model
    import pyomo.environ as pyo

    m = build_scps_model(scps_prob)
    # Fix preventive to Case 3 baseline where battery is idle
    m.p0["g1", 0].fix(90.0)
    m.p0["g2", 0].fix(30.0)
    m.f0["l1", 0].fix(30.0)
    m.dis0["b1", 0].fix(0.0)
    m.ch0["b1", 0].fix(0.0)
    # Fix g1 corrective to its maximum ramped 100 MW (delivering 40 MW to B)
    m.pk["c_g2", "g1", 0].fix(100.0)

    opt = pyo.SolverFactory("appsi_highs")
    solve_res = opt.solve(m)
    assert solve_res.solver.status in (pyo.SolverStatus.ok, pyo.SolverStatus.warning)
    assert pyo.value(m.disk["c_g2", "b1", 0]) == 20.0
    assert pyo.value(m.ek["c_g2", "b1", 0]) == 40.0
    assert pyo.value(m.uk["c_g2", "node-B", 0]) == 0.0

    # 4. Revision 3 Variant: Insufficient battery initial energy (10 MWh)
    batts_low = (
        Battery(id="b1", node="node-B", capacity_mwh=60.0, max_charge_mw=30.0, max_discharge_mw=30.0,
                initial_soc_mwh=10.0, min_soc_mwh=0.0, eta_charge=1.0, eta_discharge=1.0),
    )
    base_prob_low = constant_horizon(net, periods=1, step_hours=1.0, batteries=batts_low)
    prob_low = replace(base_prob_low, initial_dispatch_mw={"g1": 80.0})
    scps_low = SCPSProblem(problem=prob_low, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res_low = solve_security_constrained(scps_low, allow_unmet=True)
    assert res_low.status == "optimal"
    assert res_low.n1_secure is False  # Invariant: 10 MWh initial SoC is NOT N-1 secure!
    sched_low = res_low.contingencies["c_g2"]
    assert sched_low.is_secure is False
    assert sched_low.unmet_energy_mwh == 10.0  # Exactly 10 MW shortfall remaining


def test_hand_calculated_parallel_line_redundancy_redispatch() -> None:
    """Parallel-line redundancy test (two parallel lines, one trips, surviving carries load).

    Node A: D=100 MW. G1 at A: cap=100 MW, c=10.
    Node B: D=20 MW. G2 at B: cap=50 MW, c=20.
    Lines: L1 (A->B, cap=40 MW), L2 (A->B, cap=40 MW).
    Contingency: L1 outage.
    Expected: N-1 secure (L2 carries the 20 MW export).
    """
    from gridmind.security import solve_security_constrained

    nodes = (Node(id="node-A", name="A", demand_mw=100.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, online=True),
    )
    lines = (
        Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=40.0, in_service=True),
        Line(id="l2", name="L2", from_node="node-A", to_node="node-B", capacity_mw=40.0, in_service=True),
    )
    net = Network(name="parallel-line-case", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    assert res.status == "optimal"
    assert res.n1_secure is True
    assert res.total_objective_usd == 1400.0

    sched = res.contingencies["c_l1"]
    assert sched.is_secure is True
    assert sched.flows["l1"] == [0.0]
    assert sched.unmet_energy_mwh == 0.0


# -----------------------------------------------------------------------------
# Stage 5F.4: Independent Verification Engine Tests & Adversarial Corruption
# -----------------------------------------------------------------------------

def test_verify_scps_result_on_valid_case_4() -> None:
    """Verify that a valid optimal schedule (Case 4) passes all verifier checks with no false positives."""
    from gridmind.multiperiod import Battery, replace
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, online=True),
    )
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),)
    batts = (
        Battery(id="b1", node="node-B", capacity_mwh=60.0, max_charge_mw=30.0, max_discharge_mw=30.0,
                initial_soc_mwh=60.0, min_soc_mwh=0.0, eta_charge=1.0, eta_discharge=1.0),
    )
    net = Network(name="case-4-val", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0, batteries=batts)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    report = verify_scps_result(scps_prob, res)

    assert report.passed is True
    assert report.physical_feasible is True
    assert report.model_valid is True
    assert report.n1_secure is True
    assert len(report.failed()) == 0
    assert "c_g2" in report.contingency_reports
    assert report.contingency_reports["c_g2"].is_secure is True
    assert report.contingency_reports["c_g2"].unmet_energy_mwh == 0.0


def test_adversarial_corruption_preventive_capacity() -> None:
    """ADV-01: Corrupt preventive generator output beyond capacity."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-adv", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Corrupt: set g1 output above 100 MW capacity
    corrupted_res.preventive_result.dispatch["g1"] = [105.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    assert report.physical_feasible is False
    failures = [c.name for c in report.failed()]
    assert any("generator_limits" in f for f in failures)


def test_adversarial_corruption_preventive_ramp() -> None:
    """ADV-02: Corrupt preventive generator ramp rate between periods."""
    import copy
    from gridmind.multiperiod import Horizon, ScheduleProblem
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=50.0),)
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),)
    net = Network(name="net-ramp", nodes=nodes, generators=generators, lines=())
    hor = Horizon(
        periods=2, step_hours=1.0,
        demand_mw=({"node-A": 40.0}, {"node-A": 50.0}),
        availability_mw=({"g1": 100.0}, {"g1": 100.0}),
        generator_online=({"g1": True}, {"g1": True}),
        line_in_service=({}, {}),
    )
    prob = ScheduleProblem(network=net, horizon=hor)
    c_g1 = Contingency(id="c_g1", kind="generator", component_id="g1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Ramp from 40 to 70 MW (exceeds ramp_up of 10 MW/h)
    corrupted_res.preventive_result.dispatch["g1"] = [40.0, 70.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    failures = [c.name for c in report.failed()]
    assert any("ramp_limits" in f for f in failures)


def test_adversarial_corruption_outaged_generator_generates() -> None:
    """ADV-06: Corrupt outaged generator recourse dispatch to a non-zero value."""
    import copy
    from gridmind.multiperiod import replace
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=50.0, cost_per_mwh=20.0, online=True),
    )
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=50.0, in_service=True),)
    net = Network(name="c2-adv", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Tripped g2 is falsely set to generate 5 MW
    corrupted_res.contingencies["c_g2"].dispatch["g2"] = [5.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    assert report.physical_feasible is False
    failures = [c.name for c in report.failed()]
    assert "c_g2:outaged_generator_zero_output" in failures


def test_adversarial_corruption_outaged_line_carries_flow() -> None:
    """ADV-07: Corrupt outaged transmission line to carry non-zero flow."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-adv2", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Tripped line l1 carries 2 MW flow
    corrupted_res.contingencies["c_l1"].flows["l1"] = [2.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    failures = [c.name for c in report.failed()]
    assert "c_l1:outaged_line_zero_flow" in failures


def test_adversarial_corruption_surviving_generator_corrective_ramp() -> None:
    """ADV-08: Surviving generator exceeds corrective ramp limit relative to preventive dispatch."""
    import copy
    from gridmind.multiperiod import replace
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, online=True),
    )
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),)
    net = Network(name="c3-adv", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # In Case 3, p0[g1]=90. Corrective ramp max is 90 + 10 = 100 MW. Force pk[g1] = 115 MW
    corrupted_res.contingencies["c_g2"].dispatch["g1"] = [115.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    failures = [c.name for c in report.failed()]
    assert "c_g2:corrective_ramp_limits" in failures


def test_tripped_generator_ramp_exemption_verified() -> None:
    """ADV-09: Tripped generator g2 drops from p0=30 MW to 0 MW. Verifier MUST NOT flag this as ramp-down violation."""
    from gridmind.multiperiod import replace
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, ramp_down_mw=5.0, online=True),
    )
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),)
    net = Network(name="c3-ramp-ex", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    report = verify_scps_result(scps_prob, res)

    # g2 ramped down from 30 to 0 (delta = 30 MW > ramp_down 5 MW). Must NOT fail corrective_ramp_limits!
    ramp_checks = [c for c in report.checks if c.name == "c_g2:corrective_ramp_limits"]
    assert len(ramp_checks) == 1
    assert ramp_checks[0].passed is True


def test_adversarial_corruption_recourse_power_balance() -> None:
    """ADV-10: Corrupt recourse power balance at a node."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-bal", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # In Case 1, unmet at B is 20 MW. Artificially change to 10 MW without supplying the remaining 10 MW
    corrupted_res.contingencies["c_l1"].unmet["node-B"] = [10.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    failures = [c.name for c in report.failed()]
    assert "c_l1:power_balance" in failures


def test_adversarial_corruption_recourse_battery_soc_drift() -> None:
    """ADV-11: Recourse battery SoC violates dynamics starting from shared E0."""
    import copy
    from gridmind.multiperiod import Battery, replace
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, online=True),
    )
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),)
    batts = (
        Battery(id="b1", node="node-B", capacity_mwh=60.0, max_charge_mw=30.0, max_discharge_mw=30.0,
                initial_soc_mwh=60.0, min_soc_mwh=0.0, eta_charge=1.0, eta_discharge=1.0),
    )
    net = Network(name="c4-soc", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0, batteries=batts)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Corrupt recourse battery SoC value
    corrupted_res.contingencies["c_g2"].soc["b1"] = [15.0]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    failures = [c.name for c in report.failed()]
    assert "c_g2:battery_soc_dynamics" in failures


def test_recourse_battery_terminal_soc_exemption_verified() -> None:
    """ADV-12: Battery has final_soc declared in problem. Recourse finishes at different SoC. Verifier MUST NOT fail."""
    from gridmind.multiperiod import Battery, replace
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=60.0), Node(id="node-B", name="B", demand_mw=60.0))
    generators = (
        Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=200.0, cost_per_mwh=10.0, ramp_up_mw=10.0, online=True),
        Generator(id="g2", name="G2", node="node-B", fuel="gas", capacity_mw=60.0, cost_per_mwh=20.0, online=True),
    )
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=200.0, in_service=True),)
    # Declare final_soc_mwh = 60.0 on battery
    batts = (
        Battery(id="b1", node="node-B", capacity_mwh=60.0, max_charge_mw=30.0, max_discharge_mw=30.0,
                initial_soc_mwh=60.0, final_soc_mwh=60.0, min_soc_mwh=0.0, eta_charge=1.0, eta_discharge=1.0),
    )
    net = Network(name="c4-term-ex", nodes=nodes, generators=generators, lines=lines)
    base_prob = constant_horizon(net, periods=1, step_hours=1.0, batteries=batts)
    prob = replace(base_prob, initial_dispatch_mw={"g1": 80.0})
    c_g2 = Contingency(id="c_g2", kind="generator", component_id="g2")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_g2,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    report = verify_scps_result(scps_prob, res)

    # In recourse, battery discharged 30 MW to support B, leaving SoC at 30 MWh != 60.0 MWh
    # The verifier must PASS because terminal SoC is exempt in contingency recourse
    assert report.contingency_reports["c_g2"].passed is True
    assert report.contingency_reports["c_g2"].is_secure is True


def test_adversarial_corruption_objective_tampering() -> None:
    """ADV-14: Tamper reported total objective cost."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-tamper", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Artificially alter total objective
    corrupted_res.total_objective_usd += 100.0

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    assert report.model_valid is False
    failures = [c.name for c in report.failed()]
    assert "preventive_total_objective_consistency" in failures


def test_adversarial_corruption_falsified_security_flag() -> None:
    """ADV-16: Tamper n1_secure flag on an insecure solution (Case 1 has 20 MW unmet demand)."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-falsify", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    # Falsify n1_secure flag to True
    corrupted_res.n1_secure = True
    corrupted_res.contingencies["c_l1"].is_secure = True

    # Verifier must independently detect the unmet energy and assert n1_secure is False!
    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.n1_secure is False
    assert report.contingency_reports["c_l1"].is_secure is False
    assert report.contingency_reports["c_l1"].unmet_energy_mwh == 20.0


def test_adversarial_corruption_missing_contingency() -> None:
    """ADV-17: Result drops one contingency from its mapping."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-drop", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    corrupted_res.contingencies.clear()

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    failures = [c.name for c in report.failed()]
    assert "contingency_completeness" in failures


def test_adversarial_corruption_non_finite_values() -> None:
    """ADV-18: Injection of NaN or Infinity into schedule arrays."""
    import copy
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import verify_scps_result

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-nan", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    corrupted_res = copy.deepcopy(res)
    corrupted_res.contingencies["c_l1"].dispatch["g1"] = [float("nan")]

    report = verify_scps_result(scps_prob, corrupted_res)
    assert report.passed is False
    assert report.physical_feasible is False
    failures = [c.name for c in report.failed()]
    assert "c_l1:finite_values" in failures


def test_independent_validation_attached_to_scps_result() -> None:
    """Verify solve_security_constrained automatically attaches SCPSValidationReport to SCPSResult."""
    from gridmind.security import solve_security_constrained
    from gridmind.security_validation import SCPSValidationReport

    nodes = (Node(id="node-A", name="A", demand_mw=80.0), Node(id="node-B", name="B", demand_mw=20.0))
    generators = (Generator(id="g1", name="G1", node="node-A", fuel="gas", capacity_mw=100.0, cost_per_mwh=10.0, online=True),)
    lines = (Line(id="l1", name="L1", from_node="node-A", to_node="node-B", capacity_mw=30.0, in_service=True),)
    net = Network(name="c1-attach", nodes=nodes, generators=generators, lines=lines)
    prob = constant_horizon(net, periods=1, step_hours=1.0)
    c_l1 = Contingency(id="c_l1", kind="line", component_id="l1")
    scps_prob = SCPSProblem(problem=prob, contingency_set=ContingencySet(contingencies=(c_l1,)))

    res = solve_security_constrained(scps_prob, allow_unmet=True)
    assert isinstance(res.validation, SCPSValidationReport)
    assert res.validation.passed is True
    assert res.validation.physical_feasible is True
    assert res.n1_secure == res.validation.n1_secure
    assert res.n1_secure is False  # Case 1 has 20 MW shortfall at B
