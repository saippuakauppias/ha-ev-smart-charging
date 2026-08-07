"""The night of 2026-08-06, replayed from the traces it actually produced.

The sixth run was the first on 1.4.3 and the calmest so far: 37 passes,
67% -> 99% by the end of the window and 100% shortly after, one setpoint change
all night (13 A held from 23:30 to 05:00, then 14 A). The 1.4.3 fixes held —
the "машина не дома" verdict no longer trailed the finish, and the log said
"пишем ток" where it used to promise a change it could not confirm.

The user had moved ``start_time`` from 23:05 to 23:30 that evening. That one
setting is what surfaced both defects below, and neither is specific to it.

1. **Two runs at 23:30:00.000 sent the same command twice.** ``window_start``
   fired at 20:30:00.091 UTC and the recalculation ``tick`` at 20:30:00.295 —
   204 ms apart, because a round start time lands on the half-hour grid. Mode
   ``restart`` did not help: the first run reached ``number.set_value`` before
   the second one displaced it, so the station got two identical writes a fifth
   of a second apart. That is precisely the interval ``command_gap`` exists to
   prevent, and precisely what cheap stations drop.

   The throttle could not see it. It measures age from the setpoint entity's
   ``last_changed``, and 204 ms in the entity had not moved yet: the second run
   read ``current_age`` = 230 s, the same as the first, and considered the gap
   served. ``run_age`` closes that blind spot by asking the automation itself
   when it last ran.

2. **The blueprint called its own session a stranger's, mid-stop.** Branch 2
   switches the charger off and *then* lowers the session flag. The flag is a
   local ``input_boolean`` and drops instantly; the station confirmed ``off``
   43 seconds later. In between, ``switch_on`` was still true and the flag was
   already down — the exact signature of a foreign session. The 06:55:28 pass
   fell into that gap and reported ``foreign_session`` true while stopping.

   It got away with it: the stop ran on ``charger_reports_charged``, which is
   in ``stop_regardless_of_owner``. Had it been a plain end-of-window stop, the
   repeat would have been suppressed — ``must_stop`` requires
   ``not foreign_session`` — and a swallowed ``turn_off`` would have left the
   charge running until morning with the blueprint politely not interfering.

Same hardware as the previous nights (Voyah Dream, 44 kWh), window 23:30-06:55,
efficiency 88, target 100%, starting at 67%.
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
from ha_sim import automation_this

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
    "start_time": "23:30:00",
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
    switch_age: int = 3600,
    setpoint: float = 6,
    setpoint_age: int = 230,
    status: str = "plugged_in",
    soc: float = 67,
    soc_age_min: float = 257.8,
    power: float = 0,
    delivered: float = 0.0,
    tracker: str = "home",
    flag: str = "on",
) -> dict[str, State]:
    old = now - dt.timedelta(hours=2)
    return {
        SWITCH: State(switch, last_changed=now - dt.timedelta(seconds=switch_age)),
        NUMBER: State(
            setpoint,
            {"min": 6.0, "max": 28.0, "step": 1.0},
            now - dt.timedelta(seconds=setpoint_age),
        ),
        STATUS: State(status, last_changed=old),
        POWER: State(power, last_changed=old),
        AMPERE: State(delivered, last_changed=old),
        VOLTAGE: State(218.0, last_changed=old),
        LINK: State("on", last_changed=old),
        PROBLEM: State("off", last_changed=old),
        MODE: State("immediate", last_changed=old),
        SOC: State(soc, last_changed=now - dt.timedelta(minutes=soc_age_min)),
        SOH: State(102, last_changed=old),
        TRACKER: State(tracker, last_changed=old),
        SESSION: State(flag, last_changed=old),
        "zone.home": State(1, {"friendly_name": "Home"}, now),
    }


#: ``this=None`` is a state the blueprint really meets — the window before the
#: automation entity exists — so it cannot double as "caller said nothing".
_DEFAULT = object()


@pytest.fixture
def replay(blueprint):
    def _replay(now, world, *, this=_DEFAULT, **inputs):
        merged = dict(NIGHT_INPUTS)
        merged.update(inputs)
        if this is _DEFAULT:
            this = automation_this(now - dt.timedelta(minutes=30))
        return blueprint.evaluate(world=world, now=now, inputs=merged, this=this)

    return _replay


@pytest.fixture
def replay_actions(blueprint):
    def _run(now, world, *, this=_DEFAULT, **inputs):
        merged = dict(NIGHT_INPUTS)
        merged.update(inputs)
        if this is _DEFAULT:
            this = automation_this(now - dt.timedelta(minutes=30))
        return blueprint.run_actions(world=world, now=now, inputs=merged, this=this)

    return _run


# --------------------------------------------- two triggers, one command


def test_a_run_that_starts_on_the_heels_of_another_sends_nothing(replay_actions):
    """23:30:00.295. ``window_start`` fired 204 ms earlier and already wrote.

    The setpoint entity has not caught up — it still reads 6 A, aged 230 s —
    so every entity-based check says the gap is served. Only the automation's
    own ``last_triggered`` knows a command just went out.
    """
    now = moment(23, 30)
    world = night_world(now, switch="off")
    calls = replay_actions(
        now, world, this=automation_this(now - dt.timedelta(seconds=0.204))
    )
    assert [c.get("action") for c in calls if "action" in c] == [], (
        "the second run of a racing pair must stay silent"
    )


def test_the_first_run_of_the_pair_still_writes(replay_actions):
    """The other half of the same rule: suppressing the race must not suppress
    the command. ``window_start`` itself has a normal-aged previous run."""
    now = moment(23, 30)
    world = night_world(now, switch="off")
    calls = replay_actions(now, world)
    assert [c.get("action") for c in calls if "action" in c] == ["number.set_value"]
    assert calls[0]["data"]["value"] > 6, "and it carries the plan, not the leftover"


def test_the_race_guard_reads_the_previous_run_not_this_one(replay):
    """``this`` is snapshotted before the run starts, so ``last_triggered``
    always describes the previous pass. A guard reading the current run would
    veto every command ever sent."""
    now = moment(23, 30)
    world = night_world(now, switch="off")
    ctx = replay(now, world, this=automation_this(now - dt.timedelta(seconds=0.204)))
    assert ctx["run_age"] == 0.204
    assert ctx["run_is_a_race"] is True
    assert ctx["gap_elapsed"] is False
    assert ctx["needs_write"] is False


def test_an_ordinary_recalculation_is_not_a_race(replay):
    """Half an hour between passes is the normal cadence, not a collision."""
    now = moment(2, 0)
    ctx = replay(now, night_world(now, status="charging", setpoint=13))
    assert ctx["run_is_a_race"] is False
    assert ctx["gap_elapsed"] is True


@pytest.mark.parametrize(
    ("label", "this"),
    [
        ("the automation entity does not exist yet", None),
        ("the very first run in its life", automation_this(None)),
        ("an entity carrying no such attribute", automation_this()),
    ],
)
def test_an_unreadable_last_triggered_never_blocks_a_command(replay, label, this):
    """Each of these raises in Home Assistant if read naively — ``this`` is
    ``None`` before the entity exists, and ``last_triggered`` is present-but-
    ``None`` on a fresh install, so ``get(key, default)`` returns ``None`` and
    the subtraction below it fails. All three must read as "ran long ago",
    or a fresh install would refuse to charge."""
    now = moment(23, 30)
    ctx = replay(now, night_world(now, switch="off"), this=this)
    assert ctx["run_age"] == 999999, label
    assert ctx["run_is_a_race"] is False, label
    assert ctx["needs_write"] is True, label


def test_disabling_the_throttle_disables_the_race_guard_too(replay):
    """``command_gap`` at zero means the user deliberately removed throttling.
    Reintroducing a delay through the back door would be a surprise."""
    now = moment(23, 30)
    ctx = replay(
        now,
        night_world(now, switch="off"),
        command_gap=0,
        this=automation_this(now - dt.timedelta(seconds=0.204)),
    )
    assert ctx["run_is_a_race"] is False
    assert ctx["gap_elapsed"] is True


def test_the_switch_command_is_guarded_by_the_same_race(replay):
    """Turning on is throttled by the switch's own age, which is blind in
    exactly the same way — the entity has not moved 204 ms in."""
    now = moment(23, 30)
    ctx = replay(
        now,
        night_world(now, switch="on", switch_age=3600, setpoint=13, setpoint_age=60),
        this=automation_this(now - dt.timedelta(seconds=0.204)),
    )
    assert ctx["switch_gap_elapsed"] is False


# --------------------------------------------- finishing our own stop


def test_a_stop_in_progress_is_not_mistaken_for_a_foreign_session(replay):
    """06:55:28. We switched off 28 s ago; the station has not confirmed.

    The flag is already down and the switch still reads ``on`` — the signature
    of a hand-started session. What tells them apart is the age of the switch:
    ours was commanded seconds ago, a human's has been on for hours.
    """
    now = moment(6, 56)
    world = night_world(
        now, switch="on", switch_age=28, status="charging", soc=99, flag="off"
    )
    ctx = replay(now, world)
    assert ctx["foreign_session"] is True, "it does look foreign at first glance"
    assert ctx["finishing_own_stop"] is True, "but the switch age gives it away"
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"


def test_the_repeat_of_a_swallowed_stop_actually_goes_out(replay_actions):
    """Deciding is not enough. If the station dropped the first ``turn_off``,
    the command has to be sent again — that is the whole point."""
    now = moment(6, 56)
    world = night_world(
        now, switch="on", switch_age=28, status="charging", soc=99, flag="off"
    )
    calls = [c.get("action") for c in replay_actions(now, world) if "action" in c]
    assert calls[:2] == ["switch.turn_off", "input_boolean.turn_off"]


def test_a_genuinely_manual_session_survives_the_end_of_the_window(replay):
    """The protection this must not break. Somebody who switched the charger on
    by hand chose their own current, and the end of the window is not our
    business — their switch has been on far longer than a command gap."""
    now = moment(6, 56)
    world = night_world(
        now, switch="on", switch_age=7200, status="charging", soc=80, flag="off"
    )
    world[NUMBER] = State(
        10, {"min": 6.0, "max": 28.0, "step": 1.0}, now - dt.timedelta(hours=2)
    )
    ctx = replay(now, world)
    assert ctx["foreign_session"] is True
    assert ctx["finishing_own_stop"] is False
    assert ctx["must_stop"] is False, "not ours to switch off"


def test_a_manual_session_inside_the_window_keeps_its_own_current(replay):
    """The same protection on the writing side, and the one a careless fix to
    ``foreign_session`` would have broken: a dropout rejuvenates the switch,
    and the blueprint would start overwriting a human's chosen 10 A."""
    now = moment(3, 0)
    world = night_world(
        now, switch="on", switch_age=1, status="charging", soc=80, flag="off"
    )
    world[NUMBER] = State(
        10, {"min": 6.0, "max": 28.0, "step": 1.0}, now - dt.timedelta(hours=2)
    )
    ctx = replay(now, world)
    assert ctx["foreign_session"] is True, "a fresh switch does not make it ours"
    assert ctx["needs_write"] is False, "so the chosen current stands"


