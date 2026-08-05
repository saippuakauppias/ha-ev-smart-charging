"""Trusting - or not trusting - the reported state of charge.

Three checks run in a fixed order: is the entity available at all, are the data
recent, and has the percentage frozen while current is demonstrably flowing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import (
    ENERGY,
    NUMBER,
    PING,
    POWER,
    SOC,
    SOH,
    STATUS,
    charging_since,
    setpoint,
)
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


@pytest.mark.parametrize(
    ("watts", "flowing"), [(0, False), (150, False), (199, False), (250, True)]
)
def test_standby_draw_is_not_mistaken_for_charging(evaluate, watts, flowing):
    """The threshold exists because an idle station is not a silent one.

    Electronics, a display and a contactor coil draw tens of watts with no car
    charging at all. Comparing against zero would read that as current flowing,
    which in turn feeds "the car is physically here" and the frozen-data
    detector — both of which then answer confidently on the strength of a
    standby light.
    """
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"no_power_threshold": 200},
        **{
            POWER: watts,
            STATUS: "paused",
            "switch.charger": charging_since(now),
        },
    )
    assert ctx["charging_now"] is flowing


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
    assert ctx["plan_source"] == "none"


def test_losing_the_data_mid_session_does_not_cut_the_current(evaluate):
    """The reserve current answers "what do we start at", not "what do we drop
    to". Halving a current that was already behind schedule makes the shortfall
    worse, and the percentage then climbs slower still - the same shape of
    defect that cost the first real night its last seventeen points.
    """
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"fallback_current": 10},
        **{SOC: State("unavailable"),
           "switch.charger": charging_since(now),
           NUMBER: setpoint(22, now)},
    )
    assert ctx["desired_current"] == 22, "the running current is held, not cut"


def test_the_reserve_current_still_applies_before_a_session_starts(evaluate):
    """With the charger off there is nothing to hold on to."""
    now = moment(23, 0)
    ctx = evaluate(
        now,
        inputs={"fallback_current": 10},
        **{SOC: State("unavailable"),
           "switch.charger": State("off", last_changed=moment(18, 0)),
           NUMBER: setpoint(22, now)},
    )
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


def test_a_cumulative_meter_is_not_mistaken_for_a_session_meter(evaluate):
    """Both kinds carry ``device_class: energy``, so they are easy to confuse.

    A lifetime total dwarfs the target, which would read as "already delivered"
    and silently block charging forever. Falling back to the reserve current
    keeps the car charging and leaves the mistake visible.
    """
    ctx = evaluate(
        moment(23, 0),
        inputs={
            "car_battery_sensor": [],
            "session_energy_sensor": ENERGY,
            "session_energy_target": 20,
        },
        **{ENERGY: 4500.0},
    )
    assert ctx["energy_valid"] is False
    assert ctx["plan_source"] == "none"
    assert ctx["target_reached"] is False
    assert ctx["should_charge"] is True


@pytest.mark.parametrize(
    "target,delivered",
    [(20, 21.5), (2, 7.0), (5, 30.0), (1, 25.0)],
)
def test_a_session_meter_that_overshoots_the_target_is_still_trusted(
    evaluate, target, delivered
):
    """Overshooting is normal and must not be read as a cumulative meter.

    A small target overshoots by a lot in relative terms - a 2 kWh target on a
    night that delivered 7 kWh is threefold - so the plausibility check has to
    be measured against the battery, not against the target. Rejecting the
    meter here would leave the session running past its goal.
    """
    ctx = evaluate(
        moment(23, 0),
        inputs={
            "car_battery_sensor": [],
            "session_energy_sensor": ENERGY,
            "session_energy_target": target,
        },
        **{ENERGY: delivered, "switch.charger": charging_since(moment(23, 0))},
    )
    assert ctx["energy_valid"] is True
    assert ctx["target_reached"] is True
    assert ctx["must_stop"] is True


@pytest.mark.parametrize("reading", ["inf", "-inf", "nan"])
def test_non_finite_readings_are_rejected(evaluate, reading):
    """``float('inf')`` survives a plain float() call but poisons every
    comparison downstream, so it must be filtered out with the same rigour as
    an unavailable entity."""
    ctx = evaluate(moment(23, 0), **{SOC: State(reading)})
    assert ctx["soc_present"] is False
    assert ctx["soc_problem"] == "unavailable"
    assert ctx["desired_current"] == 10


# ---------------------------------------------------------- capacity and soh


def test_capacity_is_used_as_is_without_a_health_sensor(evaluate):
    ctx = evaluate(moment(23, 0))
    assert ctx["soh_pct"] == 100
    assert ctx["effective_capacity"] == 43.0


def test_health_sensor_scales_the_usable_capacity(evaluate):
    ctx = evaluate(moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: 88})
    assert ctx["effective_capacity"] == pytest.approx(37.84)


@pytest.mark.parametrize("reading", [9999, 3, -5, 0, 45, 131])
def test_absurd_health_readings_are_ignored_rather_than_clamped(evaluate, reading):
    """Clamping hid the mistake: a sensor reading 3 became 50 %, and the pack
    silently shrank by half. An implausible reading says the sensor is wrong,
    not that the battery is - so the declared capacity stands.
    """
    ctx = evaluate(moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: reading})
    assert ctx["soh_pct"] == 100
    assert ctx["effective_capacity"] == 43.0


@pytest.mark.parametrize("reading,expected", [(0.98, 98.0), (1.0, 100.0), (0.8, 80.0)])
def test_health_reported_as_a_fraction_is_read_as_a_percentage(evaluate, reading, expected):
    """Some integrations publish 0.98 rather than 98. That used to clamp to the
    50 % floor and halve every current for the night."""
    ctx = evaluate(moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: reading})
    assert ctx["soh_pct"] == expected


def test_unavailable_health_sensor_leaves_capacity_untouched(evaluate):
    ctx = evaluate(
        moment(23, 0), inputs={"soh_sensor": SOH}, **{SOH: State("unavailable")}
    )
    assert ctx["soh_pct"] == 100


# ------------------------------------------------- charger status vocabulary


def test_a_numeric_status_vocabulary_does_not_break_the_render(evaluate):
    """YAML turns a bare ``0`` into an int, and ints have no ``.split``.

    The whole ``variables:`` block used to fail to render, which in Home
    Assistant means the automation does nothing at all for the night - no
    charging, no stopping, not even the error hook. Numeric statuses are
    ordinary on OCPP wrappers and on some Tuya chargers.
    """
    ctx = evaluate(
        moment(1, 0),
        inputs={"status_unplugged": 0, "status_charging": 2},
        **{STATUS: "2", "switch.charger": charging_since(moment(1, 0))},
    )
    assert ctx["plugged_in"] is True
    assert ctx["charging_now"] is True


def test_a_status_typed_in_the_wrong_case_still_matches(evaluate):
    """People retype the charger's statuses into the field however they please."""
    ctx = evaluate(
        moment(1, 0),
        inputs={"status_unplugged": "AVAILABLE"},
        **{STATUS: "available", "switch.charger": charging_since(moment(1, 0))},
    )
    assert ctx["plugged_in"] is False


