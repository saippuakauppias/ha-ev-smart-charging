"""The start/stop decision matrix and the reason reported for stopping."""

from __future__ import annotations

import pytest
from conftest import (
    AMPERE,
    LINK,
    NUMBER,
    POWER,
    PROBLEM,
    SESSION,
    SOC,
    STATUS,
    TRACKER,
    build_world,
    charging_since,
    setpoint,
)
from ha_sim import State, moment


def test_the_happy_path_charges(evaluate):
    ctx = evaluate(moment(23, 0))
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False


@pytest.mark.parametrize("status", ["available", "fault_unplugged"])
def test_an_unplugged_cable_prevents_charging(evaluate, status):
    ctx = evaluate(moment(23, 0), **{STATUS: status})
    assert ctx["plugged_in"] is False
    assert ctx["should_charge"] is False


def test_an_unreadable_status_does_not_start_a_session(evaluate):
    """Starting blind is worse than waiting.

    Without a readable status there is no way to tell a plugged-in cable from
    an empty socket, and turning the charger on would also make the watchdog
    report a phantom "switched on but no current".
    """
    ctx = evaluate(moment(23, 0), **{STATUS: State("unknown")})
    assert ctx["status_known"] is False
    assert ctx["plugged_in"] is False
    assert ctx["should_charge"] is False


