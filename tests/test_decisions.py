"""The start/stop decision matrix and the reason reported for stopping."""

from __future__ import annotations

import pytest
from conftest import LINK, PROBLEM, SESSION, SOC, STATUS, TRACKER, charging_since
from ha_sim import State, moment


def test_the_happy_path_charges(evaluate):
    ctx = evaluate(moment(23, 0))
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False


@pytest.mark.parametrize("status", ["available", "fault_unplugged"])
def test_an_unplugged_cable_prevents_charging(evaluate, status):
    ctx = evaluate(moment(23, 0), **{STATUS: status})
    assert ctx["plugged_in"] is False
    assert ctx["should_charge"] is False


def test_an_unknown_status_is_treated_as_plugged_in(evaluate):
    """Better to try than to sit out the whole night over a missing sensor."""
    ctx = evaluate(moment(23, 0), **{STATUS: State("unknown")})
    assert ctx["plugged_in"] is True
    assert ctx["should_charge"] is True


def test_an_unplugged_cable_does_not_trigger_a_stop_command(evaluate):
    """There is nothing to stop; sending switch.turn_off would be noise."""
    ctx = evaluate(moment(23, 0), **{STATUS: "available"})
    assert ctx["must_stop"] is False


def test_charger_reporting_completion_stops_the_session(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{STATUS: "charged", "switch.charger": charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "charger_reports_charged"


def test_a_charger_fault_stops_the_session(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{STATUS: "fault", "switch.charger": charging_since(now)})
    assert ctx["charger_fault"] is True
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "fault"


def test_the_problem_sensor_also_counts_as_a_fault(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{PROBLEM: "on", "switch.charger": charging_since(now)})
    assert ctx["charger_fault"] is True
    assert ctx["must_stop"] is True


def test_an_offline_charger_is_left_alone(evaluate):
    """No point issuing commands into the void, and no point flapping the switch."""
    now = moment(1, 0)
    ctx = evaluate(now, **{LINK: "off", "switch.charger": charging_since(now)})
    assert ctx["charger_online"] is False
    assert ctx["should_charge"] is False
    assert ctx["must_stop"] is False


def test_reaching_the_target_stops_the_session(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{SOC: 100, "switch.charger": charging_since(now)})
    assert ctx["target_reached"] is True
    assert ctx["stop_reason"] == "target_reached"
    assert ctx["must_stop"] is True


def test_a_lower_target_stops_earlier(evaluate):
    now = moment(3, 0)
    ctx = evaluate(
        now, inputs={"target_soc": 80}, **{SOC: 81, "switch.charger": charging_since(now)}
    )
    assert ctx["target_reached"] is True


def test_end_of_window_stops_by_default(evaluate):
    now = moment(9, 0)
    ctx = evaluate(now, **{"switch.charger": charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"
    assert ctx["target_missed"] is True


def test_end_of_window_can_be_allowed_to_overrun(evaluate):
    now = moment(9, 0)
    ctx = evaluate(
        now, inputs={"stop_at_window_end": False}, **{"switch.charger": charging_since(now)}
    )
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False


def test_overrun_never_starts_a_new_session_outside_the_window(evaluate):
    ctx = evaluate(moment(9, 0), inputs={"stop_at_window_end": False})
    assert ctx["should_charge"] is False


def test_a_manual_session_survives_the_end_of_the_window(evaluate):
    """With the session flag wired up, only our own sessions get cut off."""
    now = moment(9, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "off", "switch.charger": charging_since(now)},
    )
    assert ctx["session_owned"] is False
    assert ctx["must_stop"] is False


def test_our_own_session_is_cut_off_at_the_end_of_the_window(evaluate):
    now = moment(9, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "on", "switch.charger": charging_since(now)},
    )
    assert ctx["session_owned"] is True
    assert ctx["must_stop"] is True


def test_safety_stops_ignore_the_session_flag(evaluate):
    """A departing car is cut off whoever started the session."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "off", TRACKER: "not_home", "switch.charger": charging_since(now)},
    )
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "car_not_home"


def test_emergency_charging_ignores_the_window(evaluate):
    ctx = evaluate(moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12})
    assert ctx["in_window"] is False
    assert ctx["should_charge"] is True


def test_emergency_charging_still_requires_the_car_to_be_home(evaluate):
    ctx = evaluate(
        moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12, TRACKER: "not_home"}
    )
    assert ctx["should_charge"] is False


def test_emergency_charging_still_requires_a_plugged_cable(evaluate):
    ctx = evaluate(
        moment(14, 0), inputs={"emergency_soc": 20}, **{SOC: 12, STATUS: "available"}
    )
    assert ctx["should_charge"] is False


def test_emergency_is_off_by_default(evaluate):
    ctx = evaluate(moment(14, 0), **{SOC: 5})
    assert ctx["emergency"] is False
    assert ctx["should_charge"] is False


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({STATUS: "fault"}, "fault"),
        ({SOC: 100}, "target_reached"),
        ({STATUS: "charged"}, "charger_reports_charged"),
        ({TRACKER: "not_home"}, "car_not_home"),
        ({STATUS: "available"}, "unplugged"),
    ],
)
def test_stop_reasons_are_reported_in_priority_order(evaluate, overrides, expected):
    now = moment(3, 0)
    overrides = dict(overrides)
    overrides["switch.charger"] = charging_since(now)
    ctx = evaluate(now, **overrides)
    assert ctx["stop_reason"] == expected
