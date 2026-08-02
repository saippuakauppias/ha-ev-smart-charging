"""The charging window: midnight crossing, weekdays and the time budget."""

from __future__ import annotations

import pytest
from conftest import SOC, charging_since
from ha_sim import moment

# 2026: 31 July is a Friday, 1 August a Saturday, 3 August a Monday.
FRIDAY, SATURDAY, SUNDAY, MONDAY = 31, 1, 2, 3
WEEKDAYS_ONLY = ["mon", "tue", "wed", "thu", "fri"]


@pytest.mark.parametrize(
    "hour,minute,inside",
    [
        (22, 59, False),
        (23, 0, True),
        (23, 1, True),
        (2, 0, True),
        (6, 59, True),
        (7, 0, False),
        (12, 0, False),
    ],
)
def test_overnight_window_spans_midnight(evaluate, hour, minute, inside):
    ctx = evaluate(moment(hour, minute))
    assert ctx["in_window"] is inside


@pytest.mark.parametrize(
    "hour,inside", [(9, False), (10, True), (14, True), (17, True), (18, False)]
)
def test_daytime_window_behaves_normally(evaluate, hour, inside):
    ctx = evaluate(
        moment(hour, 0), inputs={"start_time": "10:00:00", "stop_time": "18:00:00"}
    )
    assert ctx["in_window"] is inside


def test_identical_start_and_stop_means_always_on(evaluate):
    for hour in (0, 6, 12, 18, 23):
        ctx = evaluate(
            moment(hour, 0), inputs={"start_time": "00:00:00", "stop_time": "00:00:00"}
        )
        assert ctx["in_window"] is True


def test_time_budget_shrinks_as_the_night_passes(evaluate):
    early = evaluate(moment(23, 0))["hours_left"]
    middle = evaluate(moment(2, 0))["hours_left"]
    late = evaluate(moment(6, 0))["hours_left"]
    assert early > middle > late


def test_reserve_is_subtracted_from_the_budget(evaluate):
    without = evaluate(moment(23, 0), inputs={"time_reserve_minutes": 0})["hours_left"]
    with_reserve = evaluate(moment(23, 0), inputs={"time_reserve_minutes": 30})["hours_left"]
    assert without - with_reserve == pytest.approx(0.5, abs=1e-3)


def test_budget_never_reaches_zero(evaluate):
    """A zero would divide by zero in the current calculation."""
    ctx = evaluate(moment(6, 59), inputs={"time_reserve_minutes": 60})
    assert ctx["hours_left"] > 0


def test_budget_at_the_end_of_the_window_stays_positive(evaluate):
    assert evaluate(moment(6, 59, day=MONDAY))["hours_left"] > 0


def test_less_time_left_means_more_current(evaluate):
    early = evaluate(moment(23, 0))["calc_current"]
    late = evaluate(moment(4, 0))["calc_current"]
    assert late > early


@pytest.mark.parametrize("hour", [9, 12, 17, 21])
def test_outside_the_window_the_budget_does_not_span_the_whole_day(evaluate, hour):
    """The next window end can be almost 24 hours away, and planning against
    that horizon would collapse the current to the minimum."""
    ctx = evaluate(moment(hour, 0))
    assert ctx["in_window"] is False
    assert ctx["hours_left"] <= 8


def test_an_emergency_outside_the_window_charges_flat_out(evaluate):
    """An emergency top-up exists precisely to be quick."""
    ctx = evaluate(moment(12, 0), inputs={"emergency_soc": 20}, **{SOC: 10})
    assert ctx["emergency"] is True
    assert ctx["hours_left"] < 1
    assert ctx["desired_current"] == 28


@pytest.mark.parametrize("soc,ceiling", [(31, 22), (50, 16), (99, 7)])
def test_finishing_off_after_the_window_is_gentle(evaluate, soc, ceiling):
    """The other reason to charge outside the window is "the window ended but
    the target was missed". There is no hurry there, and hammering the daytime
    tariff at maximum current is the opposite of what this blueprint is for.
    """
    now = moment(12, 0)
    # The session has to predate the end of the window, or it is not something
    # that was "started" in the first place - see the emergency case below.
    ctx = evaluate(
        now,
        inputs={"emergency_soc": 20, "stop_at_window_end": False},
        **{SOC: soc, "switch.charger": charging_since(now, minutes=10 * 60)},
    )
    assert ctx["emergency"] is False
    assert ctx["should_charge"] is True
    assert ctx["desired_current"] <= ceiling


