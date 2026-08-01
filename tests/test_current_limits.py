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


def test_the_emergency_top_up_does_not_quit_on_the_threshold(evaluate):
    """Stopping the moment the threshold is crossed would leave the car at the
    bare minimum and chatter on and off as the percentage wobbles."""
    now = moment(14, 0)
    ctx = evaluate(
        now,
        inputs={"emergency_soc": 20},
        **{SOC: 22, "switch.charger": charging_since(now)},
    )
    assert ctx["emergency"] is True
    assert ctx["must_stop"] is False


def test_the_emergency_top_up_ends_once_the_margin_is_reached(evaluate):
    now = moment(14, 0)
    ctx = evaluate(
        now,
        inputs={"emergency_soc": 20, "emergency_hysteresis": 10},
        **{SOC: 31, "switch.charger": charging_since(now)},
    )
    assert ctx["emergency"] is False
    assert ctx["must_stop"] is True


def test_an_idle_charger_only_enters_the_emergency_below_the_threshold(evaluate):
    """The margin applies to finishing the top-up, not to starting one."""
    ctx = evaluate(moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 25})
    assert ctx["emergency"] is False
    assert ctx["should_charge"] is False


def test_fallback_current_is_used_when_no_plan_is_possible(evaluate):
    ctx = evaluate(moment(1, 0), inputs={"car_battery_sensor": []})
    assert ctx["plan_source"] == "none"
    assert ctx["desired_current"] == 10


def test_a_custom_fallback_current_is_honoured(evaluate):
    ctx = evaluate(
        moment(1, 0),
        inputs={"car_battery_sensor": [], "fallback_current": 16},
    )
    assert ctx["desired_current"] == 16


# ------------------------------------------------------------------- phases


def test_three_phases_need_a_third_of_the_current(evaluate):
    """Three phases carry three times the power at the same amperage.

    Ignoring the phase count would triple the real load and trip the breaker
    the blueprint is supposed to stay under.
    """
    single = evaluate(moment(23, 0), inputs={"phases": "1"})
    three = evaluate(moment(23, 0), inputs={"phases": "3"})
    assert three["calc_current"] == pytest.approx(single["calc_current"] / 3, rel=0.02)


def test_the_phase_count_does_not_change_the_energy_budget(evaluate):
    """Phases affect how fast the energy arrives, not how much is needed."""
    single = evaluate(moment(23, 0), inputs={"phases": "1"})
    three = evaluate(moment(23, 0), inputs={"phases": "3"})
    assert three["needed_kwh"] == pytest.approx(single["needed_kwh"])


# --------------------------------------------------------------- efficiency


@pytest.mark.parametrize(
    "efficiency,expected_kwh",
    [(100, 27.95), (88, 31.76), (60, 46.58)],
)
def test_the_budget_is_grossed_up_by_the_efficiency(
    evaluate, efficiency, expected_kwh
):
    """Losses mean more has to come out of the wall than lands in the battery."""
    ctx = evaluate(moment(23, 0), inputs={"efficiency": efficiency})
    assert ctx["needed_kwh"] == pytest.approx(expected_kwh, rel=1e-3)


def test_a_worse_efficiency_asks_for_more_current(evaluate):
    lossy = evaluate(moment(23, 0), inputs={"efficiency": 70})
    clean = evaluate(moment(23, 0), inputs={"efficiency": 95})
    assert lossy["calc_current"] > clean["calc_current"]


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
