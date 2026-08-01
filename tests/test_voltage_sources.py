"""Where the voltage used in the current calculation comes from.

Priority: live sensor, then derived from power and actual current, and only
then the manually configured fallback.
"""

from __future__ import annotations

import pytest
from conftest import AMPERE, POWER, VOLTAGE
from ha_sim import State, moment


def test_live_sensor_wins_when_it_reads_plausibly(evaluate):
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 207.0})
    assert ctx["voltage_source"] == "sensor"
    assert ctx["voltage"] == 207.0


def test_derived_voltage_is_used_when_no_voltage_sensor_exists(evaluate):
    """3790 W drawn at 18.3 A on one phase is about 207 V."""
    ctx = evaluate(moment(23, 0), inputs={"charger_voltage_sensor": []})
    assert ctx["voltage_source"] == "derived"
    assert 206 < ctx["voltage"] < 208


def test_idle_voltage_sensor_falls_through_to_the_derived_value(evaluate):
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 0})
    assert ctx["voltage_source"] == "derived"
    assert 206 < ctx["voltage"] < 208


def test_cold_start_uses_the_configured_fallback(evaluate):
    """At the moment charging is decided the charger is not energised yet."""
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 0, POWER: 0, AMPERE: 0})
    assert ctx["voltage_source"] == "fallback"
    assert ctx["voltage"] == 230


def test_fallback_is_used_when_no_electrical_sensors_are_configured(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={
            "charger_voltage_sensor": [],
            "charger_power_sensor": [],
            "charger_current_sensor": [],
        },
    )
    assert ctx["voltage_source"] == "fallback"
    assert ctx["voltage"] == 230


@pytest.mark.parametrize("reading", [174.9, 150, 100, 12, 0])
def test_readings_below_the_cutoff_are_rejected(evaluate, reading):
    """Below 175 V a charger should have tripped, so the number is not real."""
    ctx = evaluate(
        moment(23, 0),
        inputs={"charger_power_sensor": [], "charger_current_sensor": []},
        **{VOLTAGE: reading},
    )
    assert ctx["voltage_source"] == "fallback"


def test_reading_just_above_the_cutoff_is_accepted(evaluate):
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 175.0})
    assert ctx["voltage_source"] == "sensor"
    assert ctx["voltage"] == 175.0


@pytest.mark.parametrize("power,current", [(900, 10), (4000, 10), (100, 0.2)])
def test_implausible_derived_values_are_discarded(evaluate, power, current):
    """90 V and 400 V are arithmetic, not electricity."""
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 0, POWER: power, AMPERE: current})
    assert ctx["voltage_source"] == "fallback"


def test_three_phase_derivation_divides_by_the_phase_count(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"phases": "3"},
        **{VOLTAGE: 0, POWER: 11000, AMPERE: 17},
    )
    assert ctx["voltage_source"] == "derived"
    assert 215 < ctx["voltage"] < 216


def test_unavailable_current_sensor_blocks_derivation(evaluate):
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 0, AMPERE: State("unavailable")})
    assert ctx["voltage_source"] == "fallback"


def test_zero_current_never_divides_by_zero(evaluate):
    ctx = evaluate(moment(23, 0), **{VOLTAGE: 0, AMPERE: 0, POWER: 3790})
    assert ctx["derived_voltage"] == 0
    assert ctx["voltage_source"] == "fallback"


def test_lower_voltage_demands_more_current_for_the_same_energy(evaluate):
    high = evaluate(moment(23, 0), **{VOLTAGE: 230})
    low = evaluate(moment(23, 0), **{VOLTAGE: 190})
    assert low["calc_current"] > high["calc_current"]


def test_voltage_never_gates_the_decision_to_charge(evaluate):
    """Whatever the voltage says, starting and stopping is decided elsewhere."""
    for reading in (0, 150, 175, 207, 253):
        ctx = evaluate(moment(1, 0), **{VOLTAGE: reading})
        assert ctx["should_charge"] is True
