"""Command throttling and the health watchdog."""

from __future__ import annotations

import pytest
from conftest import (
    AMPERE,
    CAR_CHARGING,
    MODE,
    NUMBER,
    POWER,
    SOC,
    STATUS,
    charging_since,
    setpoint,
)
from ha_sim import State, moment

# ------------------------------------------------------------------ throttle


@pytest.mark.parametrize(
    "age_seconds,elapsed",
    [(0, False), (10, False), (45, False), (59, False), (60, True), (18000, True)],
)
def test_the_gap_is_measured_from_the_last_setpoint_change(
    evaluate, age_seconds, elapsed
):
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(20, now, age_seconds=age_seconds)})
    assert ctx["gap_elapsed"] is elapsed


def test_a_longer_gap_holds_the_write_back_longer(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"command_gap": 300},
        **{NUMBER: setpoint(20, now, age_seconds=60)},
    )
    assert ctx["gap_elapsed"] is False


def test_a_zero_gap_never_holds_a_write_back(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now, inputs={"command_gap": 0}, **{NUMBER: setpoint(20, now, age_seconds=0)}
    )
    assert ctx["gap_elapsed"] is True


def test_a_missing_setpoint_entity_does_not_block_forever(evaluate):
    world_now = moment(23, 0)
    ctx = evaluate(world_now, **{NUMBER: None})
    assert ctx["current_age"] == 999999
    assert ctx["gap_elapsed"] is True


def test_a_fresh_setpoint_defers_the_write_to_the_next_tick(evaluate):
    """The throttle is a condition, not a pause.

    An automation in ``restart`` mode cannot rely on a blocking delay: any
    trigger firing during it kills the delay and the command that follows.
    Holding the write back until the next recalculation is restart-proof.
    """
    now = moment(5, 0)
    ctx = evaluate(
        now,
        **{
            NUMBER: setpoint(10, now, age_seconds=5),
            SOC: 40,
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["want_write"] is True
    assert ctx["gap_elapsed"] is False
    assert ctx["needs_write"] is False


def test_the_first_command_of_a_session_ignores_the_gap(evaluate):
    """Otherwise starting a charge would be delayed by the whole gap."""
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(10, now, age_seconds=0)})
    assert ctx["switch_on"] is False
    assert ctx["gap_elapsed"] is False
    assert ctx["needs_write"] is True


# ------------------------------------------------------------------ deadband


def test_small_drift_does_not_rewrite_the_setpoint(evaluate):
    """Recalculating every half hour always yields a slightly different number."""
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(20, now), "switch.charger": charging_since(now)})
    assert abs(ctx["desired_current"] - ctx["current_now"]) < 3
    assert ctx["needs_write"] is False


def test_a_real_change_does_rewrite_the_setpoint(evaluate):
    now = moment(5, 0)
    ctx = evaluate(
        now, **{NUMBER: setpoint(10, now), SOC: 40, "switch.charger": charging_since(now)}
    )
    assert ctx["needs_write"] is True


