import dataclasses

from gridmind.network import sample_network
from gridmind.scenarios import (ALL_SCENARIOS, CORE_SCENARIOS, apply_scenario, generate_random_scenarios,
                                get_scenario, scale_demand, with_generator_outage, with_line_outage)


def test_sample_network_is_valid_and_documented():
    net = sample_network()
    net.validate()
    assert net.total_demand_mw == 375.0
    assert len(net.nodes) == 8 and len(net.generators) == 6 and len(net.lines) == 8
    assert "not a real grid" in net.notes.lower()


def test_four_core_scenarios_exist():
    assert [s.id for s in CORE_SCENARIOS] == ["normal", "peak_demand", "low_renewable", "line_outage"]
    assert len({s.id for s in ALL_SCENARIOS}) == len(ALL_SCENARIOS)


def test_transformations_do_not_mutate_input():
    net = sample_network()
    before = net.to_dict()
    scale_demand(net, 2.0)
    with_line_outage(net, "line-central-east")
    with_generator_outage(net, "gas-north")
    assert net.to_dict() == before


def test_scenario_effects():
    net = sample_network()
    assert apply_scenario(net, get_scenario("peak_demand")).total_demand_mw == 375 * 1.28
    low = apply_scenario(net, get_scenario("low_renewable"))
    assert low.generator("solar-west").available_mw == 72 * 0.3
    assert low.generator("gas-north").available_mw == 200  # non-renewable untouched
    assert apply_scenario(net, get_scenario("line_outage")).line("line-central-east").in_service is False


def test_random_scenarios_are_deterministic():
    net = sample_network()
    a, b = generate_random_scenarios(net, 25, seed=7), generate_random_scenarios(net, 25, seed=7)
    assert a == b
    assert a != generate_random_scenarios(net, 25, seed=8)
    assert all(0.7 <= s.demand_multiplier <= 1.4 for s in a)


def test_invalid_network_rejected():
    net = sample_network()
    bad = dataclasses.replace(net, lines=net.lines + (dataclasses.replace(net.lines[0], to_node="nowhere"),))
    try:
        bad.validate()
    except ValueError:
        return
    raise AssertionError("expected ValueError")
