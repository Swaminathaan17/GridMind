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
