"""The night of 2026-08-03, replayed from the traces it actually produced.

The third run on real hardware reached 100%, but wasted the first half hour and
then had to sprint. Three separate faults, all visible in the traces:

1. **The start was lost.** At 23:05 the setpoint went out (16 -> 11 A). At
   23:06 the queued ``switch.turn_on`` followed — and the station swallowed it.
   The switch stayed ``off``, and *nothing happened for twenty-four minutes*,
   until the 23:30 tick repeated the command. A command that fails to take
   effect produces no state change, and every trigger the blueprint had was
   either a clock or somebody else's state change.

2. **Setpoint writes were lost the same way.** ``11 -> 12`` was written at
   23:30, 00:00 and 00:30; only the third one stuck. The proof is in
   ``current_age``, which kept growing (4761 -> 6561 -> 8361 s) across all
   three passes: the setpoint never moved. Same again for ``13 -> 16`` at 03:00
   and 03:30. Every miss cost a full recalculation interval.

3. **The plan ran optimistic all night and paid for it at dawn.** Efficiency
   was configured at 80% against a real 74.6% (17.46 kWh from the wall for
   13.02 kWh into the battery), and the station delivered ~10% under whatever
   was asked. The regulator only noticed once the horizon collapsed, ramping
   13 -> 16 -> 20 -> 23 -> 27 -> 28 A inside six minutes.

Same hardware as the previous nights (Voyah Dream, 44 kWh, SoH 102%, Afyeev
32 A over tuya-local), window 23:05-06:55, target 100%, starting at 71%.
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
from ha_sim import InputRef

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
    "efficiency": 80,
    "time_reserve_minutes": 15,
    "command_gap": 60,
    # The night ran without either knob; individual tests switch them on.
    "current_headroom": 0,
    "gentle_finish_soc": 100,
}


def night_world(
    now: dt.datetime,
    *,
    switch: str = "off",
    setpoint: float = 11,
    setpoint_age: int = 3600,
    status: str = "plugged_in",
    soc: float = 71,
    delivered: float = 0.0,
    power: float = 0,
    flag: str = "off",
) -> dict[str, State]:
    old = now - dt.timedelta(hours=2)
    return {
        SWITCH: State(switch, last_changed=old),
        NUMBER: State(
            setpoint,
            {"min": 6.0, "max": 32.0, "step": 1.0},
            now - dt.timedelta(seconds=setpoint_age),
        ),
        STATUS: State(status, last_changed=old),
        POWER: State(power, last_changed=old),
        AMPERE: State(delivered, last_changed=old),
        VOLTAGE: State(220.0, last_changed=old),
        LINK: State("on", last_changed=old),
        PROBLEM: State("off", last_changed=old),
        MODE: State("immediate", last_changed=old),
        SOC: State(soc, last_changed=now - dt.timedelta(minutes=10)),
        SOH: State(102, last_changed=old),
        TRACKER: State("home", last_changed=old),
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


# ------------------------------------------------- a command that vanished


def test_a_swallowed_turn_on_is_retried_without_waiting_for_the_tick(replay_actions):
    """23:06 on the third night, one minute later.

    The setpoint has settled, the switch is still confirmed ``off``, and there
    is nothing left to write — so the only thing owed is the switch itself.
    Before the retry trigger existed this state was reachable only on the half
    hour, which is exactly why the car sat idle until 23:30.
    """
    now = moment(23, 7, day=3, month=8)
    world = night_world(now, switch="off", setpoint=10, setpoint_age=120)
    calls = replay_actions(now, world)
    assert "switch.turn_on" in [c["action"] for c in calls if "action" in c]


def test_the_retry_trigger_watches_the_switch_staying_off(blueprint):
    """The miss itself is silent, so the trigger has to be about *not* moving.

    ``for:`` on a state that never changed is the only way Home Assistant can
    say "this has failed to happen".
    """
    triggers = {t.get("id"): t for t in blueprint.triggers}
    missed = triggers["command_missed"]
    assert missed["to"] == "off"
    assert missed["for"] == {"seconds": InputRef("command_gap")}
    assert "not_from" not in missed, "coming back from unavailable must still fire"


def test_the_stale_setpoint_trigger_waits_longer_than_a_normal_write(blueprint):
    """A healthy write leaves the setpoint untouched for exactly ``command_gap``
    seconds, which is what ``current_written`` already keys off. Firing on the
    same interval would make every normal write look like a failure."""
    triggers = {t.get("id"): t for t in blueprint.triggers}
    stale = triggers["setpoint_stale"]
    assert stale["for"] != {"seconds": InputRef("command_gap")}
    assert "* 2" in str(stale["for"]["seconds"]), "twice the command gap"


def test_a_charger_that_is_simply_off_is_not_hammered(replay_actions):
    """The retry must not turn into a command every minute.

    Outside the window the same trigger fires just as often; nothing may go out.
    """
    now = moment(21, 0, day=3, month=8)
    world = night_world(now, switch="off", setpoint_age=3600)
    calls = replay_actions(now, world)
    actions = [c["action"] for c in calls if "action" in c]
    assert "switch.turn_on" not in actions


# ------------------------------------------------- the car that never left


def test_a_dropout_does_not_decide_the_car_drove_away(replay):
    """01:35:42 on the third night.

    The tracker had been claiming ``not_home`` for four hours from GPS drift
    while the car sat plugged in — the case ``physically_present`` exists for.
    Then a one-second dropout took the switch to ``unavailable``, and the old
    ``not switch_on`` read that as "not charging", so ``car_left`` went true
    while the power sensor was reading 2616 W in the same pass.

    Nothing was switched off only because ``charger_online`` was false at that
    instant too, which is luck rather than design: an unavailable *switch* with
    a live *station* would have stopped a healthy session.
    """
    now = moment(1, 35, day=4, month=8)
    world = night_world(now, switch="unavailable", status="charging", soc=90,
                        setpoint=13, delivered=11.6, power=2616,
                        setpoint_age=1800, flag="on")
    world[TRACKER] = State("not_home", last_changed=now - dt.timedelta(hours=4))
    ctx = replay(now, world)
    assert ctx["switch_off_confirmed"] is False, "unavailable is not off"
    assert ctx["car_left"] is False, "a dropout is not the car driving away"
    assert ctx["must_stop"] is False


def test_gps_drift_alone_never_stops_a_charge_that_is_visibly_running(replay):
    """What this protection is worth, taken from the *first* night's traces.

    At 01:30 that night the car was drawing 3890 W at 17.6 A with the cable in,
    charged to 83% against a target of 100 — and the tracker drifted to
    ``not_home``. The blueprint of the day had no notion of physical presence,
    so it stopped the session with ``car_not_home`` two hours before the window
    closed, and the charge never resumed.

    Replayed here against the current logic, which must keep charging.
    """
    now = moment(1, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=83,
                        setpoint=19, delivered=17.618, power=3890,
                        setpoint_age=1800, flag="on")
    world[TRACKER] = State("not_home", last_changed=now - dt.timedelta(minutes=5))
    ctx = replay(now, world)
    assert ctx["car_home"] is False, "the tracker really is claiming not_home"
    assert ctx["physically_present"] is True, "but the power sensor says otherwise"
    assert ctx["must_stop"] is False
    assert ctx["stop_reason"] == "none"


def test_a_confirmed_off_switch_still_means_nothing_is_flowing(replay):
    """The guard must not cost the honest reading it was guarding.

    With the switch genuinely ``off`` there is no session to protect, and a
    stale power reading must not keep claiming the car is present.
    """
    now = moment(1, 35, day=4, month=8)
    world = night_world(now, switch="off", status="charging", soc=90,
                        setpoint=13, delivered=11.6, power=2616,
                        setpoint_age=1800, flag="on")
    world[TRACKER] = State("not_home", last_changed=now - dt.timedelta(hours=4))
    ctx = replay(now, world)
    assert ctx["switch_off_confirmed"] is True
    assert ctx["physically_present"] is False


# ------------------------------------------------- the headroom


def test_without_headroom_the_plan_asks_for_exactly_what_it_computed(replay):
    """The baseline the first two nights ran on, kept for comparison."""
    now = moment(23, 30, day=3, month=8)
    world = night_world(now, switch="on", status="charging", soc=81,
                        setpoint=11, delivered=9.9, power=2211)
    ctx = replay(now, world, current_headroom=0)
    assert ctx["desired_current"] == 7


def test_headroom_asks_for_more_from_the_very_first_hour(replay):
    """The whole point: the correction arrives while there is still time to use
    it, instead of surfacing as a sprint at dawn."""
    now = moment(23, 30, day=3, month=8)
    world = night_world(now, switch="on", status="charging", soc=81,
                        setpoint=11, delivered=9.9, power=2211)
    plain = replay(now, world, current_headroom=0)["desired_current"]
    padded = replay(now, world, current_headroom=15)["desired_current"]
    assert padded > plain


def test_headroom_never_breaks_the_ceiling(replay):
    """It is a planning correction, not a licence to exceed the wiring limit."""
    now = moment(6, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=13, delivered=11.6, power=2592)
    ctx = replay(now, world, current_headroom=30)
    assert ctx["desired_current"] <= NIGHT_INPUTS["max_current"]


def test_headroom_is_a_multiplier_so_a_settled_setpoint_stays_settled(replay):
    """A constant *added* to the plan would sit permanently outside the deadband
    and rewrite the setpoint on every pass. A multiplier converges instead:
    once the station is on the padded value, the plan agrees with it."""
    now = moment(1, 0, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=87,
                        setpoint=13, delivered=11.6, power=2600, setpoint_age=1800)
    first = replay(now, world, current_headroom=10)
    settled = night_world(now, switch="on", status="charging", soc=87,
                          setpoint=first["desired_current"], delivered=11.6,
                          power=2600, setpoint_age=1800)
    again = replay(now, settled, current_headroom=10)
    assert again["want_write"] is False, "the padded setpoint is a fixed point"


# ------------------------------------------------- the gentle finish


def test_the_gentle_finish_refuses_to_raise_current_on_the_last_percents(replay):
    """06:30 on the third night, at 99%.

    This is the ramp the user asked to stop: the plan wanted 16 A and kept
    climbing to 28, while the car — already in absorption — was never going to
    take it. The BMS tapers anyway; the extra amperes only heat the pack.
    """
    now = moment(6, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=13, delivered=11.6, power=2592, setpoint_age=1800)
    free = replay(now, world, gentle_finish_soc=100)
    gentle = replay(now, world, gentle_finish_soc=95)
    assert free["desired_current"] > 13, "unrestrained, the plan sprints"
    assert gentle["desired_current"] == 13, "held at whatever already stands"
    assert gentle["gentle_finish_active"] is True


def test_the_gentle_finish_still_allows_lowering_the_current(replay):
    """One-directional. A setpoint left high by the ramp must still come down,
    or it would stay pinned for the rest of the session.

    Charge is nearly complete and the horizon is still wide, so the plan itself
    calls for very little: the cap must not hold the setpoint up at 28 A.
    """
    now = moment(23, 30, day=3, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=28, delivered=25.0, power=5500, setpoint_age=1800)
    ctx = replay(now, world, gentle_finish_soc=95)
    assert ctx["gentle_finish_active"] is True
    assert ctx["desired_current"] < 28


def test_the_verdict_says_the_current_was_held_back_on_purpose(replay):
    """Without its own line, a held current renders as "ток в норме" — which
    reads as the regulator failing, exactly the thing this night was spent
    diagnosing. The journal has to name the choice."""
    now = moment(6, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99, flag="on",
                        setpoint=13, delivered=11.6, power=2592, setpoint_age=1800)
    ctx = replay(now, world, gentle_finish_soc=95)
    assert "щадящий финиш" in ctx["verdict"]
    assert "13" in ctx["verdict"], "the current it is being held at"


def test_diag_reports_both_new_knobs(replay):
    """Anything that changes the current has to be visible when reading traces
    back, or the next night's diagnosis starts by guessing the settings."""
    now = moment(6, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=13, delivered=11.6, power=2592, setpoint_age=1800)
    plan = replay(now, world, gentle_finish_soc=95, current_headroom=10)["diag"]["план"]
    assert plan["запас_тока_проц"] == 10
    assert plan["щадящий_финиш"] is True