def test_the_deadband_compares_setpoint_with_setpoint(evaluate):
    """The measured current is always a little below the setpoint. That gap
    must not be mistaken for drift and cause a write on every tick."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        **{
            NUMBER: setpoint(20, now),
            AMPERE: 18.3,
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["current_now"] == 20
    assert ctx["actual_current"] == 18.3
    assert ctx["needs_write"] is False


def test_starting_a_session_always_writes(evaluate):
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(20, now)})
    assert ctx["switch_on"] is False
    assert ctx["needs_write"] is True


def test_a_narrower_deadband_writes_more_often(evaluate):
    now = moment(23, 0)
    world = {NUMBER: setpoint(20, now), "switch.charger": charging_since(now)}
    assert evaluate(now, inputs={"current_deadband": 3}, **world)["needs_write"] is False
    assert evaluate(now, inputs={"current_deadband": 0}, **world)["needs_write"] is True


# ------------------------------------------------------------------ watchdog


def test_no_alarm_while_power_is_flowing(evaluate):
    now = moment(1, 0)
    ctx = evaluate(now, **{"switch.charger": charging_since(now, 40)})
    assert ctx["no_power_alarm"] is False
    assert ctx["alarm_reason"] == "none"


def test_alarm_after_the_grace_period_without_power(evaluate):
    now = moment(1, 0)
    ctx = evaluate(now, **{POWER: 0, "switch.charger": charging_since(now, 40)})
    assert ctx["no_power_alarm"] is True


def test_no_alarm_before_the_grace_period_expires(evaluate):
    now = moment(1, 0)
    ctx = evaluate(now, **{POWER: 0, "switch.charger": charging_since(now, 3)})
    assert ctx["no_power_alarm"] is False


def test_no_alarm_when_the_charger_is_off(evaluate):
    ctx = evaluate(moment(1, 0), **{POWER: 0})
    assert ctx["no_power_alarm"] is False


def test_no_alarm_when_the_cable_is_unplugged(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now, **{POWER: 0, STATUS: "available", "switch.charger": charging_since(now, 40)}
    )
    assert ctx["no_power_alarm"] is False


def test_watchdog_can_be_disabled(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"watchdog_minutes": 0},
        **{POWER: 0, "switch.charger": charging_since(now, 40)},
    )
    assert ctx["no_power_alarm"] is False


def test_no_alarm_without_a_power_sensor_while_the_status_says_charging(evaluate):
    """The status is the fallback evidence that current is flowing."""
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"charger_power_sensor": []},
        **{"switch.charger": charging_since(now, 40)},
    )
    assert ctx["charging_now"] is True
    assert ctx["no_power_alarm"] is False


@pytest.mark.parametrize(
    "car_state,expected",
    [
        ("off", "car_refused_charge"),
        ("on", "charger_reports_no_power_but_car_charging"),
    ],
)
def test_the_car_sensor_refines_the_diagnosis(evaluate, car_state, expected):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"car_charging_binary": CAR_CHARGING},
        **{
            POWER: 0,
            CAR_CHARGING: car_state,
            "switch.charger": charging_since(now, 40),
        },
    )
    assert ctx["alarm_reason"] == expected


def test_without_the_car_sensor_the_diagnosis_stays_generic(evaluate):
    now = moment(1, 0)
    ctx = evaluate(now, **{POWER: 0, "switch.charger": charging_since(now, 40)})
    assert ctx["car_says_charging"] == "unknown"
    assert ctx["alarm_reason"] == "no_power_while_on"


def test_a_charger_fault_outranks_every_other_alarm(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        **{
            STATUS: "fault",
            POWER: 0,
            SOC: State("unavailable"),
            "switch.charger": charging_since(now, 40),
        },
    )
    assert ctx["alarm_reason"] == "charger_fault"


def test_the_watchdog_works_without_a_power_sensor(evaluate):
    """Without the sensor, "is current flowing" falls back to the charger
    status. Requiring the sensor would silently disable the watchdog for
    exactly the setups that have the least instrumentation."""
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"charger_power_sensor": []},
        **{STATUS: "plugged_in", "switch.charger": charging_since(now, 40)},
    )
    assert ctx["charging_now"] is False
    assert ctx["no_power_alarm"] is True
    assert ctx["alarm_reason"] == "no_power_while_on"


def test_the_alarm_stops_repeating_after_an_hour(evaluate):
    """Reporting once is help; reporting every recalculation until morning is
    a reason to mute the notification channel."""
    now = moment(1, 0)
    ctx = evaluate(now, **{POWER: 0, "switch.charger": charging_since(now, 300)})
    assert ctx["no_power_alarm"] is False


# ------------------------------------------------- throttling other commands


def test_a_charger_that_refuses_to_switch_on_is_not_hammered(evaluate):
    """If turn_on has no effect the switch stays off, and without throttling
    both the command and the start notification would repeat on every tick."""
    now = moment(23, 0)
    fresh = evaluate(now, **{"switch.charger": State("off", last_changed=now)})
    assert fresh["should_charge"] is True
    assert fresh["needs_turn_on"] is False

    settled = evaluate(now)
    assert settled["needs_turn_on"] is True


def test_an_empty_mode_value_is_never_written(evaluate):
    """An empty option is rejected by the charger, and continue_on_error would
    swallow the failure, leaving the mode never actually set."""
    ctx = evaluate(
        moment(23, 0),
        inputs={"charger_mode_value": ""},
        **{MODE: "scheduled_charge"},
    )
    assert ctx["needs_mode_write"] is False


def test_the_mode_is_written_when_it_genuinely_differs(evaluate):
    ctx = evaluate(moment(23, 0), **{MODE: "scheduled_charge"})
    assert ctx["needs_mode_write"] is True
