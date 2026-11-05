"""Regression coverage for the three GridMind validation audit findings."""
from dataclasses import replace

import pytest

from gridmind.multiperiod import Battery, Horizon, ScheduleProblem
from gridmind.network import Generator, Line, Network, Node
from gridmind.stochastic import _validate_scenario
from gridmind.uncertainty import ScenarioSet, UncertaintyConfig, generate_scenarios
from gridmind.validation import validate_schedule


def _problem():
    network = Network(
        name="audit-regression",
        nodes=(Node("A", "A", 10.0), Node("B", "B", 0.0)),
        generators=(Generator("G", "G", "A", "gas", 100.0, 10.0,
                              ramp_up_mw=5.0, ramp_down_mw=5.0),),
        lines=(Line("L", "L", "A", "B", 10.0),),
        unmet_penalty_per_mwh=1000.0,
    )
    horizon = Horizon(
        periods=2,
        step_hours=1.0,
        demand_mw=({"A": 10.0, "B": 0.0}, {"A": 10.0, "B": 0.0}),
        availability_mw=({"G": 100.0}, {"G": 100.0}),
        generator_online=({"G": True}, {"G": True}),
        line_in_service=({"L": True}, {"L": True}),
    )
    battery = Battery("B", "B", 10.0, 3.0, 3.0, initial_soc_mwh=5.0,
                      min_soc_mwh=2.0, final_soc_mwh=5.0)
    return ScheduleProblem(network, horizon, (battery,), initial_dispatch_mw={"G": 10.0})


def _trajectories():
    return {
        "dispatch": {"G": [10.0, 10.0]},
        "flows": {"L": [0.0, 0.0]},
        "unmet": {"A": [0.0, 0.0], "B": [0.0, 0.0]},
        "charge": {"B": [0.0, 0.0]},
        "discharge": {"B": [0.0, 0.0]},
        "soc": {"B": [5.0, 5.0]},
    }


def _stochastic_report(problem, trajectories):
    return _validate_scenario(
        problem, problem,
        trajectories["dispatch"], trajectories["flows"], trajectories["unmet"],
        trajectories["charge"], trajectories["discharge"], trajectories["soc"],
        reported=200.0, allow_unmet=True, battery_mode="lp",
    )


def test_stochastic_scenario_validator_accepts_feasible_trajectory():
    problem = _problem()
    assert _stochastic_report(problem, _trajectories()).passed


def test_scenario_set_rejects_duplicate_ids():
    problem = _problem()
    generated = generate_scenarios(problem, UncertaintyConfig(n_scenarios=2, seed=71))
    first, second = generated.scenarios
    duplicate = ScenarioSet(
        scenarios=(replace(first, id="duplicate", probability=0.5),
                   replace(second, id="duplicate", probability=0.5)),
        demand_nodes=generated.demand_nodes,
        availability_generators=generated.availability_generators,
    )
    with pytest.raises(ValueError, match="scenario IDs must be unique.*duplicate"):
        duplicate


def test_scenario_set_accepts_unique_ids():
    problem = _problem()
    generated = generate_scenarios(problem, UncertaintyConfig(n_scenarios=2, seed=72))
    assert len({scenario.id for scenario in generated.scenarios}) == 2


@pytest.mark.parametrize(
    ("field", "element", "period", "value"),
    [
        ("flows", "L", 0, 11.0),
        ("dispatch", "G", 1, 20.0),
        ("unmet", "A", 0, 11.0),
        ("charge", "B", 0, 4.0),
        ("soc", "B", 0, 1.0),
        ("soc", "B", 1, 4.0),
    ],
)
def test_stochastic_scenario_validator_rejects_missing_constraint_violations(
    field, element, period, value
):
    problem = _problem()
    trajectories = _trajectories()
    trajectories[field][element][period] = value
    assert not _stochastic_report(problem, trajectories).passed


@pytest.mark.parametrize("field", ["dispatch", "flows", "unmet", "charge", "discharge", "soc"])
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_validate_schedule_rejects_non_finite_trajectory_values(field, bad_value):
    problem = _problem()
    trajectories = _trajectories()
    trajectories[field][next(iter(trajectories[field]))][0] = bad_value
    report = validate_schedule(
        problem, trajectories["dispatch"], trajectories["flows"], trajectories["unmet"],
        trajectories["charge"], trajectories["discharge"], trajectories["soc"],
    )
    finite_check = next(check for check in report.checks if check.name == "finite_values")
    assert not finite_check.passed
    assert not report.passed


def test_validate_schedule_rejects_non_finite_reported_objective():
    problem = _problem()
    trajectories = _trajectories()
    report = validate_schedule(
        problem, trajectories["dispatch"], trajectories["flows"], trajectories["unmet"],
        trajectories["charge"], trajectories["discharge"], trajectories["soc"],
        reported_objective=float("nan"),
    )
    assert not next(check for check in report.checks if check.name == "finite_values").passed
    assert not report.passed


def test_validate_schedule_still_accepts_finite_feasible_trajectory():
    problem = _problem()
    trajectories = _trajectories()
    report = validate_schedule(
        problem, trajectories["dispatch"], trajectories["flows"], trajectories["unmet"],
        trajectories["charge"], trajectories["discharge"], trajectories["soc"],
    )
    assert report.passed
