"""End-to-end simulation of a whole charging night.

Every other test file checks one variable in isolation. This one runs the
regulator in a closed loop: the current it picks determines how fast the battery
fills, which determines the current it picks on the next tick. Bugs that only
show up as oscillation, runaway current or a charge that never finishes are
caught here and nowhere else.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytest
from conftest import (
    AMPERE,
    HOME,
    LINK,
    NUMBER,
    POWER,
    PROBLEM,
    SOC,
    STATUS,
    SWITCH,
    TRACKER,
    VOLTAGE,
)
from ha_sim import State, moment

TICK = dt.timedelta(minutes=30)
#: Chargers deliver slightly less than the setpoint asks for.
DELIVERY_RATIO = 0.915


@dataclass
class NightLog:
    setpoints: list[float] = field(default_factory=list)
    writes: int = 0
    soc_trace: list[float] = field(default_factory=list)
    switch_commands: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    @property
    def final_soc(self) -> float:
        return self.soc_trace[-1]

    @property
    def largest_jump(self) -> float:
        pairs = zip(self.setpoints, self.setpoints[1:], strict=False)
        return max((abs(b - a) for a, b in pairs), default=0.0)


def simulate_night(
    blueprint,
    inputs: dict,
    *,
    start_soc: float = 20.0,
    capacity: float = 43.0,
    mains_voltage: float = 207.0,
    real_efficiency: float = 0.88,
    start: dt.datetime | None = None,
    ticks: int = 17,
    soc_dies_after: int | None = None,
) -> NightLog:
    """Run the regulator against a simple battery model, tick by tick."""
    now = start or moment(23, 0, day=31, month=7)
    soc = start_soc
    setpoint_value = 6.0
    switch_on = False
    soc_changed_at = now
    setpoint_changed_at = now - dt.timedelta(hours=5)
    log = NightLog()

    for index in range(ticks):
        soc_alive = soc_dies_after is None or index < soc_dies_after
        delivered_current = setpoint_value * DELIVERY_RATIO if switch_on else 0.0
        world = {
            SWITCH: State("on" if switch_on else "off",
                          last_changed=now - dt.timedelta(hours=1)),
            NUMBER: State(round(setpoint_value, 2),
                          {"min": 6.0, "max": 32.0, "step": 1.0},
                          setpoint_changed_at),
            STATUS: State("charging" if switch_on else "plugged_in", last_changed=now),
            POWER: State(round(delivered_current * mains_voltage, 1), last_changed=now),
            AMPERE: State(round(delivered_current, 2), last_changed=now),
            VOLTAGE: State(mains_voltage if switch_on else 0.0, last_changed=now),
            LINK: State("on", last_changed=now),
            PROBLEM: State("off", last_changed=now),
            SOC: State(round(soc, 1) if soc_alive else "unavailable",
                       last_changed=soc_changed_at),
            TRACKER: State("home", last_changed=now),
            HOME: State(2, {"friendly_name": "Home"}, now),
        }

        ctx = blueprint.evaluate(world=world, now=now, inputs=inputs)
        log.soc_trace.append(soc)

        if ctx["should_charge"]:
            if ctx["needs_write"]:
                setpoint_value = ctx["desired_current"]
                setpoint_changed_at = now
                log.writes += 1
            if not switch_on:
                switch_on = True
                log.switch_commands.append("on")
            log.setpoints.append(setpoint_value)
        elif ctx["must_stop"]:
            if switch_on:
                switch_on = False
                log.switch_commands.append("off")
                log.stop_reason = ctx["stop_reason"]

        # Advance the battery model by one tick.
        if switch_on:
            kwh = (
                setpoint_value * DELIVERY_RATIO * mains_voltage
                * (TICK.total_seconds() / 3600) / 1000 * real_efficiency
            )
            gained = kwh / capacity * 100
            if gained > 0.05:
                soc = min(100.0, soc + gained)
                soc_changed_at = now + TICK
        now += TICK

    log.soc_trace.append(soc)
    return log


@pytest.fixture
def night_inputs(base_inputs):
    inputs = dict(base_inputs)
    inputs.update(
        {
            "start_time": "23:00:00",
            "stop_time": "07:00:00",
            "target_soc": 100,
            "min_current": 6,
            "max_current": 28,
            "current_step": 1,
            "time_reserve_minutes": 30,
            "current_deadband": 3,
            "command_gap": 60,
            "efficiency": 88,
            "battery_capacity": 43,
        }
    )
    return inputs


# --------------------------------------------------------------- happy paths


def test_a_normal_night_reaches_the_target(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=20)
    assert log.final_soc >= 99.0, f"only reached {log.final_soc:.1f}%"


def test_current_stays_inside_the_configured_bounds_all_night(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=20)
    assert log.setpoints, "the charger was never switched on"
    assert min(log.setpoints) >= 6
    assert max(log.setpoints) <= 28


def test_the_regulator_does_not_slam_the_maximum_from_the_start(blueprint, night_inputs):
    """The whole point of the blueprint: spread the load, do not sprint."""
    log = simulate_night(blueprint, night_inputs, start_soc=50)
    assert log.setpoints[0] < 28


def test_the_setpoint_is_rewritten_only_a_handful_of_times(blueprint, night_inputs):
    """Sixteen ticks must not mean sixteen commands to the charger."""
    log = simulate_night(blueprint, night_inputs, start_soc=20)
    assert log.writes <= 6, f"{log.writes} writes over one night"


def test_the_current_never_oscillates_wildly(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=20)
    assert log.largest_jump <= 12, f"largest jump was {log.largest_jump} A"


def test_the_charger_is_switched_on_exactly_once(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=20)
    assert log.switch_commands.count("on") == 1


def test_charging_stops_once_the_target_is_reached(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=85)
    assert "off" in log.switch_commands
    assert log.stop_reason == "target_reached"


def test_a_nearly_full_battery_charges_gently(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=90)
    assert max(log.setpoints) <= 12


def test_a_partial_target_finishes_early(blueprint, night_inputs):
    inputs = dict(night_inputs, target_soc=80)
    log = simulate_night(blueprint, inputs, start_soc=40)
    assert log.stop_reason == "target_reached"
    assert 80 <= log.final_soc < 90


# ------------------------------------------------------------ adverse nights


def test_a_cold_night_recovers_almost_all_of_the_shortfall(blueprint, night_inputs):
    """Real efficiency ten points worse than configured.

    Flat out for the entire window would yield about 97 %, and the regulator
    needs a couple of ticks to notice it is behind, so the ceiling here is
    roughly 95 %. What matters is that it detects the lag and ends up pinned
    at the maximum rather than politely under-delivering.
    """
    log = simulate_night(blueprint, night_inputs, start_soc=20, real_efficiency=0.78)
    assert log.final_soc >= 94.0, f"only reached {log.final_soc:.1f}%"
    assert log.setpoints[-1] == 28, "the regulator never went to full current"
    assert log.setpoints[0] < 28, "it should not have started at full current"


def test_a_weak_mains_supply_still_reaches_the_target(blueprint, night_inputs):
    log = simulate_night(blueprint, night_inputs, start_soc=25, mains_voltage=195.0)
    assert log.final_soc >= 97.0, f"only reached {log.final_soc:.1f}%"


def test_a_wrong_capacity_setting_is_absorbed_by_the_feedback_loop(
    blueprint, night_inputs
):
    """Configured 43 kWh, actual pack 50 kWh: the plan must still converge."""
    log = simulate_night(blueprint, night_inputs, start_soc=30, capacity=50.0)
    assert log.final_soc >= 95.0, f"only reached {log.final_soc:.1f}%"


def test_losing_the_car_connection_mid_night_keeps_charging(blueprint, night_inputs):
    """The single most important promise of the blueprint."""
    log = simulate_night(blueprint, night_inputs, start_soc=30, soc_dies_after=6)
    # It may only stop for the ordinary reason: the window closing.
    assert log.stop_reason == "window_end"
    assert log.switch_commands == ["on", "off"]
    # And it kept adding charge after the data went away.
    assert log.final_soc > log.soc_trace[6] + 10


def test_after_losing_the_connection_the_current_settles_on_the_fallback(
    blueprint, night_inputs
):
    log = simulate_night(blueprint, night_inputs, start_soc=30, soc_dies_after=4)
    assert log.setpoints[-1] == 10


def test_a_short_window_pushes_the_current_up(blueprint, night_inputs):
    short = dict(night_inputs, start_time="03:00:00", stop_time="07:00:00")
    log = simulate_night(
        blueprint, short, start_soc=40, start=moment(3, 0, day=1, month=8), ticks=9
    )
    assert max(log.setpoints) >= 20


def test_an_impossible_night_charges_flat_out_and_reports_the_shortfall(
    blueprint, night_inputs
):
    """Four hours cannot fill an empty pack; the regulator must not give up."""
    short = dict(night_inputs, start_time="03:00:00", stop_time="07:00:00")
    log = simulate_night(
        blueprint, short, start_soc=5, start=moment(3, 0, day=1, month=8), ticks=9
    )
    assert max(log.setpoints) == 28
    assert log.final_soc < 100


# ------------------------------------------------------- minimal wiring works


def test_the_blueprint_runs_with_only_the_three_required_inputs(blueprint):
    """No car integration, no power meter, no tracker: it must still charge."""
    minimal = {
        "charger_switch": SWITCH,
        "charger_current_number": NUMBER,
        "charger_status_sensor": STATUS,
    }
    now = moment(23, 0)
    world = {
        SWITCH: State("off", last_changed=now),
        NUMBER: State(10, {"min": 6.0, "max": 32.0, "step": 1.0}, now),
        STATUS: State("plugged_in", last_changed=now),
    }
    ctx = blueprint.evaluate(world=world, now=now, inputs=minimal)
    assert ctx["plan_source"] == "none"
    assert ctx["should_charge"] is True
    assert ctx["desired_current"] == 10
    assert ctx["alarm_reason"] == "none"


def test_the_blueprint_survives_a_completely_empty_world(blueprint):
    """Right after a Home Assistant restart nothing has a state yet."""
    minimal = {
        "charger_switch": SWITCH,
        "charger_current_number": NUMBER,
        "charger_status_sensor": STATUS,
    }
    ctx = blueprint.evaluate(world={}, now=moment(23, 0), inputs=minimal)
    assert ctx["must_stop"] is False
    assert ctx["num_min"] <= ctx["desired_current"] <= ctx["num_max"]
