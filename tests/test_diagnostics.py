"""What the automation leaves behind for someone debugging it later.

A charging window is eight hours of a mostly idle automation, and the runs that
are hardest to explain are the ones that did nothing at all. These tests pin
down the two things that make such a run reconstructable: a single stated
reason for every outcome (``verdict``), and one structured snapshot of the
state it was decided from (``diag``).
"""

from __future__ import annotations

import datetime as dt

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
def logged_run(run):
    """``run`` with the logbook switched on - this file is about what gets
    written down, and nothing is written without it."""

    def _run(now=None, *, inputs=None, **kwargs):
        merged = {"debug_logging": True}
        merged.update(inputs or {})
        return run(now, inputs=merged, **kwargs)

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
        (23, {}, "ставим ток"),
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
    """The hardest case to debug: everything is fine, so nothing happens.

    Uses a setpoint above the target, since a setpoint below it is now always
    rewritten - see the deadband asymmetry.
    """
    now = moment(23, 30)
    world = build_world(now, **{SWITCH: charging_since(now, 300)})
    world[NUMBER] = setpoint(23, now)
    ctx = evaluate(now, world=world)
    assert ctx["needs_write"] is False
    assert "зоны нечувствительности" in ctx["verdict"]


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


def test_the_two_steps_of_a_start_are_named_separately(evaluate):
    """Starting takes two runs now; each has to say which one it is."""
    now = moment(23, 0)
    world = build_world(now)
    world[NUMBER] = setpoint(10, now, age_seconds=18000)
    world[SWITCH] = State("off", last_changed=now - dt.timedelta(hours=5))
    assert "включим следующим шагом" in evaluate(now, world=world)["verdict"]

    world[NUMBER] = setpoint(21, now, age_seconds=18000)
    assert "запускаем зарядку" in evaluate(now, world=world)["verdict"]


def test_a_missing_switch_entity_is_named_outright(evaluate):
    """Renaming the switch, or an integration that failed to load, otherwise
    reads as unexplained idleness: a missing entity reports age zero, and age
    zero blocks switching on forever via the command throttle."""
    now = moment(3, 0)
    world = build_world(now)
    del world[SWITCH]
    ctx = evaluate(now, world=world)
    assert ctx["switch_present"] is False
    assert "не найден" in ctx["verdict"]
    assert SWITCH in ctx["verdict"], "name the entity, so it can be looked up"


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