def test_a_charger_shouting_its_status_still_matches(evaluate):
    """The other direction, and the one that actually happens: chargers report
    ``CHARGING`` and ``SUSPENDED_EV`` in caps while the field holds lower case.

    Both sides have to be normalised - folding only the field would leave this
    case broken, and the charger's own spelling is the one we cannot control.
    """
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"status_charging": "charging", "status_unplugged": "available"},
        **{STATUS: "CHARGING", "switch.charger": charging_since(now)},
    )
    assert ctx["charging_now"] is True
    assert ctx["plugged_in"] is True


def test_a_finished_status_in_caps_is_recognised(evaluate):
    now = moment(1, 0)
    ctx = evaluate(
        now,
        inputs={"status_done": "charged"},
        **{STATUS: "CHARGED", "switch.charger": charging_since(now)},
    )
    assert ctx["charger_finished"] is True
    assert ctx["must_stop"] is True


def test_the_status_is_logged_exactly_as_the_charger_reports_it(evaluate):
    """Normalising is for comparisons only - the log must show the real thing."""
    ctx = evaluate(moment(1, 0), **{STATUS: "CHARGING"})
    assert ctx["charger_status"] == "CHARGING"


# --------------------------------------------- a percentage sitting on target


def test_a_percentage_resting_at_the_target_is_not_frozen_data(evaluate):
    """Reaching the target is a perfectly good reason to stop changing.

    The charger keeps drawing power afterwards - cell balancing, top-off - so
    the freeze detector used to fire, and the damage was not the false alarm
    but what followed: an invalid percentage cannot satisfy ``target_reached``,
    so ``must_stop`` never came and the session ran on to the end of the window.
    """
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"target_soc": 100, "soc_freeze_minutes": 90},
        **{SOC: State(100, last_changed=moment(1, 20)),
           POWER: 3800,
           "switch.charger": charging_since(now, minutes=180)},
    )
    assert ctx["soc_frozen"] is False
    assert ctx["target_reached"] is True
    assert ctx["must_stop"] is True


