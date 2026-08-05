"""Command throttling and the health watchdog."""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    AMPERE,
    CAR_CHARGING,
    MODE,
    NUMBER,
    POWER,
    SESSION,
    SOC,
    STATUS,
    SWITCH,
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
    """Recalculating every half hour always yields a slightly different number.

    Only downward drift is absorbed - see the asymmetry tests below.
    """
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(22, now), "switch.charger": charging_since(now)})
    assert abs(ctx["desired_current"] - ctx["current_now"]) < 3
    assert ctx["current_rising"] is False
    assert ctx["needs_write"] is False


# ------------------------------------------------- the deadband is asymmetric


def test_a_request_to_raise_the_current_is_never_absorbed(evaluate):
    """The defect that cost a full charge on the first real night.

    A higher setpoint is the regulator saying "at this rate we miss the
    target". Swallowing that because the step is small means deliberately
    under-charging: four consecutive recalculations asked for 20-21 A against
    a setpoint of 19 A, every one was absorbed by the 3 A deadband, and the
    car reached 83% instead of 100%.
    """
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(20, now), "switch.charger": charging_since(now)})
    assert ctx["desired_current"] > ctx["current_now"]
    assert ctx["desired_current"] - ctx["current_now"] < 3, "well inside the deadband"
    assert ctx["current_rising"] is True
    assert ctx["needs_write"] is True


def test_settling_down_onto_a_boundary_ignores_the_deadband(evaluate):
    """Coming down to exactly the ceiling (or floor) is written even when the
    step is small. Otherwise a charger that rounds the boundary its own way
    leaves the setpoint permanently a little off, and the difference is never
    large enough to clear the deadband - so it would never be corrected.

    Raising the current is already always written; this covers the way down,
    which is the only case where the boundary exception still does work.
    """
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{NUMBER: setpoint(30, now), SOC: 20, "switch.charger": charging_since(now)},
    )
    assert ctx["desired_current"] < ctx["current_now"], "we are coming down"
    assert ctx["current_now"] - ctx["desired_current"] < 3, "inside the deadband"
    assert ctx["at_boundary"] is True
    assert ctx["setpoint_differs"] is True
    assert ctx["needs_write"] is True


def test_a_request_to_lower_the_current_still_waits_for_the_deadband(evaluate):
    """Nothing is at stake in lowering the current: an extra ampere never
    prevents reaching the target, while rewriting the setpoint for fractions
    of an ampere wears out the charger. This is what the deadband is for.

    The deadband is stated explicitly rather than relying on the default, so
    that the test keeps testing the rule if the default is ever retuned.
    """
    now = moment(23, 0)
    world = {NUMBER: setpoint(23, now), "switch.charger": charging_since(now)}
    ctx = evaluate(now, inputs={"current_deadband": 4}, **world)
    assert ctx["desired_current"] < ctx["current_now"]
    assert ctx["current_now"] - ctx["desired_current"] < 4, "inside the deadband"
    assert ctx["current_rising"] is False
    assert ctx["needs_write"] is False


def test_the_deadband_is_a_strict_threshold(evaluate):
    """A drop of exactly the deadband is written; only smaller ones are held
    back. Otherwise "deadband 0" would mean "never write" instead of "write on
    any difference"."""
    now = moment(23, 0)
    world = {NUMBER: setpoint(23, now), "switch.charger": charging_since(now)}
    exactly_two = evaluate(now, inputs={"current_deadband": 2}, **world)
    assert exactly_two["current_now"] - exactly_two["desired_current"] == 2
    assert exactly_two["needs_write"] is False

    smaller = evaluate(now, inputs={"current_deadband": 1}, **world)
    assert smaller["needs_write"] is True


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
            NUMBER: setpoint(22, now),
            AMPERE: 18.3,
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["current_now"] == 22
    assert ctx["actual_current"] == 18.3
    assert ctx["needs_write"] is False


def test_starting_a_session_always_writes(evaluate):
    now = moment(23, 0)
    ctx = evaluate(now, **{NUMBER: setpoint(20, now)})
    assert ctx["switch_on"] is False
    assert ctx["needs_write"] is True


def test_a_narrower_deadband_writes_more_often(evaluate):
    now = moment(23, 0)
    world = {NUMBER: setpoint(23, now), "switch.charger": charging_since(now)}
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
    settled_setpoint = {NUMBER: setpoint(21, now, age_seconds=18000)}

    fresh = evaluate(
        now, **{"switch.charger": State("off", last_changed=now), **settled_setpoint}
    )
    assert fresh["should_charge"] is True
    assert fresh["needs_turn_on"] is False

    settled = evaluate(now, **settled_setpoint)
    assert settled["needs_turn_on"] is True


