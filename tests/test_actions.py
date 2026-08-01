"""The commands actually sent to the charger.

Every other file checks the ``variables:`` block - the decisions. This one
checks the ``actions:`` block that carries them out: which service is called,
in what order, with what payload. Ordering is the interesting part, because a
charger receiving the right commands in the wrong order behaves badly in ways
no amount of correct arithmetic can fix.
"""

from __future__ import annotations

import pytest
from conftest import (
    MODE,
    NUMBER,
    SESSION,
    SOC,
    STATUS,
    SWITCH,
    charging_since,
    setpoint,
)
from ha_sim import State, moment


@pytest.fixture
def run(blueprint, base_inputs):
    from conftest import build_world

    def _run(now=None, *, inputs=None, **world_overrides):
        now = now or moment(23, 0)
        merged = dict(base_inputs)
        merged.update(inputs or {})
        return blueprint.run_actions(
            world=build_world(now, **world_overrides), now=now, inputs=merged
        )

    return _run


def actions_of(calls):
    return [c["action"] for c in calls if "action" in c]


# ------------------------------------------------------------ starting up


def test_starting_a_session_sets_the_current_before_switching_on(run):
    """Switching on first would let the charger start at whatever setpoint was
    left over from last night - possibly the maximum."""
    calls = run()
    assert actions_of(calls) == ["number.set_value", "switch.turn_on"]


def test_the_start_hook_runs_after_the_charger_is_on(run):
    calls = run()
    assert calls[-1] == {"hook": "on_start_actions"}


def test_the_setpoint_written_is_the_one_that_was_calculated(run, evaluate):
    now = moment(23, 0)
    expected = evaluate(now)["desired_current"]
    write = next(c for c in run(now) if c.get("action") == "number.set_value")
    assert write["data"]["value"] == expected
    assert write["entity_id"] == NUMBER


def test_no_blocking_delay_precedes_a_command(run):
    """In ``restart`` mode a delay is where commands go to die: any trigger
    firing during it discards everything queued behind it."""
    calls = run()
    assert not any("delay" in c for c in calls)


def test_an_already_running_session_is_not_switched_on_again(run):
    now = moment(3, 0)
    calls = run(now, **{SWITCH: charging_since(now), SOC: 40})
    assert "switch.turn_on" not in actions_of(calls)
    assert not any(c.get("hook") == "on_start_actions" for c in calls)


def test_a_settled_setpoint_produces_no_commands_at_all(run):
    """The quiet case: charging along nicely, nothing worth saying."""
    now = moment(23, 0)
    calls = run(now, **{SWITCH: charging_since(now), NUMBER: setpoint(21, now)})
    assert calls == []


# ------------------------------------------------------------ charger mode


def test_the_charger_is_forced_into_manual_mode_first(run):
    """A charger left on its own schedule overrides everything we ask for."""
    calls = run(**{MODE: "scheduled_charge"})
    assert actions_of(calls)[0] == "select.select_option"
    assert calls[0]["data"]["option"] == "immediate"


def test_the_mode_is_left_alone_when_already_correct(run):
    assert "select.select_option" not in actions_of(run())


def test_the_mode_is_left_alone_when_no_select_is_configured(run):
    calls = run(inputs={"charger_mode_select": []})
    assert "select.select_option" not in actions_of(calls)


# ------------------------------------------------------------ stopping


def test_stopping_switches_off_before_anything_else(run):
    """Turning the power off is the safety command; it must not queue behind
    a notification that might be slow or fail."""
    now = moment(3, 0)
    calls = run(now, **{SOC: 100, SWITCH: charging_since(now)})
    assert calls[0]["action"] == "switch.turn_off"
    assert calls[0]["entity_id"] == SWITCH


def test_the_finish_hook_runs_after_the_charger_is_off(run):
    now = moment(3, 0)
    calls = run(now, **{SOC: 100, SWITCH: charging_since(now)})
    assert calls[-1] == {"hook": "on_finish_actions"}


