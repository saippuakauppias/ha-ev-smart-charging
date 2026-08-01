"""Command throttling and the health watchdog."""

from __future__ import annotations

import pytest
from conftest import (
    AMPERE,
    CAR_CHARGING,
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
    "age_seconds,expected_wait",
    [(0, 60), (10, 50), (45, 15), (59, 1), (60, 0), (90, 0), (18000, 0)],
)
def test_writes_wait_out_the_configured_gap(evaluate, age_seconds, expected_wait):
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(20, now, age_seconds=age_seconds)})
    assert ctx["wait_before_write"] == expected_wait


def test_a_longer_gap_produces_a_longer_wait(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"command_gap": 300},
        **{NUMBER: setpoint(20, now, age_seconds=60)},
    )
    assert ctx["wait_before_write"] == 240


def test_a_zero_gap_disables_waiting(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now, inputs={"command_gap": 0}, **{NUMBER: setpoint(20, now, age_seconds=0)}
    )
    assert ctx["wait_before_write"] == 0


def test_a_missing_setpoint_entity_does_not_block_forever(evaluate):
    world_now = moment(23, 0)
    ctx = evaluate(world_now, **{NUMBER: None})
    assert ctx["current_age"] == 999999
    assert ctx["wait_before_write"] == 0


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


def test_watchdog_needs_a_power_sensor(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"charger_power_sensor": []},
        **{"switch.charger": charging_since(now, 40)},
    )
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