def test_a_stopping_session_with_the_flag_still_up_is_unaffected(replay):
    """The ordinary case, where the flag has not been lowered yet: nothing
    about it looks foreign, and the new term must not change the answer."""
    now = moment(6, 56)
    world = night_world(
        now, switch="on", switch_age=28, status="charging", soc=99, flag="on"
    )
    ctx = replay(now, world)
    assert ctx["foreign_session"] is False
    assert ctx["finishing_own_stop"] is False
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"


# --------------------------------------------- the gentle finish and a dropout


def test_a_dropped_out_switch_does_not_lift_the_gentle_finish(replay):
    """06:31:06, and the one line in the night that did not fit.

    The gentle finish caps the current at what is already flowing, but the cap
    only applied while ``switch_on`` — and a dropout takes the switch to
    ``unavailable``, not to ``off``. So the pass computed 18 A against a 14 A
    cap at 99% charge. Nothing was sent only because the station was offline
    too; had the switch alone dropped, the full 28 A would have gone into an
    almost-full battery.
    """
    now = moment(6, 31)
    world = night_world(
        now, switch="unavailable", status="charging", soc=99,
        setpoint=14, setpoint_age=5376, delivered=12.6, power=2760,
    )
    ctx = replay(now, world)
    assert ctx["switch_on"] is False
    assert ctx["switch_off_confirmed"] is False, "unavailable is not off"
    assert ctx["gentle_finish_active"] is True
    assert ctx["desired_current"] == 14, "the cap must survive the dropout"


