"""What the automation leaves behind for someone debugging it later.

A charging window is eight hours of a mostly idle automation, and the runs that
are hardest to explain are the ones that did nothing at all. These tests pin
down the two things that make such a run reconstructable: a single stated
reason for every outcome (``verdict``), and one structured snapshot of the
state it was decided from (``diag``).
"""

from __future__ import annotations

import pytest
from conftest import (
    AMPERE,
    LINK,
    NUMBER,
    POWER,
    PROBLEM,
    SESSION,
    SOC,
    STATUS,
    SWITCH,
    TRACKER,
    build_world,
    charging_since,
    setpoint,
)
from ha_sim import State, moment


@pytest.fixture
def run(blueprint, base_inputs):
    def _run(now=None, *, inputs=None, world=None, **world_overrides):
        now = now or moment(23, 0)
        merged = dict(base_inputs)
        merged["debug_logging"] = True
        merged.update(inputs or {})
        return blueprint.run_actions(
            world=world if world is not None else build_world(now, **world_overrides),
            now=now,
            inputs=merged,
        )

    return _run


def messages(calls):
    return [
        c["data"]["message"] for c in calls if c.get("action") == "logbook.log"
    ]


# ------------------------------------------------------------------ verdict


@pytest.mark.parametrize(
    "hour,overrides,expected",
    [
        (12, {}, "вне окна"),
        (23, {}, "запускаем"),
        (23, {TRACKER: "not_home"}, "не дома"),
        (23, {STATUS: "available"}, "кабель не подключён"),
        (23, {LINK: "off"}, "офлайн"),
        (23, {STATUS: State("unknown")}, "нечитаем"),
    ],
)
def test_every_outcome_names_its_reason(evaluate, hour, overrides, expected):
    assert expected in evaluate(moment(hour, 0), **overrides)["verdict"]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({STATUS: "fault"}, "неисправность"),
        ({SOC: 100}, "цель достигнута"),
        ({STATUS: "charged"}, "заряжено"),
    ],
)
def test_reasons_for_stopping_are_named_too(evaluate, overrides, expected):
    now = moment(3, 0)
    world = dict(overrides)
    world[SWITCH] = charging_since(now)
    assert expected in evaluate(now, **world)["verdict"]


def test_the_verdict_explains_a_deliberate_silence(evaluate):
    """The hardest case to debug: everything is fine, so nothing happens."""
    now = moment(23, 30)
    world = build_world(now, **{SWITCH: charging_since(now, 300)})
    world[NUMBER] = setpoint(21, now)
    verdict = evaluate(now, world=world)["verdict"]
    assert "зоны нечувствительности" in verdict


def test_the_verdict_explains_a_throttled_command(evaluate):
    """A suppressed write looks exactly like a broken regulator from outside."""
    now = moment(23, 30)
    world = build_world(now, **{SWITCH: charging_since(now, 300)})
    world[NUMBER] = setpoint(10, now, age_seconds=5)
    ctx = evaluate(now, world=world)
    assert ctx["needs_write"] is False, "precondition: the write is throttled"
    assert "пауза" in ctx["verdict"]


def test_a_hand_started_session_says_why_it_is_untouched(evaluate):
    now = moment(23, 30)
    world = build_world(now, **{SESSION: "off", SOC: 40, SWITCH: charging_since(now, 180)})
    world[NUMBER] = setpoint(10, now)
    ctx = evaluate(now, inputs={"session_flag": SESSION}, world=world)
    assert "вручную" in ctx["verdict"]


def test_a_charger_that_draws_no_current_says_so(evaluate):
    """Without this the watchdog case reads as a healthy "current is fine",
    which is the opposite of what is happening."""
    now = moment(23, 40)
    ctx = evaluate(
        now,
        **{
            SWITCH: charging_since(now, 20),
            POWER: 0,
            AMPERE: 0,
            STATUS: "plugged_in",
        },
    )
    assert ctx["no_power_alarm"] is True, "precondition: the watchdog fired"
    assert "ток не идёт" in ctx["verdict"]


def test_unreliable_charge_data_is_mentioned_while_charging(evaluate):
    now = moment(23, 40)
    world = build_world(
        now, **{SWITCH: charging_since(now, 300), SOC: State("unavailable")}
    )
    world[NUMBER] = setpoint(10, now)
    ctx = evaluate(now, world=world)
    assert ctx["data_alarm"] is True, "precondition: the data is bad"
    assert ctx["needs_write"] is False, "precondition: nothing else to report"
    assert "ненадёжны" in ctx["verdict"]


def test_the_verdict_prefers_the_first_reason_that_applies(evaluate):
    """With several things wrong at once, an arbitrary one would mislead:
    an offline charger explains the silence, a car that is away does not."""
    now = moment(23, 0)
    ctx = evaluate(now, **{LINK: "off", TRACKER: "not_home", STATUS: "available"})
    assert "офлайн" in ctx["verdict"]


@pytest.mark.parametrize("hour", [23, 3, 9, 12])
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {SOC: State("unavailable")},
        {STATUS: State("unknown")},
        {NUMBER: None},
        {TRACKER: State("unavailable")},
        {LINK: "off"},
        {PROBLEM: "on"},
    ],
)
def test_the_verdict_is_never_empty_or_unrendered(evaluate, hour, overrides):
    verdict = evaluate(moment(hour, 0), **overrides)["verdict"]
    assert verdict.strip()
    assert "{{" not in verdict and "{%" not in verdict


