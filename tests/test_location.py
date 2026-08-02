"""Zone handling: only charge our own car, but never cut a running session
because a GPS receiver had a bad minute.
"""

from __future__ import annotations

import pytest
from conftest import AMPERE, POWER, STATUS, SWITCH, TRACKER, charging_since
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
    """A car that drove away took the cable with it, so the current stopped
    too. That combination - away and drawing nothing - is a real departure."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{
            TRACKER: "not_home",
            POWER: 0,
            AMPERE: 0,
            STATUS: "available",
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["physically_present"] is False
    assert ctx["car_left"] is True
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "car_not_home"


def test_a_lying_tracker_does_not_interrupt_a_running_session(evaluate):
    """The failure that cost a full charge on the first real night.

    At 04:30 the tracker reported ``not_home`` while the car sat in the
    driveway drawing 17.6 A. That is not a departure - a car cannot drive off
    with the cable attached - but the automation believed the tracker and cut
    the power. Reporting a wrong zone is the common GPS failure; going
    ``unavailable`` (the only case handled before) is the rare one.
    """
    now = moment(4, 30)
    ctx = evaluate(
        now,
        **{
            TRACKER: "not_home",
            STATUS: "charging",
            POWER: 3890,
            AMPERE: 17.6,
            SWITCH: charging_since(now, 265),
        },
    )
    assert ctx["location_known"] is True, "the tracker answered, it just lied"
    assert ctx["car_home"] is False
    assert ctx["physically_present"] is True
    assert ctx["car_left"] is False
    assert ctx["must_stop"] is False
    assert ctx["should_charge"] is True


def test_a_lying_tracker_still_cannot_start_a_session(evaluate):
    """Trust in the current only protects a session already running: with the
    switch off there is no current to vouch for the car being there."""
    ctx = evaluate(moment(23, 0), **{TRACKER: "not_home", STATUS: "plugged_in"})
    assert ctx["physically_present"] is False
    assert ctx["should_charge"] is False


def test_a_charger_stuck_on_charging_without_current_is_not_proof(evaluate):
    """The status alone can freeze; only real power counts as evidence."""
    now = moment(4, 30)
    ctx = evaluate(
        now,
        **{
            TRACKER: "not_home",
            STATUS: "charging",
            POWER: 0,
            AMPERE: 0,
            SWITCH: charging_since(now, 265),
        },
    )
    assert ctx["physically_present"] is False
    assert ctx["car_left"] is True
    assert ctx["must_stop"] is True


def test_without_a_power_sensor_the_status_vouches_for_the_car(evaluate):
    """Not every install has a power sensor; the status is the fallback."""
    now = moment(4, 30)
    ctx = evaluate(
        now,
        inputs={"charger_power_sensor": []},
        **{TRACKER: "not_home", STATUS: "charging", SWITCH: charging_since(now, 265)},
    )
    assert ctx["physically_present"] is True
    assert ctx["must_stop"] is False


def test_a_tracker_that_lies_at_window_start_only_delays_the_session(evaluate):
    """The start check is not a one-shot at 00:05: every recalculation retries
    it, so a tracker that comes to its senses at 02:00 starts the session then.
    Without this a single bad GPS minute at the wrong moment would cost the
    whole night."""
    blocked = evaluate(moment(0, 5), **{TRACKER: "not_home", STATUS: "plugged_in"})
    assert blocked["should_charge"] is False

    later = evaluate(moment(2, 0), **{TRACKER: "home", STATUS: "plugged_in"})
    assert later["should_charge"] is True


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


def test_zone_without_a_friendly_name_falls_back_to_the_object_id(evaluate):
    """A nameless zone must not lock the user out of charging forever.

    Trackers report the zone's friendly name, so a zone missing one would never
    match and ``car_home`` would be false for good. Falling back to the object
    id (``zone.dacha`` -> ``dacha``) keeps the common case working.
    """
    ctx = evaluate(
        moment(1, 0),
        inputs={"home_zone": DACHA},
        **{DACHA: State(1, {}), TRACKER: "Dacha"},
    )
    assert ctx["zone_name"] == "dacha"
    assert ctx["car_home"] is True


def test_a_nameless_zone_still_rejects_a_car_that_is_elsewhere(evaluate):
    ctx = evaluate(
        moment(1, 0),
        inputs={"home_zone": DACHA},
        **{DACHA: State(1, {}), TRACKER: "not_home"},
    )
    assert ctx["car_home"] is False
    assert ctx["should_charge"] is False