def test_the_gentle_finish_still_releases_before_a_new_session(replay):
    """The reason the switch was in that condition at all: a session that has
    genuinely ended must not inherit last night's setpoint as a ceiling."""
    now = moment(6, 31)
    world = night_world(
        now, switch="off", switch_age=7200, status="plugged_in", soc=96,
        setpoint=7, setpoint_age=7200, delivered=0.0, power=0,
    )
    ctx = replay(now, world)
    assert ctx["switch_off_confirmed"] is True
    assert ctx["gentle_finish_active"] is True
    assert ctx["calc_current"] > 7, "the plan does want more than the leftover 7 A"
    assert ctx["desired_current"] > 7, "a confirmed off does release the cap"


# --------------------------------------------- what the night got right


def test_a_repeated_stop_command_is_not_throttled_away(replay_actions):
    """06:55:28 sent ``turn_off`` again 28 s after the first, and it was right
    to: the switch still read ``on`` and 12.6 A were still flowing. Safety
    commands are not subject to the command gap."""
    now = moment(6, 56)
    world = night_world(
        now,
        switch="on",
        switch_age=28,
        status="charging",
        soc=99,
        delivered=12.638,
        power=2756,
        flag="on",
    )
    calls = [c.get("action") for c in replay_actions(now, world) if "action" in c]
    assert "switch.turn_off" in calls


def test_the_deadband_holds_one_setpoint_for_five_hours(replay):
    """13 A from 23:30 to 05:00 was not a stuck regulator. The plan drifted
    between 12.0 and 12.8 A all night, and a 2 A deadband is what kept those
    fractions from becoming eleven pointless writes."""
    now = moment(2, 0)
    ctx = replay(
        now,
        night_world(
            now,
            status="charging",
            setpoint=13,
            setpoint_age=6043,
            soc=78,
            soc_age_min=0.7,
            delivered=11.799,
            power=2593,
        ),
    )
    assert 12.0 <= ctx["calc_current"] <= 12.8
    assert ctx["desired_current"] == 13
    assert ctx["needs_write"] is False
