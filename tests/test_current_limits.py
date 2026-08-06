"""The hard bounds on charging current.

The blueprint promises that the value written to the charger never leaves the
range configured in the UI, whatever the arithmetic produces.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import NUMBER, OUTSIDE, SOC, charging_since, setpoint
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


def test_inverted_bounds_collapse_towards_the_maximum(evaluate):
    """Minimum above maximum is a misconfiguration, and the two fields are easy
    to swap in the UI. It must collapse downwards: lowering the floor is safe,
    raising the ceiling above what the wiring allows is not."""
    ctx = evaluate(moment(23, 0), inputs={"min_current": 20, "max_current": 10})
    assert ctx["num_min"] == 10
    assert ctx["num_max"] == 10
    assert ctx["desired_current"] == 10


def test_swapped_bounds_never_exceed_the_intended_ceiling(evaluate):
    """The dangerous case: 80 in the minimum field, 6 in the maximum."""
    ctx = evaluate(moment(23, 0), inputs={"min_current": 80, "max_current": 6})
    assert ctx["desired_current"] == 6


@pytest.mark.parametrize("step", [2, 3, 5])
def test_current_snaps_to_the_configured_step(evaluate, step):
    # A step of 1 is deliberately absent: ``x % 1 == 0`` holds for every whole
    # number, so that case asserted nothing. It is covered below instead.
    ctx = evaluate(moment(23, 0), inputs={"current_step": step})
    assert ctx["desired_current"] % step == 0


def test_a_step_of_one_still_yields_a_whole_number_of_amps(evaluate):
    ctx = evaluate(moment(23, 0), inputs={"current_step": 1})
    assert ctx["desired_current"] == int(ctx["desired_current"])


@pytest.mark.parametrize("maximum,step", [(28, 5), (28, 3), (16, 5), (30, 4), (32, 5)])
def test_the_setpoint_is_a_multiple_of_the_step_even_at_the_ceiling(
    evaluate, maximum, step
):
    """A charger with a coarse step rejects anything in between.

    Clamping to a ceiling that is not itself a multiple of the step produces
    exactly such a value (28 with a step of 5). The charger then rounds it to
    its own liking, our setpoint never matches what we asked for, and the
    boundary rule rewrites it on every single recalculation.
    """
    now = moment(5, 30)
    ctx = evaluate(
        now,
        inputs={"min_current": 6, "max_current": maximum, "current_step": step},
        **{SOC: 20, "switch.charger": charging_since(now)},
    )
    assert ctx["calc_current"] > maximum, "the scenario must demand the ceiling"
    assert ctx["num_max"] % step == 0
    assert ctx["desired_current"] % step == 0
    assert ctx["desired_current"] <= maximum


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
        inputs={"outside_temp_sensor": OUTSIDE, "cold_threshold": -5},
        **{OUTSIDE: -23},
    )
    assert ctx["cold_mode"] is True
    assert ctx["desired_current"] == 28


def test_cold_weather_is_off_by_default(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"outside_temp_sensor": OUTSIDE},
        **{OUTSIDE: -23},
    )
    assert ctx["cold_mode"] is False


def test_unavailable_temperature_does_not_trigger_cold_mode(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"outside_temp_sensor": OUTSIDE, "cold_threshold": -5},
        **{OUTSIDE: State("unavailable")},
    )
    assert ctx["cold_mode"] is False


def test_a_stale_temperature_is_accepted_unless_a_limit_is_set(evaluate):
    """Weather integrations refresh rarely, and in winter the reading can stay
    the same for half a night. Age checking is therefore off by default."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"outside_temp_sensor": OUTSIDE, "cold_threshold": -5},
        **{OUTSIDE: State(-23, last_changed=now - dt.timedelta(hours=9))},
    )
    assert ctx["otemp_fresh"] is True
    assert ctx["cold_mode"] is True