def test_the_verdict_and_the_stop_reason_never_disagree(evaluate):
    """A car that drove off takes the cable with it, so both "away" and
    "unplugged" hold at once. If the two fields ranked them differently, the
    logbook and the trace would blame different things for one stop."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{
            TRACKER: "not_home",
            STATUS: "available",
            POWER: 0,
            AMPERE: 0,
            SWITCH: charging_since(now),
        },
    )
    assert ctx["stop_reason"] == "car_not_home"
    assert "не дома" in ctx["verdict"]


@pytest.mark.parametrize(
    "overrides,verdict_fragment",
    [
        ({LINK: "off"}, "офлайн"),
        ({STATUS: State("unknown")}, "статус"),
    ],
)
def test_troubles_that_do_not_stop_the_session_name_no_stop_reason(
    evaluate, overrides, verdict_fragment
):
    """Neither of these actually stops anything, and ``stop_reason`` must say so.

    An offline charger cannot be commanded at all - ``must_stop`` requires the
    link - and an unreadable status deliberately lets a running session carry
    on. Naming a cause here made the trace read as though the session had been
    cut for that reason. The verdict is where the trouble gets described.
    """
    now = moment(3, 0)
    ctx = evaluate(now, **{SWITCH: charging_since(now), **overrides})
    assert ctx["must_stop"] is False
    assert ctx["stop_reason"] == "none"
    assert verdict_fragment in ctx["verdict"]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({SOC: 100}, "target_reached"),
        ({STATUS: "charged"}, "charger_reports_charged"),
        ({STATUS: "fault"}, "fault"),
    ],
)
def test_a_real_stop_names_its_cause(evaluate, overrides, expected):
    now = moment(3, 0)
    ctx = evaluate(now, **{SWITCH: charging_since(now), **overrides})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == expected


def test_a_healthy_run_reports_no_stop_reason(evaluate):
    """Any value here reads as a cause; while nothing is being stopped there
    is no cause to report."""
    now = moment(3, 0)
    ctx = evaluate(now, **{SWITCH: charging_since(now)})
    assert ctx["must_stop"] is False
    assert ctx["stop_reason"] == "none"


def test_command_flags_are_silent_when_the_branch_cannot_run(evaluate):
    """These 16 runs made up most of the night: the car was gone, no command
    could possibly be sent, yet the snapshot claimed a write was pending."""
    now = moment(3, 0)
    ctx = evaluate(
        now,
        **{
            TRACKER: "not_home",
            STATUS: "available",
            POWER: 0,
            AMPERE: 0,
            SWITCH: State("off", last_changed=now - dt.timedelta(hours=3)),
        },
    )
    assert ctx["should_charge"] is False
    commands = ctx["diag"]["команды"]
    assert commands["записать_ток"] is False
    assert commands["включить"] is False
    assert commands["выставить_режим"] is False


def test_the_stop_entry_carries_what_the_stop_was_judged_on(logged_run):
    """The most important line of the night was also the least informative:
    it named the reason but not the evidence, so a tracker that lied while
    17.6 A were flowing looked exactly like a car that had driven away."""
    now = moment(3, 0)
    written = messages(logged_run(now, **{SOC: 100, SWITCH: charging_since(now, 200)}))
    assert written
    entry = written[0]
    assert "трекер=" in entry, "what the tracker claimed"
    assert "шло" in entry, "what the charger was actually delivering"
    assert "сессия длилась" in entry


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
    # The command flags report what will actually reach the charger, so they
    # carry ``should_charge`` as a factor. Spelled out in full here: with
    # ``should_charge`` true in this fixture, comparing against ``needs_write``
    # alone would be the same expression on both sides.
    assert diag["команды"]["записать_ток"] == (
        ctx["needs_write"] and ctx["should_charge"]
    )
    assert diag["план"]["часов_осталось"] == ctx["hours_left"]
    assert diag["станция"]["статус"] == ctx["charger_status"]


def test_the_shown_calculation_is_capped_but_the_raw_one_is_not(evaluate):
    """Two numbers on purpose, and the difference is the whole point.

    In the last minutes of the window the planning horizon collapses to
    seconds and the formula returns hundreds of amps. That never reaches the
    charger — the ceiling clamps it — but "расчёт 495 А" in the log reads like
    a breakage and derails the review of the night. So the snapshot shows the
    capped figure, while the raw one stays raw: it is what tells you the
    station could not keep up.
    """
    now = moment(6, 54)  # the window closes at 07:00
    ctx = evaluate(now, **{SOC: 20, "switch.charger": charging_since(now)})
    assert ctx["calc_current"] > ctx["num_max"], "the horizon has collapsed"
    assert ctx["calc_current_shown"] == ctx["num_max"], "shown value is capped"
    assert ctx["diag"]["ток"]["расчёт_до_округления"] == ctx["calc_current_shown"]
    assert ctx["diag"]["ток"]["расчёт_сырой"] == ctx["calc_current"]


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


def test_a_run_without_commands_still_leaves_a_trace(logged_run):
    """Previously the most common outcome recorded nothing at all, and
    "why did nothing happen last night" had no answer anywhere."""
    calls = logged_run(moment(12, 0))
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
def test_each_silent_outcome_names_itself_in_the_logbook(logged_run, overrides, expected):
    written = messages(logged_run(moment(23, 0), **overrides))
    assert written and expected in written[0]


def test_the_idle_entry_is_silent_when_logging_is_off(logged_run):
    assert messages(logged_run(moment(12, 0), inputs={"debug_logging": False})) == []


def test_the_charging_entry_leads_with_the_verdict(logged_run, evaluate):
    now = moment(23, 0)
    written = messages(logged_run(now))
    assert written and written[0].startswith(evaluate(now)["verdict"])


def test_the_stop_entry_states_the_verdict_and_the_reason(logged_run):
    now = moment(3, 0)
    written = messages(logged_run(now, **{SOC: 100, SWITCH: charging_since(now)}))
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
def test_every_logbook_entry_renders(logged_run, hour, overrides):
    for message in messages(logged_run(moment(hour, 0), **overrides)):
        assert "{{" not in message
        assert message.strip()


def test_a_session_ended_with_an_unreadable_status_says_so(evaluate):
    """The window closes while the status sensor is dark.

    The session does stop here - the window is over - and the reason has to name
    the unreadable status rather than falling through to something that reads
    like an ordinary end of window. Without it, a night that ended because the
    charger integration died looks identical to one that simply finished.
    """
    now = moment(9, 0)
    ctx = evaluate(now, **{STATUS: State("unavailable"), SWITCH: charging_since(now)})
    assert ctx["must_stop"] is True
    assert ctx["stop_reason"] == "status_unknown"