def test_below_the_threshold_the_regulator_is_untouched(replay):
    """The middle of the night must behave exactly as before."""
    now = moment(2, 0, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=85,
                        setpoint=11, delivered=9.9, power=2222, setpoint_age=1800)
    free = replay(now, world, gentle_finish_soc=100)
    gentle = replay(now, world, gentle_finish_soc=95)
    assert gentle["gentle_finish_active"] is False
    assert gentle["desired_current"] == free["desired_current"]


def test_a_hundred_percent_switches_the_gentle_finish_off(replay):
    """The documented way to disable it."""
    now = moment(6, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=13, delivered=11.6, power=2592, setpoint_age=1800)
    assert replay(now, world, gentle_finish_soc=100)["gentle_finish_active"] is False


def test_unreliable_charge_data_does_not_trigger_the_gentle_finish(replay):
    """A frozen or absent percentage must not freeze the current all night.

    No separate guard does this: an untrusted reading already collapses ``soc``
    to -1, which is below any threshold the selector allows. Asserted here so
    the reasoning stays checked rather than assumed.
    """
    now = moment(6, 30, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=13, delivered=11.6, power=2592, setpoint_age=1800)
    world[SOC] = State(99, last_changed=now - dt.timedelta(hours=6))
    ctx = replay(now, world, gentle_finish_soc=95)
    assert ctx["soc_valid"] is False
    assert ctx["soc"] == -1
    assert ctx["gentle_finish_active"] is False


def test_an_emergency_top_up_ignores_the_gentle_finish(replay):
    """Emergency means "every ampere counts"; a comfort feature must not stand
    in its way.

    The two genuinely collide here: the charge is above the gentle threshold
    *and* below the emergency one, so only the precedence decides.
    """
    now = moment(14, 0, day=4, month=8)
    world = night_world(now, switch="on", status="charging", soc=99,
                        setpoint=13, delivered=11.6, power=2592, setpoint_age=1800)
    ctx = replay(now, world, gentle_finish_soc=95, emergency_soc=100,
                 emergency_hysteresis=0)
    assert ctx["emergency"] is True
    assert ctx["soc"] >= 95, "the gentle threshold is met as well"
    assert ctx["gentle_finish_active"] is False
