"""The night of 2026-08-05, replayed from the traces it actually produced.

The fifth run was the first on 1.4.2, and it was the quiet one: 40 passes
against 65 the night before, 69% -> 100%, the target reached at 06:57 against
a window closing at 06:55. The 1.4.1 fixes held — ``car_arrived`` fired zero
times where it had fired sixteen, and the freshness gate held back exactly one
premature rise (01:05) instead of letting the current oscillate.

What the traces still showed:

1. **The station swallowed a write, and nothing noticed for half an hour.**
   ``set_value(13)`` went out at 03:30 and vanished: ``current_age`` climbed
   1584 -> 3384 -> 5184 seconds without ever resetting, the delivered current
   sat at 10.73 A, and — unlike every other write that night — no
   ``current_written`` echo followed. The retry only came with the 04:00 tick.

   The comment on ``current_written`` claimed that trigger caught this case.
   It cannot, and neither could the ``setpoint_stale`` trigger it replaced:
   a state trigger in Home Assistant requires the value to change, and ``for:``
   only starts counting *after* a transition. Verified against a live Home
   Assistant 2026.2.3 — rewriting the same value fires nothing, with or
   without ``not_to``. ``command_missed`` works only because ``off`` is a
   resting state a switch can lie in; "wrong current" is not.

2. **A lying tracker outlived the session and hijacked the verdict.** The
   tracker went ``not_home`` at 06:07 and stayed there while the car sat in
   the garage drawing 12.6 A. The protection worked — ``physically_present``
   kept ``car_left`` false and the session ran to its end. But once the
   charge stopped normally at 06:55 there was no current left to prove
   presence, so six consecutive passes explained themselves as "машина
   не дома" with the cable plugged in and the battery at 100%.

3. **The log could not tell a write from a retry.** "меняем ток 12.0 -> 13.0 А"
   was printed at 03:30 and again at 04:00 for a single real change.

Same hardware as the previous nights (Voyah Dream, 44 kWh, Afyeev over
tuya-local), window 23:05-06:55, efficiency 88, target 100%, starting at 69%.
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
    State,
    moment,
)

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
    "time_reserve_minutes": 15,
    "command_gap": 60,
    "soc_freeze_minutes": 90,
    "current_headroom": 10,
    "gentle_finish_soc": 95,
    "reset_current_on_stop": True,
}


def night_world(
    now: dt.datetime,
    *,
    switch: str = "on",
    setpoint: float = 12,
    setpoint_age: int = 3384,
    status: str = "charging",
    soc: float = 85,
    soc_age_min: float = 13,
    power: float = 2368,
    delivered: float = 10.728,
    tracker: str = "home",
    flag: str = "on",
) -> dict[str, State]:
    old = now - dt.timedelta(hours=2)
    return {
        SWITCH: State(switch, last_changed=old),
        NUMBER: State(
            setpoint,
            {"min": 6.0, "max": 28.0, "step": 1.0},
            now - dt.timedelta(seconds=setpoint_age),
        ),
        STATUS: State(status, last_changed=old),
        POWER: State(power, last_changed=old),
        AMPERE: State(delivered, last_changed=old),
        VOLTAGE: State(221.0, last_changed=old),
        LINK: State("on", last_changed=old),
        PROBLEM: State("off", last_changed=old),
        MODE: State("immediate", last_changed=old),
        SOC: State(soc, last_changed=now - dt.timedelta(minutes=soc_age_min)),
        SOH: State(102, last_changed=old),
        TRACKER: State(tracker, last_changed=old),
        SESSION: State(flag, last_changed=old),
        "zone.home": State(1, {"friendly_name": "Home"}, now),
    }


@pytest.fixture
def replay(blueprint):
    def _replay(now: dt.datetime, world: dict[str, State], **inputs):
        merged = dict(NIGHT_INPUTS)
        merged.update(inputs)
        return blueprint.evaluate(world=world, now=now, inputs=merged)

    return _replay


@pytest.fixture
def replay_actions(blueprint):
    def _run(now: dt.datetime, world: dict[str, State], **inputs):
        merged = dict(NIGHT_INPUTS)
        merged.update(inputs)
        return blueprint.run_actions(world=world, now=now, inputs=merged)

    return _run


# ------------------------------------------------- the swallowed write


def test_a_swallowed_write_is_repeated_on_the_next_recalculation(replay_actions):
    """04:00. The 03:30 write never reached the station.

    The setpoint still reads 12 and has not moved for 5184 seconds, so the
    plan still asks for 13 — and the command goes out again. This is the only
    thing that saved the night, and it is worth a test precisely because the
    trigger that was supposed to catch it sooner cannot.
    """
    now = moment(4, 0, day=6, month=8)
    world = night_world(now, setpoint=12, setpoint_age=5184, soc=87, soc_age_min=13)
    calls = replay_actions(now, world)
    writes = [c for c in calls if c.get("action") == "number.set_value"]
    assert writes, "a setpoint that never took must be written again"
    assert writes[0]["data"]["value"] == 13


def test_the_setpoint_trigger_does_not_claim_to_catch_a_swallowed_write(blueprint):
    """Home Assistant cannot express "this failed to change" for a value.

    ``current_written`` exists for the command queue and nothing else. The
    claim that it also caught swallowed writes survived two versions and cost
    half an hour of regulation on the fifth night; the trigger stays, the
    claim does not.
    """
    triggers = {t.get("id"): t for t in blueprint.triggers}
    assert "setpoint_stale" not in triggers, "it could not catch it either"
    written = triggers["current_written"]
    assert written["not_to"] == ["unknown", "unavailable"]


def test_the_switch_retry_still_guards_the_turn_on(blueprint):
    """``off`` *is* a resting state, so the same trick works for the switch.

    This is the asymmetry the fifth night made explicit, and the reason
    ``command_missed`` is kept while its setpoint twin was dropped.
    """
    missed = {t.get("id"): t for t in blueprint.triggers}["command_missed"]
    assert missed["to"] == "off"
    assert "not_from" not in missed


# ------------------------------------------------- the tracker that lied


def test_a_lying_tracker_does_not_stop_a_running_session(replay):
    """06:30. ``not_home`` since 06:07, and the car is drawing 12.6 A.

    The night's own proof that trusting the hardware over the satellites is
    right: the session ran to the end of the window and reached 100%.
    """
    now = moment(6, 30, day=6, month=8)
    world = night_world(
        now, tracker="not_home", setpoint=14, soc=99, soc_age_min=11,
        power=2752, delivered=12.632,
    )
    ctx = replay(now, world)
    assert ctx["car_left"] is False
    assert ctx["should_charge"] is True


def test_a_lying_tracker_does_not_explain_a_finished_session(replay):
    """06:57. Window closed, charge stopped, battery full, cable still in.

    ``car_left`` goes true here for real — the current that vouched for the
    car is gone. It just is not the reason for standing still, and saying so
    six times running made the log read as though the car had driven off
    mid-charge.
    """
    now = moment(6, 57, day=6, month=8)
    world = night_world(
        now, switch="off", tracker="not_home", status="charged",
        setpoint=14, soc=100, soc_age_min=0.3, power=0, delivered=0, flag="off",
    )
    ctx = replay(now, world)
    assert ctx["car_left"] is True, "with no current there is nothing to vouch"
    assert ctx["verdict"] != "машина не дома"


def test_a_car_that_really_left_still_blocks_a_start(replay):
    """The other half of the rule: inside the window, with nothing charging,
    an absent car is exactly why no session begins — and the log must say so.
    """
    now = moment(1, 0, day=6, month=8)
    world = night_world(
        now, switch="off", tracker="not_home", status="plugged_in",
        soc=70, power=0, delivered=0, flag="off",
    )
    ctx = replay(now, world)
    assert ctx["car_left"] is True
    assert ctx["verdict"] == "машина не дома"


def test_a_full_battery_outranks_a_wandering_tracker(replay):
    """Same moment, but asking what the verdict became instead of what it is
    not: the target is the honest answer once the charge is done.
    """
    now = moment(6, 57, day=6, month=8)
    world = night_world(
        now, switch="off", tracker="not_home", status="charged",
        setpoint=6, soc=100, soc_age_min=0.5, power=0, delivered=0, flag="off",
    )
    ctx = replay(now, world)
    assert ctx["verdict"] == "цель достигнута"


# ------------------------------------------------- the log


def test_the_write_verdict_does_not_promise_the_station_obeyed(replay):
    """03:30. The command is going out; whether it lands is not knowable here.

    The old wording ("меняем ток") printed twice for one real change.
    """
    now = moment(3, 30, day=6, month=8)
    world = night_world(now, setpoint=12, setpoint_age=3384, soc=85)
    ctx = replay(now, world)
    assert ctx["needs_write"] is True
    assert ctx["verdict"].startswith("пишем ток")


# ------------------------------------------------- what went right


def test_the_freshness_gate_held_one_premature_rise(replay):
    """01:05. The plan asks 12 A, but the percentage is 8.8 minutes old and
    older than our own setpoint — the rise waits for the car to report.
    """
    now = moment(1, 5, day=6, month=8)
    world = night_world(
        now, setpoint=11, setpoint_age=60, soc=76, soc_age_min=8.8,
        power=2169, delivered=9.905,
    )
    ctx = replay(now, world)
    assert ctx["soc_fresh_enough_to_rise"] is False
    assert ctx["needs_write"] is False


def test_the_gentle_finish_capped_the_last_hour(replay):
    """06:30, 99%. The plan wanted 15 A; the battery would not have taken it.

    Measured cost of the cap that night: 0.12 points of charge.
    """
    now = moment(6, 30, day=6, month=8)
    world = night_world(
        now, setpoint=14, soc=99, soc_age_min=11, power=2752, delivered=12.632,
    )
    ctx = replay(now, world)
    assert ctx["gentle_finish_active"] is True
    assert ctx["desired_current"] <= 14


def test_a_brief_dropout_never_interrupted_the_charge(replay):
    """One of the five dropouts, 02:33. Everything of the station's goes
    unavailable at once while 2.36 kW keeps flowing.
    """
    now = moment(2, 33, day=6, month=8)
    world = night_world(now, soc=82, soc_age_min=1.6)
    world[LINK] = State("off", last_changed=now - dt.timedelta(seconds=2))
    ctx = replay(now, world)
    assert ctx["charger_online"] is False
    assert ctx["verdict"] == "станция офлайн"
    assert ctx["must_stop"] is False
