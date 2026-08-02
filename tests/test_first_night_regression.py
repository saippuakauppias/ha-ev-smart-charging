"""The night of 2026-08-01, replayed from the traces it actually produced.

The blueprint's first run on real hardware ended with the car at 83% instead
of 100%. Three defects contributed, and each one is pinned here against the
readings that exposed it - not against numbers invented for a test.

The values come from the downloaded automation traces: a Voyah Dream (44 kWh,
declared SoH 102%) on an Afyeev 32 A charger over tuya-local, window
00:05-06:55, target 100%, starting at 51%.

Two things make this file worth its weight:

* the fixtures are real. A hand-written scenario would have used a round
  17.6 A of delivered current against a 19 A setpoint; the actual gap, the
  actual voltage drift and the actual SoC curve are what made the defects
  visible in the first place.
* every case states what the blueprint *did* that night and what it must do
  now. A regression would flip them back.
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
    "home_zone": "zone.home",
    "battery_capacity": 44,
    "nominal_voltage": 210,
    "start_time": "00:05:00",
    "stop_time": "06:55:00",
    "target_soc": 100,
    "min_current": 6,
    "max_current": 28,
    "current_step": 1,
    "phases": "1",
    "efficiency": 88,
    "time_reserve_minutes": 30,
    "command_gap": 60,
}

#: Readings lifted from the traces, keyed by local time.
#: ``setpoint`` is what stood on the charger, ``delivered`` what actually flowed.
NIGHT_READINGS = {
    "00:05": {"soc": 51, "setpoint": 20, "delivered": 0.0, "power": 0,
              "volts": 0, "status": "plugged_in", "tracker": "home", "on": False},
    "03:00": {"soc": 72, "setpoint": 19, "delivered": 17.647, "power": 3856,
              "volts": 218.5, "status": "charging", "tracker": "home", "on": True},
    "04:00": {"soc": 80, "setpoint": 19, "delivered": 17.622, "power": 3894,
              "volts": 220.7, "status": "charging", "tracker": "home", "on": True},
    # 04:30 is the moment the session died: the tracker said the car had left
    # while 17.6 A were flowing into it.
    "04:30": {"soc": 83, "setpoint": 19, "delivered": 17.618, "power": 3890,
              "volts": 220.8, "status": "charging", "tracker": "not_home", "on": True},
}


def world_at(label: str) -> tuple[dt.datetime, dict[str, State]]:
    hour, minute = (int(part) for part in label.split(":"))
    now = moment(hour, minute, day=2, month=8)
    r = NIGHT_READINGS[label]
    # Before the window the switch had been off since the previous evening;
    # once charging it had been on since 00:05. Either way it is old enough
    # for the command gap, which is what the traces show.
    switch_age = dt.timedelta(hours=5 if not r["on"] else 3)
    return now, {
        SWITCH: State("on" if r["on"] else "off", last_changed=now - switch_age),
        NUMBER: State(r["setpoint"], {"min": 6.0, "max": 32.0, "step": 1.0},
                      now - dt.timedelta(hours=4)),
        STATUS: State(r["status"], last_changed=now - dt.timedelta(minutes=30)),
        POWER: State(r["power"], last_changed=now),
        AMPERE: State(r["delivered"], last_changed=now),
        VOLTAGE: State(r["volts"], last_changed=now),
        LINK: State("on", last_changed=now),
        PROBLEM: State("off", last_changed=now),
        MODE: State("immediate", last_changed=now - dt.timedelta(hours=4)),
        SOC: State(r["soc"], last_changed=now - dt.timedelta(minutes=5)),
        # The car reports 102% state of health, which is what turns the
        # configured 44 kWh into the 44.88 kWh the traces show.
        SOH: State(102, last_changed=now - dt.timedelta(hours=6)),
        TRACKER: State(r["tracker"], last_changed=now),
        "zone.home": State(1, {"friendly_name": "Home"}, now),
    }


@pytest.fixture
def replay(blueprint):
    def _replay(label: str, **overrides):
        now, world = world_at(label)
        world.update(overrides)
        inputs = dict(NIGHT_INPUTS)
        return blueprint.evaluate(world=world, now=now, inputs=inputs)

    return _replay


# --------------------------------------------------- the session-ending bug


def test_the_tracker_lie_at_0430_no_longer_ends_the_session(replay):
    """What actually happened: ``must_stop`` went true and the charger was
    switched off with 2.4 hours of window and 17 percentage points to go."""
    ctx = replay("04:30")
    assert ctx["tracker_state"] == "not_home"
    assert ctx["actual_current"] == 17.618, "current was flowing the whole time"
    assert ctx["physically_present"] is True
    assert ctx["car_left"] is False
    assert ctx["must_stop"] is False
    assert ctx["should_charge"] is True


def test_the_car_really_leaving_still_ends_the_session(replay):
    """The same instant, but with the cable actually pulled."""
    now, _ = world_at("04:30")
    ctx = replay(
        "04:30",
        **{
            STATUS: State("available", last_changed=now),
            POWER: State(0, last_changed=now),
            AMPERE: State(0, last_changed=now),
        },
    )
    assert ctx["car_left"] is True
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "car_not_home"


# ------------------------------------------------------ the deadband defect


@pytest.mark.parametrize("label", ["03:00", "04:00", "04:30"])
def test_the_regulator_was_asking_for_more_current_and_was_ignored(replay, label):
    """Three recalculations in a row asked for a higher setpoint. Every one was
    swallowed by the 3 A deadband, so the setpoint sat at 19 A all night while
    the plan fell further behind."""
    ctx = replay(label)
    assert ctx["desired_current"] > ctx["current_now"], "the plan wanted more"
    assert ctx["desired_current"] - ctx["current_now"] < 3, "and was swallowed"
    assert ctx["current_rising"] is True
    assert ctx["needs_write"] is True, "now it gets written"


def test_the_delivered_current_stayed_below_the_setpoint_all_night(replay):
    """The charger under-delivers by about 1.4 A. That is normal and must not
    by itself trigger a rewrite - the deadband compares setpoint to setpoint."""
    for label in ("03:00", "04:00", "04:30"):
        ctx = replay(label)
        shortfall = ctx["current_now"] - ctx["actual_current"]
        assert 1.3 < shortfall < 1.5, f"{label}: {shortfall}"


# ------------------------------------------------------- the command volley


def test_the_start_no_longer_sends_two_commands_at_once(blueprint):
    """At 00:05 the setpoint and the switch-on went out 14 ms apart. Cheap
    chargers drop the second command of such a pair."""
    now, world = world_at("00:05")
    calls = blueprint.run_actions(world=world, now=now, inputs=dict(NIGHT_INPUTS))
    actions = [c["action"] for c in calls if "action" in c]
    assert "number.set_value" in actions
    assert "switch.turn_on" not in actions, "the switch waits for the next run"


def test_the_switch_goes_out_once_the_setpoint_has_settled(blueprint):
    """The follow-up run: the setpoint is correct and old enough."""
    now, world = world_at("00:05")
    world[NUMBER] = State(19, {"min": 6.0, "max": 32.0, "step": 1.0},
                          now - dt.timedelta(seconds=90))
    calls = blueprint.run_actions(world=world, now=now, inputs=dict(NIGHT_INPUTS))
    actions = [c["action"] for c in calls if "action" in c]
    assert actions == ["switch.turn_on"]


# ------------------------------------------------------------- the arithmetic


@pytest.mark.parametrize(
    "label,hours_left,needed_kwh",
    [
        ("00:05", 6.3333, 24.99),
        ("03:00", 3.4166, 14.28),
        ("04:00", 2.4166, 10.20),
        ("04:30", 1.9166, 8.67),
    ],
)
def test_the_plan_matches_what_the_blueprint_computed_that_night(
    replay, label, hours_left, needed_kwh
):
    """The budget arithmetic was never in doubt, and it must stay that way:
    these are the exact figures from the traces."""
    ctx = replay(label)
    assert ctx["hours_left"] == pytest.approx(hours_left, abs=0.001)
    assert ctx["needed_kwh"] == pytest.approx(needed_kwh, abs=0.01)
    assert ctx["effective_capacity"] == pytest.approx(44.88, abs=0.01)


def test_the_night_had_enough_time_and_current_to_finish(replay):
    """At 04:30, 7.6 kWh remained and 2.4 hours of window with it. A 28 A
    ceiling covers that with room to spare - the target was missed because the
    session was cut, not because the plan was impossible."""
    ctx = replay("04:30")
    remaining_kwh = ctx["needed_kwh"]
    hours = ctx["hours_left"]
    available_kw = 28 * ctx["voltage"] / 1000
    assert remaining_kwh / hours < available_kw


def test_the_voltage_came_from_the_sensor_not_the_fallback(replay):
    """210 V was configured as the fallback; the charger reported 218-221 V.
    Using the fallback mid-session would have skewed every calculation."""
    ctx = replay("03:00")
    assert ctx["voltage_source"] == "sensor"
    assert ctx["voltage"] == pytest.approx(218.5, abs=0.1)


# ------------------------------------------------------------- the diagnosis


def test_the_verdict_at_0430_now_describes_the_real_situation(replay):
    """It said "машина не дома" while the car sat there drawing current."""
    assert "не дома" not in replay("04:30")["verdict"]


def test_the_stop_entry_would_have_shown_the_contradiction(blueprint):
    """The logbook line that night named the reason but not the evidence, so
    "car not home" looked identical whether the car had left or the tracker
    had lied. With the current and the tracker state both in the entry, the
    contradiction is visible without opening a single trace."""
    now, world = world_at("04:30")
    world[STATUS] = State("available", last_changed=now)
    world[POWER] = State(0, last_changed=now)
    world[AMPERE] = State(0, last_changed=now)
    inputs = dict(NIGHT_INPUTS)
    inputs["debug_logging"] = True
    entries = [
        c["data"]["message"]
        for c in blueprint.run_actions(world=world, now=now, inputs=inputs)
        if c.get("action") == "logbook.log"
    ]
    assert entries
    assert "трекер=not_home" in entries[0]
    assert "шло" in entries[0]


def test_the_cumulative_energy_meter_is_still_rejected(replay):
    """The configured energy sensor counts lifetime kWh (95.57 that night) and
    occasionally spikes to 5322.48. It must never be mistaken for a session
    meter - the plan ran on SoC all night, which is why the spikes were
    harmless."""
    now, _ = world_at("03:00")
    for reading in (95.57, 5322.48):
        ctx = replay(
            "03:00",
            **{"sensor.charger_session_energy": State(reading, last_changed=now)},
        )
        assert ctx["plan_source"] == "soc"