def test_an_unreadable_status_does_not_interrupt_a_running_session(evaluate):
    """A sensor dropping out mid-charge is not a reason to cut the power."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{STATUS: State("unavailable"), "switch.charger": charging_since(now)},
    )
    assert ctx["status_known"] is False
    assert ctx["plugged_in"] is True
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False


def test_an_unplugged_cable_does_not_trigger_a_stop_command(evaluate):
    """There is nothing to stop; sending switch.turn_off would be noise."""
    ctx = evaluate(moment(23, 0), **{STATUS: "available"})
    assert ctx["must_stop"] is False


def test_charger_reporting_completion_stops_the_session(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{STATUS: "charged", "switch.charger": charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "charger_reports_charged"


def test_a_charger_fault_stops_the_session(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{STATUS: "fault", "switch.charger": charging_since(now)})
    assert ctx["charger_fault"] is True
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "fault"


def test_the_problem_sensor_also_counts_as_a_fault(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{PROBLEM: "on", "switch.charger": charging_since(now)})
    assert ctx["charger_fault"] is True
    assert ctx["must_stop"] is True


def test_an_offline_charger_is_left_alone(evaluate):
    """No point issuing commands into the void, and no point flapping the switch."""
    now = moment(1, 0)
    ctx = evaluate(now, **{LINK: "off", "switch.charger": charging_since(now)})
    assert ctx["charger_online"] is False
    assert ctx["should_charge"] is False
    assert ctx["must_stop"] is False


def test_reaching_the_target_stops_the_session(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{SOC: 100, "switch.charger": charging_since(now)})
    assert ctx["target_reached"] is True
    assert ctx["stop_reason"] == "target_reached"
    assert ctx["must_stop"] is True


def test_a_lower_target_stops_earlier(evaluate):
    now = moment(3, 0)
    ctx = evaluate(
        now, inputs={"target_soc": 80}, **{SOC: 81, "switch.charger": charging_since(now)}
    )
    assert ctx["target_reached"] is True
    assert ctx["must_stop"] is True, "and the name of this test is about stopping"
    assert ctx["stop_reason"] == "target_reached"


def test_end_of_window_stops_by_default(evaluate):
    now = moment(9, 0)
    ctx = evaluate(now, **{"switch.charger": charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"
    assert ctx["target_missed"] is True


def test_end_of_window_can_be_allowed_to_overrun(evaluate):
    now = moment(9, 0)
    # Running since 04:00, i.e. from inside the window: this is a session to be
    # finished, not one that started outside it.
    ctx = evaluate(
        now,
        inputs={"stop_at_window_end": False},
        **{"switch.charger": charging_since(now, minutes=5 * 60)},
    )
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False


def test_overrun_never_starts_a_new_session_outside_the_window(evaluate):
    ctx = evaluate(moment(9, 0), inputs={"stop_at_window_end": False})
    assert ctx["should_charge"] is False


def test_a_manual_session_survives_the_end_of_the_window(evaluate):
    """With the session flag wired up, only our own sessions get cut off."""
    now = moment(9, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "off", "switch.charger": charging_since(now)},
    )
    assert ctx["session_owned"] is False
    assert ctx["must_stop"] is False


def test_our_own_session_is_cut_off_at_the_end_of_the_window(evaluate):
    now = moment(9, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "on", "switch.charger": charging_since(now)},
    )
    assert ctx["session_owned"] is True
    assert ctx["must_stop"] is True


def test_a_hand_started_session_keeps_its_current_and_its_window(evaluate):
    """Charging switched on by hand is none of the automation's business.

    Someone who flipped the switch themselves chose the current they wanted, and
    chose to keep charging: neither the setpoint nor the end of the window is
    ours to override. That courtesy is what the session flag exists for.
    """
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "off", SOC: 40, "switch.charger": charging_since(now)},
    )
    assert ctx["foreign_session"] is True
    assert ctx["must_stop"] is False
    assert ctx["needs_write"] is False


def test_the_window_ending_does_not_stop_a_hand_started_session(evaluate):
    """The courtesy that matters most: the window is our schedule, not theirs."""
    now = moment(8, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION, "stop_at_window_end": True},
        **{SESSION: "off", SOC: 40, "switch.charger": charging_since(now)},
    )
    assert ctx["in_window"] is False
    assert ctx["foreign_session"] is True
    assert ctx["must_stop"] is False


@pytest.mark.parametrize(
    "label,overrides,reason",
    [
        ("the target was reached", {SOC: 100}, "target_reached"),
        ("the charger says it is done", {STATUS: "charged"}, "charger_reports_charged"),
        ("the cable was pulled", {STATUS: "available"}, "unplugged"),
        ("the charger faulted", {STATUS: "fault"}, "fault"),
    ],
)
def test_reasons_unrelated_to_the_owners_choice_stop_a_hand_started_session(
    evaluate, label, overrides, reason
):
    """Courtesy covers the current and the schedule — not everything.

    A driver who starts charging by hand chose an amperage and a duration. They
    did not choose for the car to leave, the cable to be pulled, the battery to
    fill, or the station to fault, and leaving the switch on through any of that
    serves nobody.

    Making all of these hang on the session flag was the flaw behind the second
    real night: one false ``foreign_session`` disabled every stop at once, and
    charging ran on past the window because a two-second dropout had cleared the
    flag. Stops that have nothing to do with the owner's choice now survive it.
    """
    now = moment(3, 0)
    world = dict(overrides)
    world[SESSION] = "off"
    world["switch.charger"] = charging_since(now)
    ctx = evaluate(now, inputs={"session_flag": SESSION}, world=build_world(now, **world))
    assert ctx["foreign_session"] is True, label
    assert ctx["must_stop"] is True, label
    assert ctx["stop_reason"] == reason, label


def test_a_car_that_drove_away_stops_a_hand_started_session(evaluate):
    """Same rule, stated separately because "gone" needs the power to be gone too.

    A tracker saying ``not_home`` while current is still flowing means the
    tracker is lying, not that the car left — that was the first night's bug.
    Here nothing is drawing power, so the car really is elsewhere, and whoever
    started the session by hand would not want the switch left on for it.
    """
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        world=build_world(
            now,
            **{
                SESSION: "off",
                SOC: 40,
                TRACKER: "not_home",
                POWER: 0,
                "switch.charger": charging_since(now),
            },
        ),
    )
    assert ctx["foreign_session"] is True
    assert ctx["physically_present"] is False, "no current means the car is really gone"
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "car_not_home"


def test_the_setpoint_of_a_hand_started_session_is_never_rewritten(evaluate):
    """The whole point: the manually chosen current survives the night."""
    now = moment(23, 30)
    world = build_world(now, **{SESSION: "off", SOC: 40})
    world[NUMBER] = setpoint(10, now)
    world["switch.charger"] = charging_since(now, 180)
    ctx = evaluate(now, inputs={"session_flag": SESSION}, world=world)
    assert ctx["want_write"] is True, "the plan does disagree with 10 A"
    assert ctx["needs_write"] is False, "but we must not act on that"


def test_a_cable_plugged_in_early_is_not_a_foreign_session(evaluate):
    """Plugging in during the day and leaving the charger off is the normal way
    to queue a car for the night. That must start on schedule as usual."""
    ctx = evaluate(
        moment(23, 30),
        inputs={"session_flag": SESSION},
        **{SESSION: "off", SOC: 40, STATUS: "plugged_in"},
    )
    assert ctx["foreign_session"] is False
    assert ctx["should_charge"] is True
    assert ctx["needs_write"] is True


def test_emergency_charging_ignores_the_window(evaluate):
    ctx = evaluate(moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12})
    assert ctx["in_window"] is False
    assert ctx["should_charge"] is True


def test_emergency_charging_still_requires_the_car_to_be_home(evaluate):
    ctx = evaluate(
        moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12, TRACKER: "not_home"}
    )
    assert ctx["should_charge"] is False


def test_emergency_charging_still_requires_a_plugged_cable(evaluate):
    ctx = evaluate(
        moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12, STATUS: "available"}
    )
    assert ctx["should_charge"] is False


def test_emergency_is_off_by_default(evaluate):
    ctx = evaluate(moment(14, 0), **{SOC: 5})
    assert ctx["emergency"] is False
    assert ctx["should_charge"] is False


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({STATUS: "fault"}, "fault"),
        ({SOC: 100}, "target_reached"),
        ({STATUS: "charged"}, "charger_reports_charged"),
        # A real departure takes the cable with it, so the current stops too.
        ({TRACKER: "not_home", STATUS: "available", POWER: 0, AMPERE: 0},
         "car_not_home"),
        ({STATUS: "available"}, "unplugged"),
    ],
)
def test_stop_reasons_are_reported_in_priority_order(evaluate, overrides, expected):
    now = moment(3, 0)
    overrides = dict(overrides)
    overrides["switch.charger"] = charging_since(now)
    ctx = evaluate(now, **overrides)
    assert ctx["stop_reason"] == expected


def test_an_unplugged_cable_stops_a_running_session(evaluate):
    """The charger reporting an empty socket while the switch is still on means
    the cable was pulled; leaving the switch on would arm the next plug-in."""
    now = moment(3, 0)
    ctx = evaluate(now, **{STATUS: "available", "switch.charger": charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "unplugged"


# ------------------------------------------------------------------ invariant


@pytest.mark.parametrize("hour", [23, 3, 6, 9, 14])
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {STATUS: "fault"},
        {STATUS: "available"},
        {STATUS: "charged"},
        {STATUS: State("unavailable")},
        {SOC: 100},
        {SOC: State("unavailable")},
        {TRACKER: "not_home"},
        {TRACKER: State("unavailable")},
        {LINK: "off"},
        {PROBLEM: "on"},
    ],
)
@pytest.mark.parametrize("running", [True, False])
def test_charging_and_stopping_are_never_demanded_at_once(
    evaluate, hour, overrides, running
):
    """Both branches of the action block are guarded by these two flags. If they
    could ever hold together, the charger would be switched on and off in the
    same run."""
    now = moment(hour, 0)
    world = dict(overrides)
    if running:
        world["switch.charger"] = charging_since(now)
    ctx = evaluate(now, **world)
    assert not (ctx["should_charge"] and ctx["must_stop"])
