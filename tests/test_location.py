"""Zone handling: only charge our own car, but never cut a running session
because a GPS receiver had a bad minute.
"""

from __future__ import annotations

import pytest
from conftest import TRACKER, charging_since
from ha_sim import State, moment

DACHA = "zone.dacha"


def test_car_in_the_default_home_zone_is_recognised(evaluate):
    ctx = evaluate(moment(1, 0), **{TRACKER: "home"})
    assert ctx["car_home"] is True
    assert ctx["allow_start"] is True


@pytest.mark.parametrize("state", ["not_home", "Work", "Gym"])
def test_car_elsewhere_blocks_the_start(evaluate, state):
    ctx = evaluate(moment(1, 0), **{TRACKER: state})
    assert ctx["car_home"] is False
    assert ctx["should_charge"] is False


@pytest.mark.parametrize("label", ["Dacha", "dacha", "DACHA"])
def test_custom_zone_matches_by_name_ignoring_case(evaluate, label):
    ctx = evaluate(
        moment(1, 0),
        inputs={"home_zone": DACHA},
        **{DACHA: State(1, {"friendly_name": "Dacha"}), TRACKER: label},
    )
    assert ctx["car_home"] is True


def test_custom_zone_does_not_match_the_literal_home_state(evaluate):
    ctx = evaluate(
        moment(1, 0),
        inputs={"home_zone": DACHA},
        **{DACHA: State(1, {"friendly_name": "Dacha"}), TRACKER: "home"},
    )
    assert ctx["car_home"] is False


def test_zone_check_can_be_switched_off(evaluate):
    ctx = evaluate(moment(1, 0), inputs={"require_home": False}, **{TRACKER: "not_home"})
    assert ctx["allow_start"] is True
    assert ctx["should_charge"] is True


def test_no_tracker_configured_means_no_restriction(evaluate):
    ctx = evaluate(moment(1, 0), inputs={"car_tracker": []})
    assert ctx["allow_start"] is True
    assert ctx["should_charge"] is True


# ------------------------------------------------------- GPS loss vs. driving


@pytest.mark.parametrize("state", ["unavailable", "unknown", "none"])
def test_unknown_position_blocks_a_new_session(evaluate, state):
    ctx = evaluate(moment(1, 0), **{TRACKER: State(state)})
    assert ctx["location_known"] is False
    assert ctx["allow_start"] is False
    assert ctx["should_charge"] is False


def test_unknown_position_does_not_interrupt_a_running_session(evaluate):
    """A silent tracker is not the same thing as a departing car."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{TRACKER: State("unavailable"), "switch.charger": charging_since(now)},
    )
    assert ctx["allow_continue"] is True
    assert ctx["should_charge"] is True
    assert ctx["must_stop"] is False


def test_a_car_that_actually_left_is_cut_off(evaluate):
    now = moment(3, 0)
    ctx = evaluate(now, **{TRACKER: "not_home", "switch.charger": charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "car_not_home"


def test_arriving_mid_window_starts_charging_immediately(evaluate):
    """No waiting for the next night if the car was late."""
    absent = evaluate(moment(23, 0), **{TRACKER: "not_home"})
    assert absent["should_charge"] is False

    arrived = evaluate(moment(1, 0), **{TRACKER: "home"})
    assert arrived["in_window"] is True
    assert arrived["should_charge"] is True


def test_arriving_outside_the_window_does_not_start_charging(evaluate):
    ctx = evaluate(moment(14, 0), **{TRACKER: "home"})
    assert ctx["car_home"] is True
    assert ctx["should_charge"] is False


def test_zone_without_a_friendly_name_falls_back_gracefully(evaluate):
    ctx = evaluate(
        moment(1, 0),
        inputs={"home_zone": DACHA},
        **{DACHA: State(1, {}), TRACKER: "Dacha"},
    )
    assert ctx["car_home"] is False
    assert ctx["should_charge"] is False
