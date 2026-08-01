"""Shared fixtures: the blueprint under test and a configurable fake world."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from ha_sim import Blueprint, State, moment

BLUEPRINT_PATH = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "automation"
    / "ev_smart_charging"
    / "ev_smart_night_charging.yaml"
)

# Entity ids used throughout the suite. Deliberately short and neutral so that
# no test depends on a particular integration's naming.
SWITCH = "switch.charger"
NUMBER = "number.charger_set_current"
STATUS = "sensor.charger_status"
POWER = "sensor.charger_power"
AMPERE = "sensor.charger_current"
VOLTAGE = "sensor.charger_voltage"
ENERGY = "sensor.charger_session_energy"
LINK = "binary_sensor.charger_online"
PROBLEM = "binary_sensor.charger_problem"
MODE = "select.charger_mode"
SOC = "sensor.car_battery"
PING = "sensor.car_last_ping"
SOH = "sensor.car_battery_soh"
OUTSIDE = "sensor.outside_temperature"
CAR_CHARGING = "binary_sensor.car_charging"
TRACKER = "device_tracker.car"
HOME = "zone.home"
SESSION = "input_boolean.charging_session"

#: A configuration that wires up every optional entity. Individual tests narrow
#: it down by passing ``inputs={...}`` overrides.
FULL_INPUTS: dict[str, Any] = {
    "charger_switch": SWITCH,
    "charger_current_number": NUMBER,
    "charger_status_sensor": STATUS,
    "charger_power_sensor": POWER,
    "charger_current_sensor": AMPERE,
    "charger_voltage_sensor": VOLTAGE,
    "charger_mode_select": MODE,
    "charger_link_sensor": LINK,
    "charger_problem_sensor": PROBLEM,
    "car_battery_sensor": SOC,
    "car_tracker": TRACKER,
    "home_zone": HOME,
    "battery_capacity": 43,
    "min_current": 6,
    "max_current": 28,
    "nominal_voltage": 230,
    "target_soc": 100,
}


@pytest.fixture(scope="session")
def blueprint() -> Blueprint:
    return Blueprint.load(BLUEPRINT_PATH)


@pytest.fixture
def base_inputs() -> dict[str, Any]:
    return dict(FULL_INPUTS)


def build_world(now: dt.datetime, **overrides: Any) -> dict[str, State]:
    """A healthy world: car at home, cable plugged in, charging in progress.

    Values are chosen to be internally consistent: 3790 W at 18.3 A works out to
    roughly 207 V, which matches the voltage sensor.
    """
    old = now - dt.timedelta(hours=5)
    world: dict[str, State] = {
        SWITCH: State("off", last_changed=old),
        NUMBER: State(20, {"min": 6.0, "max": 32.0, "step": 1.0}, old),
        STATUS: State("charging", last_changed=old),
        POWER: State(3790, last_changed=old),
        AMPERE: State(18.3, last_changed=old),
        VOLTAGE: State(207.0, last_changed=old),
        LINK: State("on", last_changed=old),
        PROBLEM: State("off", last_changed=old),
        MODE: State("immediate", last_changed=old),
        SOC: State(35, last_changed=now - dt.timedelta(minutes=10)),
        TRACKER: State("home", last_changed=old),
        HOME: State(2, {"friendly_name": "Home", "radius": 100}, old),
    }
    for entity_id, value in overrides.items():
        if value is None:
            world.pop(entity_id, None)
        elif isinstance(value, State):
            # Home Assistant always populates last_changed; mirror that so tests
            # cannot accidentally construct an impossible state object.
            if value.last_changed is None:
                value.last_changed = now
            world[entity_id] = value
        else:
            world[entity_id] = State(value, last_changed=now)
    return world


@pytest.fixture
def world_at():
    """Factory fixture: ``world_at(moment(23, 0), **overrides)``."""
    return build_world


@pytest.fixture
def evaluate(blueprint, base_inputs):
    """Factory that renders the blueprint variables for a given situation."""

    def _evaluate(
        now: dt.datetime | None = None,
        *,
        inputs: dict[str, Any] | None = None,
        world: dict[str, State] | None = None,
        **world_overrides: Any,
    ) -> dict[str, Any]:
        now = now or moment(23, 0)
        merged_inputs = dict(base_inputs)
        merged_inputs.update(inputs or {})
        merged_world = world if world is not None else build_world(now, **world_overrides)
        return blueprint.evaluate(world=merged_world, now=now, inputs=merged_inputs)

    return _evaluate


@pytest.fixture
def fire(blueprint, base_inputs):
    """Factory that evaluates a template trigger."""

    def _fire(
        trigger_id: str,
        now: dt.datetime | None = None,
        *,
        inputs: dict[str, Any] | None = None,
        **world_overrides: Any,
    ) -> Any:
        now = now or moment(23, 0)
        merged_inputs = dict(base_inputs)
        merged_inputs.update(inputs or {})
        world = build_world(now, **world_overrides)
        return blueprint.fire_trigger(
            trigger_id, world=world, now=now, inputs=merged_inputs
        )

    return _fire


def charging_since(now: dt.datetime, minutes: float = 120) -> State:
    """A switch that has been on for a while."""
    return State("on", last_changed=now - dt.timedelta(minutes=minutes))


def setpoint(value: float, now: dt.datetime, *, low: float = 6.0, high: float = 32.0,
             step: float = 1.0, age_seconds: float = 7200) -> State:
    """A ``number`` entity holding the current setpoint."""
    return State(
        value,
        {"min": low, "max": high, "step": step},
        now - dt.timedelta(seconds=age_seconds),
    )
