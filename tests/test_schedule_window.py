"""The charging window: midnight crossing, weekdays and the time budget."""

from __future__ import annotations

import pytest
from conftest import charging_since
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


def test_a_closed_day_stops_an_active_session(evaluate):
    now = moment(23, 30, day=SATURDAY)
    ctx = evaluate(
        now,
        inputs={"weekdays": WEEKDAYS_ONLY},
        **{"switch.charger": charging_since(now)},
    )
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "window_end"
