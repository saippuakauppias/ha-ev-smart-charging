"""The night of 2026-08-02, replayed from the traces it actually produced.

The second run on real hardware charged the car to 100%, but did it wrong: for
seven of the eight hours the automation believed it was watching somebody
else's charge. It never adjusted the current, never stopped at the end of the
window, and only finished because the station itself reported "charged" at
07:18 — twenty-three minutes past the configured 06:55.

The cause was a two-second dropout. The station's integration drops every
entity at once and brings them back in four separate ticks:

===========  ============================================================
T+0.0 s      everything unavailable: switch, status, setpoint, link
T+0.6 s      status returns as ``charging``
T+1.5 s      the switch returns as ``on``
T+1.9 s      the link sensor returns as ``on``
===========  ============================================================

At T+0.0 the switch was not ``on``, so the housekeeping branch read that as
"somebody turned the charging off by hand" and cleared the session flag. At
T+1.5 the switch was back on with the flag down, which is the definition of a
foreign session — and there was no way back, because the flag could only be
raised by the branch that starts charging, and that branch requires the switch
to be off.

Same hardware as the first night (Voyah Dream, 44 kWh, SoH 102%, Afyeev 32 A
over tuya-local), window 23:05-06:55, target 100%, starting at 53%.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    AMPERE,
    LINK,
    MODE,
    NUMBER,
    POWER,
    PROBLEM,
    SESSION,
    SOC,
    SOH,
    STATUS,
    SWITCH,
    TRACKER,
    VOLTAGE,
)
from ha_sim import State, moment

#: Exactly what the user had configured that night.
NIGHT_INPUTS = {
    "charger_switch": SWITCH,
    "charger_current_number": NUMBER,
    "charger_status_sensor": STATUS,
    "charger_power_sensor": POWER,
    "charger_current_sensor": AMPERE,
    "charger_voltage_sensor": VOLTAGE,
    "charger_link_sensor": LINK,
    "charger_problem_sensor": PROBLEM,
    "charger_mode_select": MODE,
    "car_battery_sensor": SOC,
    "car_tracker": TRACKER,
    "soh_sensor": SOH,
    "session_flag": SESSION,
    "home_zone": "zone.home",
    "battery_capacity": 44,
    "nominal_voltage": 210,
    "start_time": "23:05:00",
    "stop_time": "06:55:00",
    "target_soc": 100,
    "min_current": 6,
    "max_current": 28,
    "current_step": 1,
    "phases": "1",
    "efficiency": 88,
    # The traces replayed here were recorded before either knob existed, so both
    # stay off: this file must keep reproducing the night as it actually ran.
    "current_headroom": 0,
    "gentle_finish_soc": 100,
    "time_reserve_minutes": 15,
    "command_gap": 60,
}

#: The four ticks of the 00:21 dropout, copied from the traces. ``switch`` and
#: ``status`` are the two that matter; the rest come back with them.
DROPOUT = [
    ("T+0.0", {"switch": "unavailable", "status": "unknown",
               "setpoint": "unknown", "link": "off", "problem": "on"}),
    ("T+0.6", {"switch": "unavailable", "status": "charging",
               "setpoint": 16, "link": "off", "problem": "on"}),
    ("T+1.5", {"switch": "on", "status": "charging",
               "setpoint": 16, "link": "off", "problem": "off"}),
    ("T+1.9", {"switch": "on", "status": "charging",
               "setpoint": 16, "link": "on", "problem": "off"}),
]


def dropout_world(tick: dict, *, flag: str = "on") -> tuple[dt.datetime, dict[str, State]]:
    """The world during one tick of the dropout, at 00:21 on the second night.

    Ages are the ones the traces recorded: the dropout resets ``last_changed``
    on every entity it touches, which is itself part of the problem.
    """
    now = moment(0, 21, day=3, month=8)
    fresh = now - dt.timedelta(seconds=1)
    return now, {
        SWITCH: State(tick["switch"], last_changed=fresh),
        NUMBER: State(tick["setpoint"], {"min": 6.0, "max": 32.0, "step": 1.0}, fresh),
        STATUS: State(tick["status"], last_changed=fresh),
        # The current never stopped flowing: 14.7 A throughout, which is the
        # physical proof that nobody switched anything off.
        POWER: State(3178, last_changed=fresh),
        AMPERE: State(14.715, last_changed=fresh),
        VOLTAGE: State(216.0, last_changed=fresh),
        LINK: State(tick["link"], last_changed=fresh),
        PROBLEM: State(tick["problem"], last_changed=fresh),
        MODE: State("immediate", last_changed=now - dt.timedelta(hours=1)),
        SOC: State(60, last_changed=now - dt.timedelta(minutes=9)),
        SOH: State(102, last_changed=now - dt.timedelta(hours=6)),
        TRACKER: State("home", last_changed=now - dt.timedelta(hours=2)),
        SESSION: State(flag, last_changed=now - dt.timedelta(hours=1)),
        "zone.home": State(1, {"friendly_name": "Home"}, now),
    }


@pytest.fixture
def during_dropout(blueprint):
    def _run(tick: dict, *, flag: str = "on"):
        now, world = dropout_world(tick, flag=flag)
        return blueprint.evaluate(world=world, now=now, inputs=dict(NIGHT_INPUTS))

    return _run


@pytest.fixture
def replay(blueprint):
    """Evaluate a world built by hand, for the cases that need to bend one
    reading away from what the traces recorded."""
    def _replay(now: dt.datetime, world: dict[str, State], **inputs):
        merged = dict(NIGHT_INPUTS)
        merged.update(inputs)
        return blueprint.evaluate(world=world, now=now, inputs=merged)

    return _replay


TICKS = dict(DROPOUT)


# ------------------------------------------------- the flag must survive


@pytest.mark.parametrize("label", [label for label, _ in DROPOUT])
def test_no_tick_of_the_dropout_clears_the_session_flag(during_dropout, label):
    """The bug in one assertion.

    At T+0.0 the old ``session_flag_stuck`` was true — switch not ``on``, flag
    still up — and the housekeeping branch cleared the flag. Every later tick
    then read the session as a stranger's. Nothing about a dropout may clear
    it: the charger is offline, so there is nothing to tidy up after.
    """
    ctx = during_dropout(TICKS[label])
    assert ctx["session_flag_stuck"] is False


def test_an_unavailable_switch_is_not_a_switch_that_was_turned_off(during_dropout):
    """The root confusion, isolated.

    ``is_state(switch, 'on')`` is false both when the switch is off and when
    the entity is missing, and the blueprint used to act on the difference it
    could not see.
    """
    ctx = during_dropout(TICKS["T+0.0"])
    assert ctx["switch_on"] is False, "it is genuinely not reporting 'on'"
    assert ctx["switch_off_confirmed"] is False, "but it is not off either"


def test_a_confirmed_manual_stop_still_clears_the_flag(replay):
    """The guard must not cost us the cleanup it was guarding.

    A switch that really is ``off``, on a charger that is really online, with
    the flag still raised, is the case the housekeeping exists for.
    """
    now, world = dropout_world(TICKS["T+1.9"])
    world[SWITCH] = State("off", last_changed=now - dt.timedelta(hours=1))
    world[POWER] = State(0, last_changed=now)
    world[AMPERE] = State(0, last_changed=now)
    ctx = replay(now, world)
    assert ctx["switch_off_confirmed"] is True
    assert ctx["session_flag_stuck"] is True


# ------------------------------------------------- and if it is lost anyway


def test_a_flag_lost_mid_charge_is_taken_back(blueprint, during_dropout):
    """What had no way out before.

    With the flag down and the switch on, the automation used to be stuck as a
    bystander until the station switched itself off. The setpoint gives it
    away: 16 A standing against a 15 A plan is inside the deadband, so this is
    a session we set up ourselves and then lost track of.
    """
    ctx = during_dropout(TICKS["T+1.9"], flag="off")
    assert ctx["foreign_session"] is True, "this is what it looks like at first"
    assert ctx["want_write"] is False, "the setpoint is one we would have written"
    assert ctx["session_lost"] is True, "so we recognise it as ours and reclaim it"

    # Deciding is not enough — the flag has to actually go back up, or the
    # automation stays a bystander no matter what it concluded.
    now, world = dropout_world(TICKS["T+1.9"], flag="off")
    calls = blueprint.run_actions(world=world, now=now, inputs=dict(NIGHT_INPUTS))
    assert [c["action"] for c in calls] == ["input_boolean.turn_on"]
    assert calls[0]["entity_id"] == SESSION


def test_a_genuinely_manual_charge_is_never_captured(replay):
    """The other side of the same rule, and the more important one.

    Somebody who flips the switch by hand does it at a moment of their own
    choosing — including in the middle of the night, inside the window — and
    picks their own amperage. A setpoint that disagrees with the plan is the
    signature of a human, and capturing that session would defeat the entire
    point of the flag.
    """
    now, world = dropout_world(TICKS["T+1.9"], flag="off")
    world[NUMBER] = State(10, {"min": 6.0, "max": 32.0, "step": 1.0},
                          now - dt.timedelta(hours=2))
    ctx = replay(now, world)
    assert ctx["foreign_session"] is True
    assert ctx["want_write"] is True, "10 A is not what the plan asks for"
    assert ctx["session_lost"] is False, "so the current is somebody's own choice"


# ------------------------------------------------- the false fault


def test_the_problem_sensor_is_not_believed_while_the_charger_is_offline(during_dropout):
    """At T+0.0 the problem sensor read ``on`` and the automation raised a
    fault — three times that night, for a station that was merely absent.

    A real fault arrives on its own, with the link up and the status readable.
    """
    ctx = during_dropout(TICKS["T+0.0"])
    assert ctx["charger_online"] is False
    assert ctx["charger_fault"] is False, "an absent station is not a broken one"
    assert ctx["alarm_reason"] == "none", "and must not wake anybody up"


def test_a_real_fault_is_still_reported(replay):
    """The guard must not swallow the signal it was filtering."""
    now, world = dropout_world(TICKS["T+1.9"])
    world[PROBLEM] = State("on", last_changed=now)
    ctx = replay(now, world)
    assert ctx["charger_online"] is True
    assert ctx["charger_fault"] is True
    assert ctx["alarm_reason"] == "charger_fault"


# ------------------------------------------------- the window that never ended


def test_the_window_end_stops_the_session_once_the_flag_is_back(blueprint):
    """07:00 that night: past the window, still charging, ``must_stop`` false.

    Ending the window really is a courtesy owed to a hand-started session, so
    ``foreign_session`` still suppresses it — that part was never the bug. The
    bug was having no way to notice the session was ours all along.

    Recovery happens hours before this point (see the dropout cases above: the
    setpoint still matched the plan at 00:22, so the flag would have come back
    on the very next pass). This pins down what follows once it has.
    """
    now = moment(7, 0, day=3, month=8)
    world = {
        SWITCH: State("on", last_changed=now - dt.timedelta(hours=8)),
        NUMBER: State(16, {"min": 6.0, "max": 32.0, "step": 1.0},
                      now - dt.timedelta(hours=4)),
        STATUS: State("charging", last_changed=now - dt.timedelta(hours=4)),
        POWER: State(3204, last_changed=now),
        AMPERE: State(14.683, last_changed=now),
        VOLTAGE: State(218.4, last_changed=now),
        LINK: State("on", last_changed=now - dt.timedelta(hours=4)),
        PROBLEM: State("off", last_changed=now - dt.timedelta(hours=4)),
        MODE: State("immediate", last_changed=now - dt.timedelta(hours=8)),
        # 99% at 07:00 is what the traces show, with the target at 100.
        SOC: State(99, last_changed=now - dt.timedelta(minutes=1)),
        SOH: State(102, last_changed=now - dt.timedelta(hours=6)),
        TRACKER: State("home", last_changed=now - dt.timedelta(hours=8)),
        SESSION: State("off", last_changed=now - dt.timedelta(hours=7)),
        "zone.home": State(1, {"friendly_name": "Home"}, now),
    }
    inputs = dict(NIGHT_INPUTS)
    inputs["stop_at_window_end"] = True
    ctx = blueprint.evaluate(world=world, now=now, inputs=inputs)
    assert ctx["in_window"] is False
    assert ctx["foreign_session"] is True
    assert ctx["must_stop"] is False, "the courtesy itself is intended behaviour"

    world[SESSION] = State("on", last_changed=now - dt.timedelta(hours=6))
    ctx = blueprint.evaluate(world=world, now=now, inputs=inputs)
    assert ctx["foreign_session"] is False
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"


# ------------------------------------------------- the current that never arrived


def test_the_shortfall_between_setpoint_and_reality_is_measured(replay):
    """Both real nights ran about 1.3 A below whatever was asked for: 16 A gave
    14.7, 19 A gave 17.7. That is the station's own offset, not cable loss and
    not the car's limit, and it costs roughly eight percent of the plan.

    It is reported rather than corrected here. Compensation exists, but as a
    multiplier applied before rounding (see ``current_headroom``); adding the
    measured gap back to the calculated current would instead feed straight into
    the deadband comparison and rewrite the setpoint on every single pass — the
    exact confusion ``test_the_deadband_compares_setpoint_with_setpoint``
    exists to prevent.

    Measured only once the setpoint has settled: see the companion test below.
    """
    now, world = dropout_world(TICKS["T+1.9"])
    settled = now - dt.timedelta(minutes=30)
    world[NUMBER] = State(16, {"min": 6.0, "max": 32.0, "step": 1.0}, settled)
    world[AMPERE] = State(14.715, last_changed=settled)
    ctx = replay(now, world)
    assert ctx["current_now"] == 16
    assert ctx["actual_current"] == 14.715
    assert ctx["current_shortfall"] == 1.29
    assert ctx["desired_current"] == 15, "the plan itself stays untouched"


def test_no_shortfall_is_claimed_while_the_setpoint_is_still_fresh(during_dropout):
    """The station's current sensor lags behind its own setpoint.

    On the third night it sat at 11.669 A for seven minutes while the power
    reading climbed 2592 -> 5683 W, so the real current was closer to 25 A. Read
    naively, that gap looks like a 16 A shortfall — every one of those bogus
    readings landed on a setpoint exactly ``command_gap`` old, while every
    trustworthy one sat on a setpoint thousands of seconds old.
    """
    ctx = during_dropout(TICKS["T+1.9"])
    assert ctx["current_now"] == 16
    assert ctx["actual_current"] == 14.715
    assert ctx["gap_elapsed"] is False, "the setpoint came back one second ago"
    assert ctx["current_shortfall"] == 0


def test_no_shortfall_is_claimed_when_nothing_is_flowing(during_dropout):
    """A station that is off, or absent, is not a station that underdelivers."""
    ctx = during_dropout(TICKS["T+0.0"])
    assert ctx["current_shortfall"] == 0


# ------------------------------------------------- restarts, same failure mode


def test_a_restart_mid_session_does_not_hand_the_session_away(replay):
    """The same bug reachable without any dropout at all.

    Restoring an ``input_boolean`` takes Home Assistant through ``unknown``,
    and the ``ha_start`` trigger guarantees a pass at exactly that moment. Read
    as "flag is not on", that made every restart mid-charge disown the session.
    An unreadable flag means unknown ownership, which is not the same as
    somebody else's.
    """
    now, world = dropout_world(TICKS["T+1.9"])
    world[SESSION] = State("unknown", last_changed=now)
    ctx = replay(now, world)
    assert ctx["session_flag_readable"] is False
    # ``session_owned`` is the assertion that carries the guard. Checking only
    # ``foreign_session`` would pass either way: it repeats the readability
    # test itself, and so hides whether ownership was decided correctly.
    assert ctx["session_owned"] is True, "unreadable means unknown, not somebody else's"
    assert ctx["foreign_session"] is False, "unknown ownership is not a stranger's"
    assert ctx["session_flag_stuck"] is False, "and nothing to tidy up either"


def test_a_dropout_does_not_trigger_the_end_of_session_housekeeping(blueprint):
    """The other two commands that read a missing switch as a stopped one.

    After a session ends, housekeeping lowers the setpoint back to the minimum
    (so the next hand-started charge does not begin at last night's current)
    and clears the session flag. Both are correct at the end of a session and
    both are damaging during a blink: the car would be left at 6 A and the
    session disowned.

    The switch is deliberately given an old timestamp here — the command gap
    has long since elapsed, so nothing but the confirmation check stands
    between a two-second blink and both commands going out.
    """
    now = moment(8, 0, day=3, month=8)
    _, world = dropout_world(TICKS["T+1.9"])
    world[NUMBER] = State(16, {"min": 6.0, "max": 32.0, "step": 1.0},
                          now - dt.timedelta(hours=3))
    world[SOC] = State(60, last_changed=now - dt.timedelta(minutes=5))
    inputs = dict(NIGHT_INPUTS)
    inputs["reset_current_on_stop"] = True

    world[SWITCH] = State("unavailable", last_changed=now - dt.timedelta(hours=3))
    assert blueprint.run_actions(world=world, now=now, inputs=inputs) == [], (
        "an absent switch is not a switch that was turned off"
    )

    # The same world with the switch genuinely off: housekeeping must still run,
    # or the guard above would have bought safety by breaking the feature.
    world[SWITCH] = State("off", last_changed=now - dt.timedelta(hours=3))
    calls = blueprint.run_actions(world=world, now=now, inputs=inputs)
    assert [c["action"] for c in calls] == ["input_boolean.turn_off", "number.set_value"]
    assert calls[1]["data"]["value"] == 6


def test_nothing_destructive_happens_while_the_station_is_absent(blueprint):
    """The whole guard, end to end: during a dropout the automation is silent.

    Every command it could send would go to an integration that is not there,
    and the one it used to send — clearing the session flag — was the command
    that cost the night.
    """
    now, world = dropout_world(TICKS["T+0.0"])
    calls = blueprint.run_actions(world=world, now=now, inputs=dict(NIGHT_INPUTS))
    assert calls == []