def test_the_switch_waits_a_turn_when_the_setpoint_was_just_written(evaluate):
    """Cheap chargers drop a command that arrives on the heels of another one.
    On the first real night the setpoint and the switch went out 14 ms apart,
    so the two are now deliberately spread across separate recalculations."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        **{
            "switch.charger": State("off", last_changed=now - dt.timedelta(hours=5)),
            NUMBER: setpoint(10, now, age_seconds=18000),
        },
    )
    assert ctx["needs_write"] is True, "the setpoint is wrong and must be fixed"
    assert ctx["needs_turn_on"] is False, "so switching on waits for the next run"


def test_a_correct_setpoint_lets_the_switch_go_first_time(evaluate):
    """Queueing must not cost a recalculation when there is nothing to queue
    behind: a setpoint that is already right means the switch goes now."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        **{
            "switch.charger": State("off", last_changed=now - dt.timedelta(hours=5)),
            NUMBER: setpoint(21, now, age_seconds=18000),
        },
    )
    assert ctx["needs_write"] is False
    assert ctx["needs_turn_on"] is True


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


def test_the_mode_of_a_manual_session_is_left_alone(evaluate):
    """Not interfering with a hand-started session means the mode too.

    Someone who started the charge themselves picked the settings they wanted;
    switching the station out from under them is the same overreach as
    rewriting their current.
    """
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{
            MODE: "scheduled_charge",
            SESSION: "off",
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["foreign_session"] is True
    assert ctx["needs_mode_write"] is False


def test_the_mode_command_waits_for_the_gap_like_any_other(evaluate):
    """The mode is one more command into the same charger.

    Mid-session it has to observe the pause; only the opening command of a
    session skips it, because the station is still off and nothing precedes it.
    """
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"command_gap": 60},
        **{
            MODE: "scheduled_charge",
            "switch.charger": State("on", last_changed=now - dt.timedelta(seconds=5)),
        },
    )
    assert ctx["switch_gap_elapsed"] is False
    assert ctx["needs_mode_write"] is False, "a running session waits its turn"


def test_a_charger_that_quantises_the_setpoint_is_not_rewritten_forever(evaluate):
    """Some chargers snap the setpoint to their own grid and report it back.

    Ask for 28 A on a charger with a 0.5 A internal step and it will answer
    27.5 - not once, but every time. With a zero tolerance on the way up, the
    blueprint saw a difference, wrote the same value again, got the same answer
    back, and kept it up every ``command_gap`` seconds until morning: precisely
    the wear the throttle exists to prevent. Half a station step is below one
    step, so a genuine one-step rise still gets written.
    """
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"current_step": 1, "current_deadband": 2},
        **{SOC: 50, NUMBER: setpoint(27.5, now), SWITCH: charging_since(now)},
    )
    assert ctx["desired_current"] == 28, "the plan wants the ceiling here"
    assert ctx["current_rising"] is True
    assert ctx["want_write"] is False, "half a step of slack absorbs the quantising"
    assert ctx["needs_write"] is False


def test_a_genuine_step_upwards_is_still_written(evaluate):
    """The tolerance must not swallow the rise the deadband fix was about."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"current_step": 1, "current_deadband": 2},
        **{SOC: 50, NUMBER: setpoint(27, now), SWITCH: charging_since(now)},
    )
    assert ctx["desired_current"] == 28
    assert ctx["current_rising"] is True
    assert ctx["want_write"] is True
    assert ctx["needs_write"] is True


def test_the_setpoint_freezes_while_the_charger_status_is_unreadable(evaluate):
    """Losing the status sensor must not stop a running charge - but it must
    stop us steering it.

    Before this, the integration could drop offline and the blueprint would
    carry on writing setpoints to a charger it had no contact with, logging the
    whole thing as ordinary work. The session continues; the setpoint holds
    until the status comes back.
    """
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{SOC: 50, NUMBER: setpoint(10, now), SWITCH: charging_since(now),
           STATUS: State("unavailable")},
    )
    assert ctx["status_known"] is False
    assert ctx["should_charge"] is True, "a running session is not cut"
    assert ctx["want_write"] is True, "the plan still disagrees with the setpoint"
    assert ctx["needs_write"] is False, "but nothing is sent"
    assert "нечитаем" in ctx["verdict"]


def test_a_readable_status_lets_the_setpoint_move_again(evaluate):
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{SOC: 50, NUMBER: setpoint(10, now), SWITCH: charging_since(now)},
    )
    assert ctx["status_known"] is True
    assert ctx["needs_write"] is True
