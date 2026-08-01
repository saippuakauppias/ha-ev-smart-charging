"""Template triggers.

These fire on a false-to-true transition, so what matters is that the truth
value is correct for each situation - and that they never raise, since a
trigger that throws would silently disable the whole automation.
"""

from __future__ import annotations

import pytest
from conftest import SOC, TRACKER
from ha_sim import State, moment

DACHA = "zone.dacha"


def test_arrival_fires_in_the_default_home_zone(fire):
    assert fire("car_arrived", **{TRACKER: "home"}) is True


@pytest.mark.parametrize("state", ["not_home", "Work", "unavailable", "unknown"])
def test_arrival_does_not_fire_elsewhere(fire, state):
    assert fire("car_arrived", **{TRACKER: State(state)}) is False


@pytest.mark.parametrize("label", ["Dacha", "dacha"])
def test_arrival_fires_for_a_custom_zone(fire, label):
    assert (
        fire(
            "car_arrived",
            inputs={"home_zone": DACHA},
            **{DACHA: State(1, {"friendly_name": "Dacha"}), TRACKER: label},
        )
        is True
    )


def test_arrival_in_a_custom_zone_ignores_the_literal_home_state(fire):
    assert (
        fire(
            "car_arrived",
            inputs={"home_zone": DACHA},
            **{DACHA: State(1, {"friendly_name": "Dacha"}), TRACKER: "home"},
        )
        is False
    )


def test_arrival_never_fires_without_a_tracker(fire):
    assert fire("car_arrived", inputs={"car_tracker": []}, **{TRACKER: "home"}) is False


@pytest.mark.parametrize("soc,expected", [(99, False), (100, True), (102, True)])
def test_target_trigger_matches_the_configured_target(fire, soc, expected):
    assert fire("target_hit", **{SOC: soc}) is expected


def test_target_trigger_respects_a_lower_target(fire):
    assert fire("target_hit", inputs={"target_soc": 80}, **{SOC: 81}) is True
    assert fire("target_hit", inputs={"target_soc": 80}, **{SOC: 79}) is False


@pytest.mark.parametrize("state", ["unavailable", "unknown", "n/a"])
def test_target_trigger_ignores_unusable_readings(fire, state):
    assert fire("target_hit", **{SOC: State(state)}) is False


def test_target_trigger_is_inert_without_a_battery_sensor(fire):
    assert fire("target_hit", inputs={"car_battery_sensor": []}, **{SOC: 100}) is False


@pytest.mark.parametrize("trigger_id", ["car_arrived", "target_hit"])
def test_triggers_survive_a_completely_empty_world(blueprint, base_inputs, trigger_id):
    """Right after a restart every entity may still be missing."""
    result = blueprint.fire_trigger(
        trigger_id, world={}, now=moment(23, 0), inputs=base_inputs
    )
    assert result is False


@pytest.mark.parametrize("trigger_id", ["car_arrived", "target_hit"])
def test_triggers_survive_with_all_optional_inputs_empty(blueprint, trigger_id):
    minimal = {
        "charger_switch": "switch.charger",
        "charger_current_number": "number.charger",
        "charger_status_sensor": "sensor.charger",
    }
    result = blueprint.fire_trigger(
        trigger_id, world={}, now=moment(23, 0), inputs=minimal
    )
    assert result is False