def test_the_current_is_reset_last_and_only_when_asked(run):
    now = moment(3, 0)
    world = {SOC: 100, SWITCH: charging_since(now)}
    plain = run(now, **world)
    assert "number.set_value" not in actions_of(plain)

    resetting = run(now, inputs={"reset_current_on_stop": True}, **world)
    assert actions_of(resetting) == ["switch.turn_off", "number.set_value"]
    # The reset trails the switch-off so a lost step cannot leave power flowing.
    assert resetting[-1]["data"]["value"] == 6.0
    assert any("delay" in c for c in resetting)


# ------------------------------------------------------------ session flag


def test_the_session_flag_is_raised_on_start_and_cleared_on_stop(run):
    started = run(inputs={"session_flag": SESSION}, **{SESSION: "off"})
    assert "input_boolean.turn_on" in actions_of(started)

    now = moment(3, 0)
    stopped = run(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "on", SOC: 100, SWITCH: charging_since(now)},
    )
    assert "input_boolean.turn_off" in actions_of(stopped)


def test_no_flag_commands_when_no_helper_is_configured(run):
    calls = actions_of(run())
    assert "input_boolean.turn_on" not in calls
    assert "input_boolean.turn_off" not in calls


# ------------------------------------------------------------ alarm hook


def test_the_error_hook_runs_before_the_charge_decision(run):
    """A fault should be reported even if stopping the charger then hangs."""
    now = moment(3, 0)
    calls = run(now, **{STATUS: "fault", SWITCH: charging_since(now)})
    assert calls[0] == {"hook": "on_error_actions"}
    assert "switch.turn_off" in actions_of(calls)


def test_a_healthy_run_does_not_fire_the_error_hook(run):
    assert not any(c.get("hook") == "on_error_actions" for c in run())


# ------------------------------------------------------------ logbook


def test_the_logbook_entry_is_written_only_when_enabled(run):
    assert "logbook.log" not in actions_of(run())
    assert "logbook.log" in actions_of(run(inputs={"debug_logging": True}))


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {SOC: State("unavailable")},
        {STATUS: State("unknown")},
        {NUMBER: None},
    ],
)
def test_the_logbook_message_renders_in_every_state(run, overrides):
    """The message interpolates a dozen variables. A single unrenderable one
    would abort the sequence, so it has to survive missing data too."""
    now = moment(23, 0)
    world = dict(overrides)
    world[SWITCH] = charging_since(now)
    calls = run(now, inputs={"debug_logging": True}, **world)
    entries = [c for c in calls if c.get("action") == "logbook.log"]
    for entry in entries:
        message = entry["data"]["message"]
        assert "{{" not in message
        assert message.strip()


# ------------------------------------------------------------ doing nothing


@pytest.mark.parametrize(
    "hour,overrides",
    [
        (12, {}),  # outside the window, charger idle
        (23, {STATUS: "available"}),  # nothing plugged in
        (23, {STATUS: State("unavailable")}),  # status unreadable
    ],
)
def test_situations_that_warrant_no_command_at_all(run, hour, overrides):
    assert run(moment(hour, 0), **overrides) == []


# ------------------------------------------------------- hand-started charging


def test_a_hand_started_session_receives_no_commands_at_all(run):
    """Charging switched on by hand keeps the current its owner chose."""
    now = moment(23, 30)
    calls = run(
        now,
        inputs={"session_flag": SESSION},
        **{
            SESSION: "off",
            SOC: 40,
            NUMBER: setpoint(10, now),
            SWITCH: charging_since(now, 180),
        },
    )
    assert calls == []


def test_a_hand_started_session_is_not_switched_off_at_the_end_of_the_window(run):
    now = moment(9, 0)
    calls = run(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "off", SWITCH: charging_since(now, 300)},
    )
    assert calls == []


def test_a_faulty_charger_is_switched_off_even_if_started_by_hand(run):
    now = moment(3, 0)
    calls = run(
        now,
        inputs={"session_flag": SESSION},
        **{SESSION: "off", STATUS: "fault", SWITCH: charging_since(now)},
    )
    assert "switch.turn_off" in actions_of(calls)