def test_a_configured_age_limit_rejects_a_stuck_temperature(evaluate):
    """A sensor frozen at -20 would otherwise pin the current at maximum and
    switch the stretching off for good."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={
            "outside_temp_sensor": OUTSIDE,
            "cold_threshold": -5,
            "cold_temp_max_age": 120,
        },
        **{OUTSIDE: State(-23, last_changed=now - dt.timedelta(hours=9))},
    )
    assert ctx["otemp_age_min"] == 540.0
    assert ctx["otemp_fresh"] is False
    assert ctx["cold_mode"] is False


def test_a_fresh_temperature_passes_the_age_limit(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={
            "outside_temp_sensor": OUTSIDE,
            "cold_threshold": -5,
            "cold_temp_max_age": 120,
        },
        **{OUTSIDE: State(-23, last_changed=now - dt.timedelta(minutes=10))},
    )
    assert ctx["otemp_fresh"] is True
    assert ctx["cold_mode"] is True


def test_emergency_charge_uses_the_maximum(evaluate):
    ctx = evaluate(moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12})
    assert ctx["emergency"] is True
    assert ctx["desired_current"] == 28
    assert ctx["should_charge"] is True


def test_the_emergency_top_up_does_not_quit_on_the_threshold(evaluate):
    """Stopping the moment the threshold is crossed would leave the car at the
    bare minimum and chatter on and off as the percentage wobbles."""
    now = moment(14, 0)
    # Ten minutes in, not two hours: at the maximum current a top-up that had
    # been running that long would be far past the threshold, and a fixture
    # that cannot happen proves nothing about the behaviour under test.
    ctx = evaluate(
        now,
        inputs={"emergency_soc": 20},
        **{SOC: 21, "switch.charger": charging_since(now, minutes=10)},
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


@pytest.mark.parametrize("reading", ["unavailable", "unknown", "недоступно"])
def test_an_emergency_never_fires_on_unusable_charge_data(evaluate, reading):
    """The one guard between a dead integration and 32 A in the afternoon.

    Invalid data drives ``soc`` to the -1 sentinel, and -1 is below *every*
    threshold. Without the ``soc_valid`` check the emergency top-up would
    therefore read "the battery is critically low" precisely when nothing at
    all is known about the battery — and the emergency path is the one that
    ignores the window, ignores the gentle finish and asks for the ceiling.

    A cloud integration losing its token at midday is the ordinary way to get
    here, not an exotic one.
    """
    ctx = evaluate(
        moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: State(reading)}
    )
    assert ctx["soc_valid"] is False
    assert ctx["soc"] == -1, "the sentinel is below any threshold"
    assert ctx["emergency"] is False, "unknown is not an emergency"
    assert ctx["should_charge"] is False


def test_an_emergency_still_fires_on_a_genuinely_low_battery(evaluate):
    """The mirror of the test above: the guard must not disarm the feature."""
    ctx = evaluate(moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12})
    assert ctx["soc_valid"] is True
    assert ctx["emergency"] is True
    assert ctx["should_charge"] is True


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


# ----------------------------------------------- limits declared by the entity


def test_the_entity_ceiling_narrows_the_configured_maximum(evaluate):
    """A 32 A setting on a 16 A charger must not be sent to the charger."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"max_current": 32},
        **{NUMBER: State(10, {"min": 6.0, "max": 16.0, "step": 1.0}, now)},
    )
    assert ctx["num_max"] == 16
    assert ctx["desired_current"] <= 16


def test_the_entity_floor_raises_the_configured_minimum(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"min_current": 6, "target_soc": 100},
        **{NUMBER: State(10, {"min": 8.0, "max": 32.0, "step": 1.0}, now),
           SOC: 99},
    )
    assert ctx["num_min"] == 8
    assert ctx["desired_current"] >= 8


def test_an_entity_without_limit_attributes_falls_back_to_the_settings(evaluate):
    """Plenty of integrations publish a ``number`` with no min/max at all."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"min_current": 6, "max_current": 28},
        **{NUMBER: State(10, {}, now)},
    )
    assert ctx["num_min"] == 6
    assert ctx["num_max"] == 28