def test_a_percentage_stuck_below_the_target_is_still_frozen_data(evaluate):
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"target_soc": 100, "soc_freeze_minutes": 90},
        **{SOC: State(60, last_changed=moment(1, 20)),
           POWER: 3800,
           "switch.charger": charging_since(now, minutes=180)},
    )
    assert ctx["soc_frozen"] is True
    assert ctx["target_reached"] is False


def test_a_lower_target_also_counts_as_reached(evaluate):
    now = moment(3, 0)
    ctx = evaluate(
        now,
        inputs={"target_soc": 80, "soc_freeze_minutes": 90},
        **{SOC: State(80, last_changed=moment(1, 20)),
           POWER: 3800,
           "switch.charger": charging_since(now, minutes=180)},
    )
    assert ctx["soc_frozen"] is False
    assert ctx["must_stop"] is True


# ------------------------------------------------- values Home Assistant can use


@pytest.mark.parametrize("soc", [99.9999, 99.99999, 99.999999, 99.9, 50.0, 0.0])
def test_the_budget_never_renders_in_scientific_notation(evaluate, soc):
    """Home Assistant hands a template result back as a number only when the
    text looks numeric by its own rule, and ``4.9e-05`` does not.

    Left alone it stays a *string*, the next multiplication builds a
    twenty-thousand-character string instead of a number, and the division
    after it raises - taking the whole ``variables:`` block with it. In Home
    Assistant that means the automation does nothing for the rest of the night:
    no charging, no stopping, not even the error hook. A car reporting four
    decimal places at a 100 % target is all it takes.
    """
    ctx = evaluate(moment(3, 0), **{SOC: State(soc)})
    assert isinstance(ctx["needed_kwh"], (int, float)), (
        f"needed_kwh came back as {ctx['needed_kwh']!r}"
    )
    assert isinstance(ctx["desired_current"], (int, float))


def test_an_energy_plan_within_a_whisker_of_its_target_also_stays_numeric(evaluate):
    ctx = evaluate(
        moment(3, 0),
        inputs={"session_energy_sensor": ENERGY, "session_energy_target": 20},
        **{SOC: State("unavailable"), ENERGY: 19.999999},
    )
    assert ctx["plan_source"] == "energy"
    assert isinstance(ctx["needed_kwh"], (int, float))


def test_a_small_battery_does_not_make_its_own_meter_implausible(evaluate):
    """The floor under the sanity ceiling is what keeps PHEVs working.

    The ceiling is twice the usable capacity, which on a 9 kWh plug-in hybrid
    would be 18 kWh — and a perfectly honest session meter passes that on a
    single evening, because a PHEV is routinely charged from empty more than
    once. Rejecting the meter would silently drop the energy plan for exactly
    the cars the blueprint claims to support in its opening sentence.
    """
    ctx = evaluate(
        moment(3, 0),
        inputs={
            "session_energy_sensor": ENERGY,
            "session_energy_target": 9,
            "battery_capacity": 9,
        },
        **{SOC: State("unavailable"), ENERGY: 15.0},
    )
    assert ctx["energy_sane_max"] >= 20, "the floor, not twice the capacity"
    assert ctx["energy_valid"] is True
    assert ctx["plan_source"] == "energy"
