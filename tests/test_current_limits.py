"""The hard bounds on charging current.

The blueprint promises that the value written to the charger never leaves the
range configured in the UI, whatever the arithmetic produces.
"""

from __future__ import annotations

import pytest
from conftest import NUMBER, SOC, charging_since, setpoint
from ha_sim import State, moment


def test_normal_case_lands_between_the_bounds(evaluate):
    ctx = evaluate(moment(23, 0))
    assert 6 <= ctx["desired_current"] <= 28


def test_tiny_demand_is_lifted_to_the_minimum(evaluate):
    """One percent left over eight hours would call for a fraction of an amp."""
    ctx = evaluate(moment(23, 10), **{SOC: 99})
    assert ctx["calc_current"] < 1
    assert ctx["desired_current"] == 6


def test_impossible_demand_is_capped_at_the_maximum(evaluate):
    """Ninety minutes to add seventy percent is not going to happen."""
    ctx = evaluate(moment(5, 30), **{SOC: 30, "switch.charger": charging_since(moment(5, 30))})
    assert ctx["calc_current"] > 100
    assert ctx["desired_current"] == 28


@pytest.mark.parametrize("low,high", [(6, 28), (8, 16), (10, 10), (6, 32)])
def test_configured_bounds_are_respected(evaluate, low, high):
    for hour, soc in [(23, 20), (2, 55), (5, 30), (6, 30)]:
        ctx = evaluate(
            moment(hour, 0),
            inputs={"min_current": low, "max_current": high},
            **{SOC: soc},
        )
        assert low <= ctx["desired_current"] <= high


def test_entity_limits_can_narrow_but_never_widen_the_range(evaluate):
    """A charger that only accepts 6-16 A must not be sent 28 A."""
    ctx = evaluate(
        moment(5, 30),
        **{NUMBER: setpoint(10, moment(5, 30), low=6.0, high=16.0), SOC: 30},
    )
    assert ctx["num_max"] == 16
    assert ctx["desired_current"] == 16


def test_entity_limits_cannot_push_below_the_configured_minimum(evaluate):
    """Even if the entity allows 1 A, the UI minimum wins."""
    ctx = evaluate(
        moment(23, 10),
        inputs={"min_current": 10},
        **{NUMBER: setpoint(12, moment(23, 10), low=1.0, high=32.0), SOC: 99},
    )
    assert ctx["num_min"] == 10
    assert ctx["desired_current"] == 10


def test_inverted_bounds_collapse_safely(evaluate):
    """Minimum above maximum is a misconfiguration; it must not crash or invert."""
    ctx = evaluate(moment(23, 0), inputs={"min_current": 20, "max_current": 10})
    assert ctx["num_min"] == 20
    assert ctx["num_max"] == 20
    assert ctx["desired_current"] == 20


@pytest.mark.parametrize("step,expected_multiple", [(1, 1), (2, 2), (3, 3), (5, 5)])
def test_current_snaps_to_the_configured_step(evaluate, step, expected_multiple):
    ctx = evaluate(moment(23, 0), inputs={"current_step": step})
    assert ctx["desired_current"] % expected_multiple == 0


def test_step_rounds_up_so_the_plan_is_never_short(evaluate):
    plain = evaluate(moment(23, 0), inputs={"current_step": 1})
    coarse = evaluate(moment(23, 0), inputs={"current_step": 5})
    assert coarse["desired_current"] >= plain["calc_current"]
    assert coarse["desired_current"] % 5 == 0


def test_entity_step_attribute_is_ignored(evaluate):
    """Only the UI setting controls granularity, per the discussion in v2."""
    half = evaluate(moment(23, 0), **{NUMBER: setpoint(10, moment(23, 0), step=0.5)})
    assert half["cur_step"] == 1
    assert half["desired_current"] == int(half["desired_current"])


def test_cold_weather_overrides_the_stretch_and_goes_to_maximum(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"outside_temp_sensor": "sensor.outside", "cold_threshold": -5},
        **{"sensor.outside": -23},
    )
    assert ctx["cold_mode"] is True
    assert ctx["desired_current"] == 28


def test_cold_weather_is_off_by_default(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"outside_temp_sensor": "sensor.outside"},
        **{"sensor.outside": -23},
    )
    assert ctx["cold_mode"] is False


def test_unavailable_temperature_does_not_trigger_cold_mode(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"outside_temp_sensor": "sensor.outside", "cold_threshold": -5},
        **{"sensor.outside": State("unavailable")},
    )
    assert ctx["cold_mode"] is False


def test_emergency_charge_uses_the_maximum(evaluate):
    ctx = evaluate(moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12})
    assert ctx["emergency"] is True
    assert ctx["desired_current"] == 28
    assert ctx["should_charge"] is True


def test_fallback_current_is_used_when_no_plan_is_possible(evaluate):
    ctx = evaluate(moment(1, 0), inputs={"car_battery_sensor": []})
    assert ctx["plan_source"] == "none"
    assert ctx["desired_current"] == 10


@pytest.mark.parametrize(
    "maximum,step",
    [(28, 5), (28, 3), (16, 5), (10, 3), (30, 4), (7, 2)],
)
def test_snapping_up_never_pushes_the_current_past_the_maximum(
    evaluate, maximum, step
):
    """Rounding up to the step can overshoot a ceiling that is not a multiple
    of it. The clamp must be applied after snapping, not only before."""
    ctx = evaluate(
        moment(5, 30),
        inputs={"min_current": 6, "max_current": maximum, "current_step": step},
        **{SOC: 20, "switch.charger": charging_since(moment(5, 30))},
    )
    assert ctx["calc_current"] > maximum, "the scenario must demand more than the cap"
    assert ctx["desired_current"] <= maximum


@pytest.mark.parametrize("minimum,step", [(7, 5), (8, 3), (11, 4)])
def test_snapping_up_respects_a_minimum_that_is_not_a_multiple_of_the_step(
    evaluate, minimum, step
):
    ctx = evaluate(
        moment(23, 10),
        inputs={"min_current": minimum, "max_current": 28, "current_step": step},
        **{SOC: 99},
    )
    assert ctx["desired_current"] >= minimum


def test_the_written_value_always_lies_inside_the_entity_range(evaluate):
    """Whatever the combination, the number entity must accept the value."""
    for maximum, step, soc, hour in [
        (28, 5, 20, 5), (16, 3, 30, 6), (10, 4, 50, 4), (32, 5, 10, 6),
    ]:
        ctx = evaluate(
            moment(hour, 30),
            inputs={"min_current": 6, "max_current": maximum, "current_step": step},
            **{SOC: soc, "switch.charger": charging_since(moment(hour, 30))},
        )
        assert ctx["num_min"] <= ctx["desired_current"] <= ctx["num_max"]