def test_an_emergency_top_up_ends_when_the_charge_clears_the_threshold(evaluate):
    """An emergency top-up is licence to *start* outside the window, not licence
    to run on to the target through the expensive part of the day.

    The hysteresis clears ``emergency`` a few points above the threshold, and
    before this was fixed the "finish what you started" clause picked the
    session straight back up and carried it to 100 % in daytime tariff.
    """
    now = moment(14, 0)
    common = {"emergency_soc": 20, "emergency_hysteresis": 10,
              "stop_at_window_end": False}
    # Two hours in: the top-up itself began well after the window closed.
    started_outside = {"switch.charger": charging_since(now, minutes=120)}

    low = evaluate(now, inputs=common, **{SOC: 15, **started_outside})
    assert low["emergency"] is True
    assert low["should_charge"] is True, "below the threshold it must keep going"

    recovered = evaluate(now, inputs=common, **{SOC: 31, **started_outside})
    assert recovered["emergency"] is False
    assert recovered["should_charge"] is False
    assert recovered["must_stop"] is True


# ------------------------------------------------------------------ weekdays


def test_weekday_filter_allows_a_configured_day(evaluate):
    ctx = evaluate(moment(23, 30, day=FRIDAY, month=7), inputs={"weekdays": WEEKDAYS_ONLY})
    assert ctx["weekday_ok"] is True
    assert ctx["in_window"] is True


def test_night_started_on_friday_still_counts_as_friday(evaluate):
    """The small hours of Saturday belong to Friday's window."""
    ctx = evaluate(moment(2, 0, day=SATURDAY), inputs={"weekdays": WEEKDAYS_ONLY})
    assert ctx["weekday_ok"] is True
    assert ctx["in_window"] is True


def test_a_window_starting_on_an_excluded_day_does_not_open(evaluate):
    ctx = evaluate(moment(23, 30, day=SATURDAY), inputs={"weekdays": WEEKDAYS_ONLY})
    assert ctx["weekday_ok"] is False
    assert ctx["in_window"] is False


def test_the_night_after_an_excluded_day_stays_closed(evaluate):
    ctx = evaluate(moment(2, 0, day=SUNDAY), inputs={"weekdays": WEEKDAYS_ONLY})
    assert ctx["weekday_ok"] is False


def test_all_days_selected_by_default(evaluate):
    for day in (FRIDAY, SATURDAY, SUNDAY):
        month = 7 if day == FRIDAY else 8
        assert evaluate(moment(2, 0, day=day, month=month))["weekday_ok"] is True


def test_an_empty_weekday_list_is_read_as_every_day(evaluate):
    """Clearing every checkbox is far more often an oversight than a request
    to disable the automation permanently."""
    ctx = evaluate(moment(23, 30, day=FRIDAY, month=7), inputs={"weekdays": []})
    assert ctx["weekday_ok"] is True
    assert ctx["in_window"] is True


def test_a_closed_day_stops_an_active_session(evaluate):
    now = moment(23, 30, day=SATURDAY)
    ctx = evaluate(
        now,
        inputs={"weekdays": WEEKDAYS_ONLY},
        **{"switch.charger": charging_since(now)},
    )
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"


# ------------------------------------------------------- daylight saving time


@pytest.mark.parametrize(
    "date,label",
    [((2026, 3, 29), "clocks go forward"), ((2026, 10, 25), "clocks go back")],
)
@pytest.mark.parametrize("hour", [0, 1, 3, 5, 6])
def test_the_window_survives_a_daylight_saving_change(blueprint, base_inputs,
                                                      date, label, hour):
    """Twice a year the night is an hour shorter or longer than usual.

    The window is stated in wall-clock time, so it has to follow the clock:
    on the short night there is genuinely one hour less to charge in, and the
    plan must notice rather than working from a fixed number of hours. The rest
    of the suite runs on a fixed UTC+3 offset and can never see this.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from conftest import build_world

    tz = ZoneInfo("Europe/Berlin")
    now = dt.datetime(*date, hour, 0, tzinfo=tz)
    inputs = dict(base_inputs)
    inputs.update({"start_time": "23:00:00", "stop_time": "07:00:00"})
    world = build_world(now, **{SOC: 50, "switch.charger": charging_since(now)})
    ctx = blueprint.evaluate(world=world, now=now, inputs=inputs)

    assert ctx["in_window"] is True, f"{label}: {now:%H:%M %Z} should be inside"
    # Wall-clock hours to 06:30 (07:00 less the half-hour reserve).
    expected = (6 - hour) + 0.5
    assert ctx["hours_left"] == pytest.approx(expected, abs=0.01)
    assert 6 <= ctx["desired_current"] <= 28
