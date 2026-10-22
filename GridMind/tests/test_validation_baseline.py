from gridmind.benchmark import compare_to_baseline, copperplate_lower_bound, proportional_baseline_cost
from gridmind.optimizer import SolveResult
from gridmind.validation import Check, ValidationReport, validate_solution
from helpers import tiny_network, two_gen_network


def good():
    return {"cheap": 30.0}, {"AB": 30.0}, {"A": 0.0, "B": 20.0}


def test_valid_solution_passes():
    d, f, u = good()
    rep = validate_solution(tiny_network(), d, f, u, reported_objective=30 * 10 + 20 * 1000)
    assert rep.passed, [c for c in rep.failed()]


def test_detects_capacity_violation():
    rep = validate_solution(tiny_network(), {"cheap": 50.0}, {"AB": 50.0}, {"A": 0.0, "B": 0.0})
    assert {c.name for c in rep.failed()} == {"line_capacity"}


def test_detects_power_balance_violation():
    rep = validate_solution(tiny_network(), {"cheap": 30.0}, {"AB": 30.0}, {"A": 0.0, "B": 0.0})
    assert "power_balance" in {c.name for c in rep.failed()}


def test_detects_generator_limit_and_renewable_availability():
    rep = validate_solution(tiny_network(), {"cheap": 120.0}, {"AB": 30.0}, {"A": 0.0, "B": 20.0})
    assert "generator_limits" in {c.name for c in rep.failed()}


def test_detects_outage_violation():
    net = tiny_network()
    out = net.__class__(**{**net.__dict__, "lines": (net.lines[0].__class__(**{**net.lines[0].__dict__, "in_service": False}),)})
    rep = validate_solution(out, {"cheap": 30.0}, {"AB": 30.0}, {"A": 0.0, "B": 20.0})
    assert "outages_respected" in {c.name for c in rep.failed()}


def test_detects_wrong_objective_and_missing_values():
    d, f, u = good()
    assert "objective_consistency" in {c.name for c in validate_solution(tiny_network(), d, f, u, reported_objective=1.0).failed()}
    assert not validate_solution(tiny_network(), d, {}, u).passed


def test_baseline_definitions_hand_computed():
    net = tiny_network()  # demand 50, cheap gen 100 MW @10
    assert proportional_baseline_cost(net) == 500.0   # single unit supplies 50 MW
    assert copperplate_lower_bound(net) == 500.0      # ignores the 30 MW line
    two = two_gen_network()  # demand 80; limits 100+100 -> fraction 0.4
    assert proportional_baseline_cost(two) == 0.4 * 100 * 10 + 0.4 * 100 * 50
    assert copperplate_lower_bound(two) == 80 * 10


def test_compare_handles_failed_solve_without_inventing_numbers():
    net = tiny_network()
    failed = SolveResult("infeasible", "glpk", "infeasible", "x", 0.0)
    cmp = compare_to_baseline(net, failed)
    assert cmp["saving_usd"] is None and cmp["congestion_cost_usd"] is None


# --------------------------------------------------- validation facets ----
def test_check_defaults_and_serialization():
    c = Check("n", True, 0.0)
    assert c.severity == "error" and c.category == "physical"
    d = vars(c).copy()
    assert d == {"name": "n", "passed": True, "max_violation": 0.0, "detail": "",
                 "severity": "error", "category": "physical"}


def test_report_facets_default_none_and_warnings_empty():
    rep = ValidationReport(True)
    assert rep.physical_feasible is None
    assert rep.model_valid is None
    assert rep.battery_exclusive is None
    assert rep.warnings() == []


def test_warnings_only_returns_failed_warning_checks():
    checks = [Check("a", False, 1.0, severity="warning"),
              Check("b", False, 1.0, severity="error"),
              Check("c", True, 0.0, severity="warning")]
    rep = ValidationReport(False, checks)
    assert [c.name for c in rep.warnings()] == ["a"]
    assert [c.name for c in rep.failed()] == ["a", "b"]


def test_single_period_facets_populated_exclusivity_none():
    d, f, u = good()
    rep = validate_solution(tiny_network(), d, f, u, reported_objective=30 * 10 + 20 * 1000)
    assert rep.passed and rep.physical_feasible and rep.model_valid
    assert rep.battery_exclusive is None  # single-period path never evaluates exclusivity
    assert {"physical_feasible", "model_valid", "battery_exclusive"} <= set(rep.to_dict())


def test_single_period_bad_solution_keeps_passed_false():
    rep = validate_solution(tiny_network(), {"cheap": 50.0}, {"AB": 50.0}, {"A": 0.0, "B": 0.0})
    assert rep.passed is False
    assert {c.name for c in rep.failed()} == {"line_capacity"}
    assert rep.model_valid is True  # no objective supplied -> model category empty -> True
    assert rep.battery_exclusive is None
