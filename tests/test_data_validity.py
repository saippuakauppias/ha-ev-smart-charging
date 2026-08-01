"""Trusting - or not trusting - the reported state of charge.

Three checks run in a fixed order: is the entity available at all, are the data
recent, and has the percentage frozen while current is demonstrably flowing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import ENERGY, PING, POWER, SOC, SOH, STATUS, charging_since
from ha_sim import State, moment


def test_healthy_data_is_trusted(evaluate):
    ctx = evaluate(moment(1, 0), **{"switch.charger": charging_since(moment(1, 0))})
    assert ctx["soc_valid"] is True
    assert ctx["soc_problem"] == "none"
    assert ctx["plan_source"] == "soc"


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "error", "none", ""])
def test_unusable_states_are_detected_first(evaluate, bad):
    """Most often this means the vehicle integration lost its authorisation."""
    now = moment(1, 0)
    ctx = evaluate(now, **{SOC: State(bad), "switch.charger": charging_since(now)})
    assert ctx["soc_present"] is False
    assert ctx["soc_problem"] == "unavailable"
    assert ctx["soc_valid"] is False
    assert "check_integration_auth" in ctx["alarm_reason"]


def test_availability_outranks_the_freeze_detector(evaluate):
    """An unavailable entity must be reported as such, not as frozen data."""
    now = moment(1, 0)
    ctx = evaluate(
        now,
        **{
            SOC: State("unavailable", last_changed=now - dt.timedelta(hours=4)),
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["soc_problem"] == "unavailable"
    assert ctx["soc_frozen"] is False


def test_stale_ping_invalidates_the_percentage(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"car_stale_sensor": PING, "car_stale_max": 1800},
        **{PING: 5400, "switch.charger": charging_since(now)},
    )
    assert ctx["soc_fresh"] is False
    assert ctx["soc_problem"] == "stale"


def test_fresh_ping_keeps_the_percentage(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"car_stale_sensor": PING},
        **{PING: 6.614, "switch.charger": charging_since(now)},
    )
    assert ctx["soc_valid"] is True


@pytest.mark.parametrize("reading", ["unknown", "unavailable", "n/a", ""])
def test_a_broken_ping_sensor_does_not_condemn_the_percentage(evaluate, reading):
    """An empty field upstream must not knock the regulator onto fallback current."""
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"car_stale_sensor": PING},
        **{PING: State(reading), "switch.charger": charging_since(now)},
    )
    assert ctx["soc_fresh"] is True
    assert ctx["soc_valid"] is True


def test_frozen_percentage_is_detected_while_current_flows(evaluate):
    """Cloud APIs happily serve a cached value forever after the car goes quiet."""
    now = moment(1, 0)
    ctx = evaluate(
        now,
        **{
            SOC: State(35, last_changed=now - dt.timedelta(minutes=200)),
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["soc_frozen"] is True
    assert ctx["soc_problem"] == "frozen"
    assert ctx["plan_source"] == "none"


def test_a_slow_but_moving_percentage_is_not_frozen(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        **{
            SOC: State(35, last_changed=now - dt.timedelta(minutes=45)),
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["soc_frozen"] is False


def test_a_parked_car_is_not_frozen_data(evaluate):
    """With the charger off, a static percentage is entirely expected."""
    now = moment(1, 0)
    ctx = evaluate(now, **{SOC: State(35, last_changed=now - dt.timedelta(hours=9))})
    assert ctx["soc_frozen"] is False
    assert ctx["soc_valid"] is True


def test_no_power_means_no_freeze_verdict(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        **{
            POWER: 0,
            STATUS: "paused",
            SOC: State(35, last_changed=now - dt.timedelta(minutes=200)),
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["charging_now"] is False
    assert ctx["soc_frozen"] is False


def test_freeze_detection_falls_back_to_the_status_sensor(evaluate):
    """Without a power sensor the charger status still tells us current is flowing."""
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"charger_power_sensor": []},
        **{
            STATUS: "charging",
            SOC: State(35, last_changed=now - dt.timedelta(minutes=200)),
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["charging_now"] is True
    assert ctx["soc_frozen"] is True


def test_freeze_detector_can_be_disabled(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"soc_freeze_minutes": 0},
        **{
            SOC: State(35, last_changed=now - dt.timedelta(minutes=400)),
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["soc_frozen"] is False


def test_data_alarms_stay_quiet_while_the_charger_is_off(evaluate):
    """Otherwise a car parked elsewhere would page the owner all day."""
    ctx = evaluate(moment(14, 0), **{SOC: State("unavailable")})
    assert ctx["soc_problem"] == "unavailable"
    assert ctx["alarm_reason"] == "none"


def test_charging_continues_even_when_every_check_fails(evaluate):
    """The core promise: bad data must never stop an in-progress charge."""
    now = moment(1, 0)
    ctx = evaluate(
        now, **{SOC: State("unavailable"), "switch.charger": charging_since(now)}
    )
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False
    assert ctx["desired_current"] == 10


# --------------------------------------------------------------- plan source


def test_energy_plan_takes_over_when_the_percentage_dies(evaluate):
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"session_energy_sensor": ENERGY, "session_energy_target": 20},
        **{SOC: State("unavailable"), ENERGY: 5.0},
    )
    assert ctx["plan_source"] == "energy"
    assert ctx["needed_kwh"] == pytest.approx(15.0)


def test_percentage_outranks_the_energy_plan(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"session_energy_sensor": ENERGY, "session_energy_target": 20},
        **{ENERGY: 0.0},
    )
    assert ctx["plan_source"] == "soc"


def test_energy_target_reached_stops_the_session(evaluate):
    ctx = evaluate(
        moment(2, 0),
        inputs={"session_energy_sensor": ENERGY, "session_energy_target": 20},
        **{SOC: State("unavailable"), ENERGY: 20.0, "switch.charger": charging_since(moment(2, 0))},
    )
    assert ctx["target_reached"] is True
    assert ctx["must_stop"] is True


def test_energy_plan_needs_a_target_to_activate(evaluate):
    ctx = evaluate(
        moment(23, 0),
        inputs={"session_energy_sensor": ENERGY},
        **{SOC: State("unavailable"), ENERGY: 5.0},
    )
    assert ctx["plan_source"] == "none"


# ---------------------------------------------------------- capacity and soh


def test_capacity_is_used_as_is_without_a_health_sensor(evaluate):
    ctx = evaluate(moment(23, 0))
    assert ctx["soh_pct"] == 100
    assert ctx["effective_capacity"] == 43.0


def test_health_sensor_scales_the_usable_capacity(evaluate):
    ctx = evaluate(moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: 88})
    assert ctx["effective_capacity"] == pytest.approx(37.84)


@pytest.mark.parametrize("reading,expected", [(9999, 130), (3, 50), (-5, 50)])
def test_absurd_health_readings_are_clamped(evaluate, reading, expected):
    ctx = evaluate(moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: reading})
    assert ctx["soh_pct"] == expected


def test_unavailable_health_sensor_leaves_capacity_untouched(evaluate):
    ctx = evaluate(
        moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: State("unavailable")}
    )
    assert ctx["soh_pct"] == 100
