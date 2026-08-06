"""The night of 2026-08-04, replayed from the traces it actually produced.

The fourth run was the first on 1.4.0, and the mechanisms added the night
before turned out to have costs of their own. The traces show four faults:

1. **Half the night was noise.** 65 passes, against 42 the night before. Of
   them, 17 came from ``current_written`` and 16 from ``car_arrived`` — and in
   all sixteen of the latter the car was sitting at home with
   ``tracker_state=home``, exactly where it had been all night. Home Assistant
   re-evaluates a template trigger whenever any entity it mentions updates,
   and a ``device_tracker`` updates its coordinates constantly.

2. **The current oscillated: 18 -> 19 -> 20 -> 17 -> 18 A inside four
   minutes** (05:24-05:27), every pass fired by ``current_written`` — that is,
   by the blueprint's own previous write. ``hours_left`` shrinks continuously
   while ``needed_kwh`` only drops when the car reports a new percentage, so
   between reports the plan creeps upward by roughly a quarter amp a minute.
   Each write woke the automation ``command_gap`` later, which wrote again.
   The ratchet broke only when soc finally moved 92 -> 93 and knocked the
   calculation down 2.4 A in one step.

3. **The plan asked for far more than the car needed.** Efficiency was set to
   75% against a real 88.8% — the meter had been reset the previous day, so
   its closing value *is* the night's total: 35.38 kWh from the wall for
   (99-29)% x 44.88 = 31.42 kWh into the battery. The plan demanded 42.49 kWh,
   an overestimate of 1.18x, which is why it opened at 28 A and spent the
   whole night walking back down.

4. **A reset cumulative meter passes the plausibility check.** The guard
   rejects a session counter reading implausibly high, but power-cycling the
   station sent this one back to zero, and at 35 kWh against a 89 kWh ceiling
   it now looks perfectly reasonable while still being cumulative.

Same hardware as the previous nights (Voyah Dream, 44 kWh, SoH 102%, Afyeev
32 A over tuya-local), window 23:10-06:55, target 100%, starting at 29%.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    AMPERE,
    ENERGY,
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
    "start_time": "23:10:00",
    "stop_time": "06:55:00",
    "target_soc": 100,
    "min_current": 6,
    "max_current": 32,
    "current_step": 1,
    "phases": "1",
    "efficiency": 75,
    "time_reserve_minutes": 15,
    "command_gap": 60,
    "soc_freeze_minutes": 60,
    "current_headroom": 10,
    "gentle_finish_soc": 95,
}


def night_world(
    now: dt.datetime,
    *,
    switch: str = "on",
    setpoint: float = 18,
    setpoint_age: int = 3600,
    status: str = "charging",
    soc: float = 92,
    soc_age_min: float = 1,
    power: float = 3700,
    delivered: float = 16.7,
    flag: str = "on",
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
        VOLTAGE: State(221.0, last_changed=old),
        LINK: State("on", last_changed=old),
        PROBLEM: State("off", last_changed=old),
        MODE: State("immediate", last_changed=old),
        SOC: State(soc, last_changed=now - dt.timedelta(minutes=soc_age_min)),
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


# ------------------------------------------------- the current that oscillated


def test_a_rise_on_a_stale_percentage_is_held_back(replay):
    """05:25 on the fourth night, the second step of the ratchet.

    soc has read 92% for nearly nine minutes while ``hours_left`` kept
    shrinking, so the plan now asks for more current than the setpoint carries.
    Nothing has actually changed about the car — only the clock — so the rise
    carries no information and must not be written.
    """
    now = moment(5, 25, day=5, month=8)
    world = night_world(now, setpoint=19, soc=92, soc_age_min=8.7, setpoint_age=60)
    out = replay(now, world)
    assert out["current_rising"] is True
    assert out["soc_fresh_enough_to_rise"] is False
    assert out["want_write"] is False


def test_the_same_rise_goes_out_once_the_percentage_is_fresh(replay):
    """The guard is about staleness, not about rises.

    Identical arithmetic, but the car has just reported — so the request is
    backed by real information and must be honoured.
    """
    now = moment(5, 25, day=5, month=8)
    world = night_world(now, setpoint=19, soc=92, soc_age_min=0.5, setpoint_age=60)
    out = replay(now, world)
    assert out["current_rising"] is True
    assert out["soc_fresh_enough_to_rise"] is True
    assert out["want_write"] is True


def test_lowering_the_current_never_waits_for_a_fresh_percentage(replay):
    """05:26, when soc finally moved 92 -> 93 and the plan fell 2.4 A.

    Coming *down* is safe and must stay reactive: the deadband already guards
    it, and holding a reduction back would leave the car pulling more than the
    plan calls for.
    """
    now = moment(5, 26, day=5, month=8)
    world = night_world(now, setpoint=20, soc=93, soc_age_min=45, setpoint_age=60)
    out = replay(now, world)
    assert out["current_rising"] is False
    assert out["soc_fresh_enough_to_rise"] is False, "stale on purpose"
    assert out["want_write"] is True, "a reduction is not gated by freshness"


def test_the_ratchet_does_not_restart_itself_a_minute_later(replay_actions):
    """The loop that made the oscillation self-sustaining.

    Every write woke the automation ``command_gap`` later through
    ``current_written``. If that pass writes again, the cycle repeats until the
    next percentage arrives — which is exactly the 18/19/20/17/18 sequence.
    """
    now = moment(5, 26, day=5, month=8)
    world = night_world(now, setpoint=20, soc=92, soc_age_min=9.7, setpoint_age=60)
    calls = replay_actions(now, world)
    assert "number.set_value" not in [c.get("action") for c in calls]


def test_a_start_is_never_delayed_by_a_stale_percentage(replay):
    """The guard must not reach the opening write.

    Before the session starts there is no setpoint worth trusting, and the
    percentage is routinely stale at 23:10 — the car has been parked for hours.
    Gating the start on freshness would lose the whole window.
    """
    now = moment(23, 10, day=4, month=8)
    world = night_world(
        now, switch="off", status="plugged_in", setpoint=6, soc=29,
        soc_age_min=260, power=0, delivered=0, flag="off",
    )
    out = replay(now, world)
    assert out["soc_fresh_enough_to_rise"] is False
    assert out["want_write"] is True, "the opening write must still go out"


def test_a_rise_is_judged_against_our_own_last_write(replay):
    """The comparison is relative, not absolute, and that is the whole point.

    A percentage older than the setpoint means nothing has been learned since
    we last wrote — that rise is the ratchet. A percentage *younger* than the
    setpoint means the car reported after that write, so the plan has already
    taken the report into account and the rise is genuine, however old the
    percentage looks on an absolute scale.
    """
    now = moment(5, 25, day=5, month=8)
    stale = night_world(now, setpoint=19, soc=92, soc_age_min=8.7, setpoint_age=60)
    assert replay(now, stale)["soc_fresh_enough_to_rise"] is False

    reported_since = night_world(
        now, setpoint=19, soc=92, soc_age_min=8.7, setpoint_age=3600
    )
    assert replay(now, reported_since)["soc_fresh_enough_to_rise"] is True


def test_a_plan_that_does_not_run_on_soc_is_never_gated(replay):
    """With the percentage unusable the plan falls back to session energy or to
    the reserve current, and neither has anything to do with soc freshness."""
    now = moment(5, 25, day=5, month=8)
    world = night_world(now, setpoint=19, soc=92, soc_age_min=8.7, setpoint_age=60)
    world[SOC] = State("unavailable", last_changed=now - dt.timedelta(hours=3))
    out = replay(now, world)
    assert out["plan_source"] != "soc"
    assert out["soc_fresh_enough_to_rise"] is True


def test_the_held_rise_is_visible_in_the_verdict(replay):
    """A suppressed rise must not read as "the current is fine" — that is
    indistinguishable from healthy regulation when the night is reviewed."""
    now = moment(5, 25, day=5, month=8)
    world = night_world(now, setpoint=19, soc=92, soc_age_min=8.7, setpoint_age=60)
    out = replay(now, world)
    assert "ждём свежий заряд" in out["verdict"]


# ------------------------------------------------- triggers that fired for nothing


def test_the_arrival_trigger_ignores_coordinate_jitter(blueprint):
    """Sixteen passes, every one with the car already at home.

    ``for:`` is what separates a real arrival from the template flickering
    while Home Assistant recomputes it on a coordinate update.
    """
    triggers = {t.get("id"): t for t in blueprint.triggers}
    assert triggers["car_arrived"]["for"] == {"seconds": 30}


def test_the_target_trigger_is_debounced_the_same_way(blueprint):
    """Same exposure: the battery sensor is a cloud entity whose attributes
    churn faster than the percentage itself."""
    triggers = {t.get("id"): t for t in blueprint.triggers}
    assert triggers["target_hit"]["for"] == {"seconds": 30}


def test_the_setpoint_is_watched_by_exactly_one_trigger(blueprint):
    """1.4.0 had two, at ``command_gap`` and twice that. The second never fired
    in a whole night: the first always got there first, and its pass either
    rewrote the setpoint or restarted the clock either way."""
    on_setpoint = [
        t for t in blueprint.triggers
        if t.get("entity_id") == InputRef("charger_current_number")
    ]
    assert len(on_setpoint) == 1, on_setpoint
    assert on_setpoint[0]["id"] == "current_written"


# ------------------------------------------------- the meter that was reset


def test_a_reset_cumulative_meter_is_still_rejected(replay):
    """The station was power-cycled, so its lifetime counter restarted at zero
    and climbed to 35 kWh — comfortably under the 89 kWh ceiling that used to
    be the only thing catching a wrong sensor. Before the session starts a
    session meter reads zero; this one does not.
    """
    now = moment(23, 10, day=4, month=8)
    world = night_world(
        now, switch="off", status="plugged_in", power=0, delivered=0, flag="off",
    )
    world[ENERGY] = State(35.38, last_changed=now - dt.timedelta(hours=2))
    # No percentage, so the energy plan is the only one left — which is exactly
    # when accepting a lifetime counter would do damage: 35 kWh reads as "already
    # delivered", the target looks met, and the night silently never starts.
    world[SOC] = State("unavailable", last_changed=now - dt.timedelta(hours=3))
    out = replay(now, world, session_energy_sensor=ENERGY, session_energy_target=40)
    assert out["energy_looks_cumulative"] is True
    assert out["energy_valid"] is False
    assert out["plan_source"] != "energy", "a lifetime counter must not drive the plan"


def test_a_genuine_session_meter_at_zero_is_accepted(replay):
    """The counterpart: a real session meter sits at zero before the session,
    and must go on being usable as a plan source."""
    now = moment(23, 10, day=4, month=8)
    world = night_world(
        now, switch="off", status="plugged_in", power=0, delivered=0, flag="off",
    )
    world[ENERGY] = State(0.0, last_changed=now - dt.timedelta(hours=2))
    out = replay(now, world, session_energy_sensor=ENERGY, session_energy_target=40)
    assert out["energy_looks_cumulative"] is False
    assert out["energy_valid"] is True


def test_a_running_session_is_never_judged_by_its_meter_reading(replay):
    """Mid-session both kinds of meter read non-zero, so the test is meaningless
    there and must not fire — otherwise the plan source would drop out halfway
    through every night."""
    now = moment(2, 0, day=5, month=8)
    world = night_world(now, soc=61)
    world[ENERGY] = State(12.0, last_changed=now - dt.timedelta(minutes=5))
    out = replay(now, world, session_energy_sensor=ENERGY, session_energy_target=40)
    assert out["energy_looks_cumulative"] is False
    assert out["energy_valid"] is True


# ------------------------------------------------- what the night got right


def test_the_gentle_finish_held_the_last_percent(replay):
    """06:30, soc 99% against a 95% threshold: the ramp is refused on purpose.

    The night ended at 99% rather than 100%, and this is why. It is the
    intended trade — a percent of range against a hard finish on the pack.
    """
    now = moment(6, 30, day=5, month=8)
    world = night_world(now, setpoint=14, soc=99, soc_age_min=11.6, delivered=12.8)
    out = replay(now, world)
    assert out["gentle_finish_active"] is True
    assert out["desired_current"] <= 14


def test_the_opening_current_follows_from_the_configured_efficiency(replay):
    """23:10, soc 29%: the plan asked for 28 A and the arithmetic was sound —
    it was the 75% efficiency feeding it that was wrong, not the formula.

    Documented here so that changing the efficiency guidance later shows up as
    a deliberate change rather than a silent drift.
    """
    now = moment(23, 10, day=4, month=8)
    world = night_world(
        now, switch="off", status="plugged_in", setpoint=6, soc=29,
        soc_age_min=260, power=0, delivered=0, flag="off",
    )
    out = replay(now, world)
    assert out["desired_current"] == pytest.approx(28, abs=1)
