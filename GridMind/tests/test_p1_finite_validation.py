"""P1 input-validation tests: NaN / Inf values must be rejected with actionable errors."""
import math

import pytest

from gridmind.multiperiod import Battery
from gridmind.network import Generator, Line, Network, Node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
NAN = float("nan")
INF = float("inf")


def _net(**kw):
    """Return a minimal Network with keyword overrides."""
    defaults = dict(
        name="t",
        nodes=(Node("A", "A", 0.0),),
        generators=(),
        lines=(),
        unmet_penalty_per_mwh=1000.0,
        interval_hours=1.0,
    )
    defaults.update(kw)
    return Network(**defaults)


def _gen(**kw):
    defaults = dict(
        id="G", name="G", node="A", fuel="gas",
        capacity_mw=100.0, cost_per_mwh=10.0,
    )
    defaults.update(kw)
    return Generator(**defaults)


def _line(**kw):
    defaults = dict(id="L", name="L", from_node="A", to_node="B", capacity_mw=50.0)
    defaults.update(kw)
    return Line(**defaults)


def _bat(**kw):
    defaults = dict(
        id="B", node="A",
        capacity_mwh=50.0, max_charge_mw=10.0, max_discharge_mw=10.0,
        eta_charge=0.95, eta_discharge=0.95,
        initial_soc_mwh=5.0, min_soc_mwh=0.0,
        final_soc_mwh=None, throughput_cost_per_mwh=0.0,
    )
    defaults.update(kw)
    return Battery(**defaults)


_NODE_IDS = {"A"}

TWO_NODES = (Node("A", "A", 0.0), Node("B", "B", 0.0))


# ---------------------------------------------------------------------------
# Network — node demand_mw
# ---------------------------------------------------------------------------
class TestNodeDemandFinite:
    def test_nan_demand_raises(self):
        with pytest.raises(ValueError, match="demand_mw"):
            _net(nodes=(Node("A", "A", NAN),)).validate()

    def test_inf_demand_raises(self):
        with pytest.raises(ValueError, match="demand_mw"):
            _net(nodes=(Node("A", "A", INF),)).validate()


# ---------------------------------------------------------------------------
# Network — Generator fields
# ---------------------------------------------------------------------------
class TestGeneratorFinite:
    def test_nan_capacity_raises(self):
        with pytest.raises(ValueError, match="capacity_mw"):
            _net(generators=(_gen(capacity_mw=NAN),)).validate()

    def test_inf_capacity_raises(self):
        with pytest.raises(ValueError, match="capacity_mw"):
            _net(generators=(_gen(capacity_mw=INF),)).validate()

    def test_inf_cost_raises(self):
        with pytest.raises(ValueError, match="cost_per_mwh"):
            _net(generators=(_gen(cost_per_mwh=INF),)).validate()

    def test_nan_cost_raises(self):
        with pytest.raises(ValueError, match="cost_per_mwh"):
            _net(generators=(_gen(cost_per_mwh=NAN),)).validate()

    def test_nan_availability_raises(self):
        with pytest.raises(ValueError, match="availability_mw"):
            _net(generators=(_gen(availability_mw=NAN),)).validate()

    def test_inf_ramp_up_raises(self):
        with pytest.raises(ValueError, match="ramp_up_mw"):
            _net(generators=(_gen(ramp_up_mw=INF),)).validate()

    def test_nan_ramp_down_raises(self):
        with pytest.raises(ValueError, match="ramp_down_mw"):
            _net(generators=(_gen(ramp_down_mw=NAN),)).validate()


# ---------------------------------------------------------------------------
# Network — Line capacity
# ---------------------------------------------------------------------------
class TestLineFinite:
    def test_nan_line_capacity_raises(self):
        with pytest.raises(ValueError, match="capacity_mw"):
            Network("t", TWO_NODES, (), (_line(capacity_mw=NAN),), 1000.0, 1.0).validate()

    def test_inf_line_capacity_raises(self):
        with pytest.raises(ValueError, match="capacity_mw"):
            Network("t", TWO_NODES, (), (_line(capacity_mw=INF),), 1000.0, 1.0).validate()


# ---------------------------------------------------------------------------
# Network — scalar fields
# ---------------------------------------------------------------------------
class TestNetworkScalarsFinite:
    def test_nan_penalty_raises(self):
        with pytest.raises(ValueError, match="unmet_penalty_per_mwh"):
            _net(unmet_penalty_per_mwh=NAN).validate()

    def test_inf_penalty_raises(self):
        with pytest.raises(ValueError, match="unmet_penalty_per_mwh"):
            _net(unmet_penalty_per_mwh=INF).validate()

    def test_inf_interval_hours_raises(self):
        with pytest.raises(ValueError, match="interval_hours"):
            _net(interval_hours=INF).validate()

    def test_nan_interval_hours_raises(self):
        with pytest.raises(ValueError, match="interval_hours"):
            _net(interval_hours=NAN).validate()


# ---------------------------------------------------------------------------
# Battery — finite field guards
# ---------------------------------------------------------------------------
class TestBatteryFinite:
    def test_nan_capacity_mwh_raises(self):
        with pytest.raises(ValueError):
            _bat(capacity_mwh=NAN).validate(_NODE_IDS)

    def test_inf_max_charge_raises(self):
        with pytest.raises(ValueError, match="max_charge_mw"):
            _bat(max_charge_mw=INF).validate(_NODE_IDS)

    def test_nan_max_discharge_raises(self):
        with pytest.raises(ValueError, match="max_discharge_mw"):
            _bat(max_discharge_mw=NAN).validate(_NODE_IDS)

    def test_inf_initial_soc_raises(self):
        with pytest.raises(ValueError):
            _bat(initial_soc_mwh=INF).validate(_NODE_IDS)

    def test_nan_throughput_cost_raises(self):
        with pytest.raises(ValueError, match="throughput_cost_per_mwh"):
            _bat(throughput_cost_per_mwh=NAN).validate(_NODE_IDS)

    def test_nan_final_soc_raises(self):
        with pytest.raises(ValueError):
            _bat(final_soc_mwh=NAN).validate(_NODE_IDS)


# ---------------------------------------------------------------------------
# Regression guards — valid inputs must still pass
# ---------------------------------------------------------------------------
class TestValidInputsUnchanged:
    def test_valid_network_passes(self):
        net = Network(
            "t",
            (Node("A", "A", 50.0),),
            (_gen(),),
            (),
            1000.0,
            1.0,
        )
        net.validate()  # must not raise

    def test_valid_battery_passes(self):
        _bat().validate(_NODE_IDS)  # must not raise

    def test_zero_demand_passes(self):
        _net(nodes=(Node("A", "A", 0.0),)).validate()

    def test_zero_cost_passes(self):
        _net(generators=(_gen(cost_per_mwh=0.0),)).validate()

    def test_none_availability_passes(self):
        _net(generators=(_gen(availability_mw=None),)).validate()

    def test_none_ramps_passes(self):
        _net(generators=(_gen(ramp_up_mw=None, ramp_down_mw=None),)).validate()

    def test_none_final_soc_passes(self):
        _bat(final_soc_mwh=None).validate(_NODE_IDS)