# --------------------------------------------------------------------- diag


def test_the_snapshot_is_a_structure_not_a_string(evaluate):
    """A string would render as one unreadable line in the trace viewer;
    a dict unfolds into a tree."""
    diag = evaluate(moment(23, 0))["diag"]
    assert isinstance(diag, dict)
    assert isinstance(diag["ток"], dict)


def test_the_snapshot_covers_every_area_of_the_decision(evaluate):
    diag = evaluate(moment(23, 0))["diag"]
    assert set(diag) == {
        "вердикт",
        "решение",
        "команды",
        "ток",
        "план",
        "заряд",
        "станция",
        "окно",
        "место",
    }


def test_the_snapshot_agrees_with_the_variables_it_reports(evaluate):
    """A snapshot that drifts from the real values is worse than none."""
    ctx = evaluate(moment(23, 0))
    diag = ctx["diag"]
    assert diag["вердикт"] == ctx["verdict"]
    assert diag["решение"]["заряжать"] == ctx["should_charge"]
    assert diag["решение"]["остановить"] == ctx["must_stop"]
    assert diag["ток"]["уставка_нужна"] == ctx["desired_current"]
    assert diag["ток"]["уставка_сейчас"] == ctx["current_now"]
    assert diag["команды"]["записать_ток"] == ctx["needs_write"]
    assert diag["план"]["часов_осталось"] == ctx["hours_left"]
    assert diag["станция"]["статус"] == ctx["charger_status"]


def test_the_snapshot_reports_missing_sensors_as_missing(evaluate):
    """Not every install wires up power and current sensors."""
    ctx = evaluate(
        moment(23, 0),
        inputs={"charger_current_sensor": [], "charger_power_sensor": []},
    )
    assert ctx["diag"]["ток"]["идёт_по_факту"] == "н/д"
    assert ctx["diag"]["ток"]["мощность_вт"] == "н/д"


def test_the_snapshot_reports_unusable_charge_data_as_missing(evaluate):
    ctx = evaluate(moment(23, 0), **{SOC: State("unavailable")})
    assert ctx["diag"]["заряд"]["процент"] == "н/д"
    assert ctx["diag"]["заряд"]["проблема"] == "unavailable"


@pytest.mark.parametrize("hour", [23, 3, 9, 12])
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {SOC: State("unavailable")},
        {STATUS: State("unknown")},
        {NUMBER: None},
        {POWER: State("unavailable"), AMPERE: State("unavailable")},
        {TRACKER: State("unavailable")},
        {LINK: "off"},
    ],
)
def test_the_snapshot_survives_missing_data(evaluate, hour, overrides):
    """It is built from a dozen sources; one unrenderable value would abort
    the whole run, and it would abort it exactly when data is bad - the
    moment the snapshot is needed most."""
    diag = evaluate(moment(hour, 0), **overrides)["diag"]
    assert isinstance(diag, dict)
    assert diag["вердикт"].strip()


def test_the_snapshot_needs_no_optional_entity_at_all(evaluate):
    """The minimal install: a switch and a number, nothing else."""
    bare = {
        key: []
        for key in (
            "charger_status_sensor",
            "charger_power_sensor",
            "charger_current_sensor",
            "charger_voltage_sensor",
            "charger_link_sensor",
            "charger_problem_sensor",
            "charger_mode_select",
            "car_battery_sensor",
            "car_tracker",
        )
    }
    diag = evaluate(moment(23, 0), inputs=bare)["diag"]
    assert isinstance(diag, dict)
    assert diag["вердикт"].strip()


# ------------------------------------------------------------------ logbook


def test_a_run_without_commands_still_leaves_a_trace(run):
    """Previously the most common outcome recorded nothing at all, and
    "why did nothing happen last night" had no answer anywhere."""
    calls = run(moment(12, 0))
    written = messages(calls)
    assert written, "an idle recalculation must still be explained"
    assert "вне окна" in written[0]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({STATUS: "available"}, "кабель не подключён"),
        ({LINK: "off"}, "офлайн"),
        ({TRACKER: "not_home"}, "не дома"),
        ({STATUS: State("unknown")}, "нечитаем"),
    ],
)
def test_each_silent_outcome_names_itself_in_the_logbook(run, overrides, expected):
    written = messages(run(moment(23, 0), **overrides))
    assert written and expected in written[0]


def test_the_idle_entry_is_silent_when_logging_is_off(run):
    assert messages(run(moment(12, 0), inputs={"debug_logging": False})) == []


def test_the_charging_entry_leads_with_the_verdict(run, evaluate):
    now = moment(23, 0)
    written = messages(run(now))
    assert written and written[0].startswith(evaluate(now)["verdict"])


def test_the_stop_entry_states_the_verdict_and_the_reason(run):
    now = moment(3, 0)
    written = messages(run(now, **{SOC: 100, SWITCH: charging_since(now)}))
    assert written
    assert "цель достигнута" in written[0]
    assert "target_reached" in written[0]


@pytest.mark.parametrize("hour", [23, 3, 9, 12])
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {SOC: State("unavailable")},
        {STATUS: State("unknown")},
        {NUMBER: None},
        {LINK: "off"},
    ],
)
def test_every_logbook_entry_renders(run, hour, overrides):
    for message in messages(run(moment(hour, 0), **overrides)):
        assert "{{" not in message
        assert message.strip()
