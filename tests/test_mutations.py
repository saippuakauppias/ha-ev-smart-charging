"""Mutation testing: do the tests actually catch a broken blueprint?

A green suite proves nothing on its own - tests can be vacuous. This file
deliberately corrupts one piece of the blueprint at a time and asserts that the
rest of the suite notices. Each mutation corresponds to a promise made in the
README, so a surviving mutation means that promise is untested.

This is the check that found the missing outer clamp on the current: the value
was clamped twice, and the inner clamp masked the outer one for every ceiling
that happened to be a multiple of the step. It later caught two regressions
introduced by fixes of its own: a plausibility check that rejected a correct
energy meter, and a ceiling that stopped being reachable at coarse step sizes.

Runs the full suite once per mutation, so it is marked ``slow``. Skip it with::

    pytest -m "not slow"
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import BLUEPRINT_PATH

REPO_ROOT = BLUEPRINT_PATH.parents[3]

#: ``(label, original_fragment, corrupted_fragment)``.
MUTATIONS: list[tuple[str, str, str]] = [
    (
        "the ceiling is not lowered to a multiple of the step",
        "{% set snapped = ((num_max_raw / cur_step) | round(0, 'floor')) * cur_step %}",
        "{% set snapped = num_max_raw %}",
    ),
    # The outer clamp in ``desired_current`` is now belt-and-braces: since the
    # ceiling itself is snapped down to a multiple of the step, rounding up can
    # no longer overshoot it. It is kept as a cheap guard, but a mutation of it
    # would survive by construction, so the mutation above targets the snapping
    # of the ceiling instead - that is what actually enforces the bound now.
    (
        "entity attributes may widen the configured range",
        'num_min: "{{ [lim_min, ent_min] | max }}"',
        'num_min: "{{ ent_min }}"',
    ),
    (
        "an unavailable battery sensor is treated as valid",
        "{{ e_battery != '' and has_value(e_battery) "
        "and is_number(states(e_battery)) }}",
        "{{ e_battery != '' }}",
    ),
    (
        "the frozen-data detector never fires",
        "and soc_age_min | float(0) >= soc_freeze_minutes | float(90) }}",
        "and false }}",
    ),
    (
        "a running session is cut off when GPS drops out",
        "    {{ car_home or (not location_known) or physically_present }}",
        "    {{ car_home }}",
    ),
    (
        "charging starts while the car is away",
        "and (allow_continue if switch_on else allow_start) }}",
        "}}",
    ),
    (
        "command throttling is bypassed",
        "  gap_elapsed: >-\n"
        "    {{ current_age | float(0) >= command_gap | float(60) and not run_is_a_race }}",
        '  gap_elapsed: "{{ true }}"',
    ),
    (
        "the window no longer spans midnight",
        "{% elif st > sp %}{% set inside = (n >= st or n < sp) %}",
        "{% elif st > sp %}{% set inside = (st <= n and n < sp) %}",
    ),
    (
        "an overnight window is attributed to the day it ends",
        "{% set ref = (n - timedelta(days=1)) if (st > sp and n < sp) else n %}",
        "{% set ref = n %}",
    ),
    (
        "current rounds down instead of up",
        "((c / cur_step) | round(0, 'ceil')) * cur_step",
        "((c / cur_step) | round(0, 'floor')) * cur_step",
    ),
    (
        "implausible voltage readings are accepted",
        "{% if live_voltage >= 175 and live_voltage <= 280 %}sensor",
        "{% if live_voltage >= 100 %}sensor",
    ),
    (
        "derived voltage has no upper sanity check",
        "{% elif derived_voltage >= 175 and derived_voltage <= 280 %}derived",
        "{% elif derived_voltage >= 175 %}derived",
    ),
    (
        "battery health is not checked against a sane range",
        "      {{ scaled if 50 <= scaled <= 130 else 100 }}",
        "      {{ scaled }}",
    ),
    (
        "battery health reported as a fraction is not rescaled",
        "      {% set scaled = (v * 100) if 0 < v < 2 else v %}",
        "      {% set scaled = v %}",
    ),
    (
        "the time reserve is not subtracted from the budget",
        "{% set sec = (sp - n).total_seconds() - (reserve_minutes | float(0)) * 60 %}",
        "{% set sec = (sp - n).total_seconds() %}",
    ),
    (
        "the deadband blocks reaching the boundary current",
        "\n        or (at_boundary and setpoint_differs))",
        ")",
    ),
    # ---- behaviour added or repaired in 1.1.0 ----
    (
        "the charge budget ignores the number of phases",
        "{% set raw = watts / ([voltage * phases_n, 1] | max) %}",
        "{% set raw = watts / ([voltage, 1] | max) %}",
    ),
    (
        "charging efficiency is ignored when sizing the budget",
        "      {{ ((delta / 100 * (effective_capacity | float(50)))\n"
        "          / ([efficiency | float(88), 1] | max / 100)) | round(4) }}",
        "      {{ (delta / 100 * (effective_capacity | float(50))) | round(4) }}",
    ),
    (
        "the budget is left in scientific notation, which Home Assistant "
        "hands back as a string",
        "      {{ ((delta / 100 * (effective_capacity | float(50)))\n"
        "          / ([efficiency | float(88), 1] | max / 100)) | round(4) }}",
        "      {{ (delta / 100 * (effective_capacity | float(50)))\n"
        "          / ([efficiency | float(88), 1] | max / 100) }}",
    ),
    (
        "the plan is stretched across the whole day outside the window",
        "{% elif emergency %}\n      {{ 0.0834 }}",
        "{% elif emergency %}\n      {{ 24 }}",
    ),
    (
        "an unreadable charger status is taken for a plugged-in cable",
        "{{ status_norm not in list_unplugged if status_known else switch_on }}",
        "{{ status_norm not in list_unplugged if status_known else true }}",
    ),
    (
        "charger statuses are compared case-sensitively",
        'status_norm: "{{ charger_status | lower }}"',
        'status_norm: "{{ charger_status }}"',
    ),
    (
        "a percentage sitting at the target counts as frozen data",
        "and switch_on and charging_now and not soc_at_target",
        "and switch_on and charging_now",
    ),
    (
        "losing the data mid-session cuts the current to the reserve figure",
        "{{ [fallback_current | float(10), current_now if switch_on else 0] | max }}",
        "{{ fallback_current | float(10) }}",
    ),
    (
        "a cumulative energy meter is mistaken for a session meter",
        "       and has_value(e_energy) and is_number(states(e_energy))\n"
        "       and energy_plausible }}",
        "       and has_value(e_energy) and is_number(states(e_energy)) }}",
    ),
    (
        "the emergency top-up stops dead on the threshold",
        "{{ soc < emergency_soc | float(0) + emergency_hysteresis | float(10) }}",
        "{{ soc < emergency_soc | float(0) }}",
    ),
    (
        # ``soc`` is -1 when the data is unusable, and -1 is below every
        # threshold - so without this guard a dead integration reads as a
        # critically low battery, and the emergency path is the one that
        # ignores the window and asks for the ceiling.
        "an emergency top-up fires on unusable charge data",
        "{% if emergency_soc | float(0) <= 0 or not soc_valid %}",
        "{% if emergency_soc | float(0) <= 0 %}",
    ),
    (
        "the mode of a hand-started session is overridden too",
        "{{ e_mode_select != '' and mode_value != '' and not foreign_session",
        "{{ e_mode_select != '' and mode_value != ''",
    ),
    (
        "the mode command ignores the pause between commands",
        "       and not is_state(e_mode_select, mode_value)\n"
        "       and not run_is_a_race\n"
        "       and (switch_gap_elapsed or not switch_on) }}",
        "       and not is_state(e_mode_select, mode_value) }}",
    ),
    (
        "the flag cleanup acts on the first flicker of the switch",
        "       and switch_gap_elapsed }}",
        "       }}",
    ),
    (
        "standby draw counts as current flowing",
        "      {{ states(e_power) | float(0) > no_power_threshold | float(200) }}",
        "      {{ states(e_power) | float(0) > 0 }}",
    ),
    (
        "a small battery makes its own session meter implausible",
        '  energy_sane_max: "{{ [effective_capacity | float(50) * 2, 20] | max }}"',
        '  energy_sane_max: "{{ effective_capacity | float(50) * 2 }}"',
    ),
    (
        "the log shows the uncapped calculation and reads like a breakage",
        '  calc_current_shown: "{{ [calc_current | float(0), num_max] | min | round(2) }}"',
        '  calc_current_shown: "{{ calc_current | float(0) | round(2) }}"',
    ),
    # ---- second review round ----
    (
        "swapped current bounds raise the ceiling instead of lowering the floor",
        '  lim_max: "{{ in_max_current | float(28) }}"\n'
        '  lim_min: "{{ [in_min_current | float(6), lim_max] | min }}"',
        '  lim_min: "{{ in_min_current | float(6) }}"\n'
        '  lim_max: "{{ [in_max_current | float(28), in_min_current | float(6)] | max }}"',
    ),
    (
        "a session meter is rejected whenever it overshoots a small target",
        "and states(e_energy) | float(0) <= energy_sane_max | float(100)",
        "and states(e_energy) | float(0) <= energy_target | float(0) * 3",
    ),
    (
        "finishing off after the window hammers the daytime tariff",
        "{% elif emergency %}\n      {{ 0.0834 }}\n    {% else %}\n      {{ 8 }}",
        "{% elif emergency %}\n      {{ 0.0834 }}\n    {% else %}\n      {{ 0.0834 }}",
    ),
    (
        "the watchdog is disabled whenever no power sensor is configured",
        "{{ watchdog_minutes | float(0) > 0 and switch_on and plugged_in\n"
        "       and (e_power == '' or has_value(e_power))",
        "{{ watchdog_minutes | float(0) > 0 and switch_on and plugged_in\n"
        "       and e_power != '' and has_value(e_power)",
    ),
    (
        "a charger that never switches on is commanded on every tick",
        "    {{ switch_off_confirmed and switch_gap_elapsed and not needs_write }}",
        "    {{ switch_off_confirmed and not needs_write }}",
    ),
    (
        "a hand-started session has its current overridden",
        "    {{ want_write and not foreign_session and status_known\n"
        "       and not run_is_a_race\n"
        "       and (gap_elapsed or not switch_on) }}",
        "    {{ want_write and status_known and not run_is_a_race\n"
        "       and (gap_elapsed or not switch_on) }}",
    ),
    (
        "the current is steered while the charger status is unreadable",
        "    {{ want_write and not foreign_session and status_known\n"
        "       and not run_is_a_race\n"
        "       and (gap_elapsed or not switch_on) }}",
        "    {{ want_write and not foreign_session\n"
        "       and not run_is_a_race\n"
        "       and (gap_elapsed or not switch_on) }}",
    ),
    (
        "a stop reason is reported when nothing is being stopped",
        "    {% if not must_stop %}none\n    {% elif charger_fault %}fault",
        "    {% if charger_fault %}fault",
    ),
    (
        "a hand-started session is switched off at the end of the window",
        "and (stop_regardless_of_owner or finishing_own_stop or not foreign_session)",
        "and true",
    ),
    (
        "a lost session flag disables every stop, not just the polite ones",
        "         stop_regardless_of_owner\n"
        "         or ((not in_window) and stop_at_window_end)",
        "         ((not in_window) and stop_at_window_end)",
    ),
    # ---- the actions block, reachable since run_actions() exists ----
    (
        "the charger is switched on before the current is set",
        "          - if:\n"
        "              - condition: template\n"
        '                value_template: "{{ needs_turn_on }}"\n',
        "          - if:\n"
        "              - condition: template\n"
        '                value_template: "{{ true }}"\n',
    ),
    (
        "the charger mode is never forced to manual",
        '                value_template: "{{ needs_mode_write }}"',
        '                value_template: "{{ false }}"',
    ),
    (
        "a blocking pause creeps back in front of the setpoint write",
        "            then:\n              - action: number.set_value\n"
        "                continue_on_error: true\n                target:\n"
        '                  entity_id: "{{ e_current }}"\n                data:\n'
        '                  value: "{{ desired_current }}"',
        "            then:\n              - delay:\n"
        '                  seconds: "{{ command_gap | int(60) }}"\n'
        "              - action: number.set_value\n"
        "                continue_on_error: true\n                target:\n"
        '                  entity_id: "{{ e_current }}"\n                data:\n'
        '                  value: "{{ desired_current }}"',
    ),
    (
        "an empty weekday list disables the automation for good",
        "{{ weekdays | count == 0\n       or ['mon','tue','wed','thu','fri','sat','sun']",
        "{{ ['mon','tue','wed','thu','fri','sat','sun']",
    ),
    (
        "a run that sends no command leaves no trace of why",
        "                Без действий: {{ verdict }} |",
        "                Без действий |",
    ),
    (
        "the verdict reports a later reason than the one that decided",
        "{% elif not charger_online %}станция офлайн",
        "{% elif false %}станция офлайн",
    ),
    (
        "the snapshot drifts from the values it claims to report",
        "        'уставка_нужна': desired_current,",
        "        'уставка_нужна': current_now,",
    ),
    (
        "a charger drawing no current still reports itself as healthy",
        "{% elif no_power_alarm %}включено, но ток не идёт ({{ alarm_reason }})\n    ",
        "",
    ),
    (
        "a lying tracker cuts a session that is visibly drawing current",
        "    {{ car_home or (not location_known) or physically_present }}",
        "    {{ car_home or (not location_known) }}",
    ),
    (
        "the deadband swallows a request to raise the current",
        "          > (rise_tolerance | float(0.5)\n"
        "             if (current_rising or not switch_on)\n"
        "             else [deadband | float(2), rise_tolerance | float(0.5)] | max)",
        "          > deadband | float(2)",
    ),
    (
        "a charger that quantises the setpoint is rewritten forever",
        'rise_tolerance: "{{ cur_step | float(1) / 2 }}"',
        'rise_tolerance: "{{ 0 }}"',
    ),
    (
        "the boundary clause forces writes at the floor as well as the ceiling",
        'at_boundary: "{{ desired_current >= num_max }}"',
        'at_boundary: "{{ desired_current >= num_max or desired_current <= num_min }}"',
    ),
    (
        "the setpoint and the switch-on go out in the same run",
        "    {{ switch_off_confirmed and switch_gap_elapsed and not needs_write }}",
        "    {{ switch_off_confirmed and switch_gap_elapsed }}",
    ),
    (
        "commands are sent to a switch that does not exist",
        "    {{ switch_present\n       and charger_online",
        "    {{ charger_online",
    ),
    (
        "a session flag left raised by a manual stop is never cleared",
        "    {{ e_session != '' and session_flag_readable and is_state(e_session, 'on')\n"
        "       and switch_off_confirmed and charger_online and status_known\n"
        "       and switch_gap_elapsed }}",
        "    {{ false }}",
    ),
    (
        "a blink resets the setpoint of a charging car to the minimum",
        "              {{ reset_current_on_stop and switch_off_confirmed and charger_online",
        "              {{ reset_current_on_stop and switch_present and not switch_on",
    ),
    (
        "an unavailable switch counts as a switch that was turned off",
        "  switch_off_confirmed: \"{{ switch_present and is_state(e_switch, 'off') }}\"",
        '  switch_off_confirmed: "{{ not switch_on }}"',
    ),
    (
        "a session flag lost mid-charge is never recovered",
        '                value_template: "{{ session_lost }}"',
        '                value_template: "{{ false }}"',
    ),
    # Mutating the ``session_flag_readable`` term out of ``foreign_session``
    # would be an equivalent mutation: an unreadable flag already makes
    # ``session_owned`` true, so both spellings agree on every input. The term
    # is kept in the blueprint for readability, and the guard is pinned here
    # instead, where it actually decides something.
    (
        "an unreadable session flag makes the session a stranger's",
        "    {{ e_session == '' or not session_flag_readable or is_state(e_session, 'on') }}",
        "    {{ e_session == '' or is_state(e_session, 'on') }}",
    ),
    (
        "a problem sensor is trusted while the charger is offline",
        "       or (e_problem != '' and charger_online and status_known\n"
        "           and has_value(e_problem) and is_state(e_problem, 'on')) }}",
        "       or (e_problem != '' and is_state(e_problem, 'on')) }}",
    ),
    (
        "an emergency top-up runs on through the day once it has recovered",
        "    {{ switch_on and not stop_at_window_end and session_started_in_window }}",
        "    {{ switch_on and not stop_at_window_end }}",
    ),
    (
        "the start notification repeats on every retry",
        '                    value_template: "{{ first_turn_on_attempt }}"',
        '                    value_template: "{{ true }}"',
    ),
    (
        "the verdict and the stop reason rank causes differently",
        "    {% elif car_left and (switch_on or (in_window and not target_reached"
        " and plugged_in)) %}машина не дома\n"
        "    {% elif not plugged_in %}кабель не подключён",
        "    {% elif not plugged_in %}кабель не подключён\n"
        "    {% elif car_left and (switch_on or (in_window and not target_reached"
        " and plugged_in)) %}машина не дома",
    ),
    (
        "an unreadable status reports no cause when it does stop the session",
        "    {% elif not status_known %}status_unknown\n",
        "",
    ),
    # ---- behaviour added or repaired in 1.4.0 ----
    (
        "a swallowed turn-on is never retried before the next recalculation",
        '  - trigger: state\n'
        '    entity_id: !input charger_switch\n'
        '    to: "off"\n'
        "    for:\n"
        "      seconds: !input command_gap\n"
        "    id: command_missed\n",
        "",
    ),
    (
        "a swallowed setpoint write is never retried",
        "  - trigger: state\n"
        "    entity_id: !input charger_current_number\n"
        '    not_to: ["unknown", "unavailable"]\n'
        "    for:\n"
        "      seconds: !input command_gap\n"
        "    id: current_written\n",
        "",
    ),
    # ---- behaviour added or repaired in 1.4.1 ----
    (
        "a rise is written even when the charge percentage is stale",
        "\n       and (not current_rising or not switch_on or soc_fresh_enough_to_rise)",
        "",
    ),
    (
        "the staleness guard also blocks lowering the current",
        "       and (not current_rising or not switch_on or soc_fresh_enough_to_rise)",
        "       and soc_fresh_enough_to_rise",
    ),
    (
        # The parentheses that keep the ceiling exception inside the guard.
        # Without them `and` binds tighter than `or`, the exception becomes a
        # clause of its own, and a ramp on a frozen percentage walks straight
        # to the ceiling - exactly what the third night recorded.
        "the ceiling exception escapes the staleness guard",
        "    {{ ((desired_current - current_now) | abs\n"
        "          > (rise_tolerance | float(0.5)\n"
        "             if (current_rising or not switch_on)\n"
        "             else [deadband | float(2), rise_tolerance | float(0.5)] | max)\n"
        "        or (at_boundary and setpoint_differs))\n"
        "       and (not current_rising or not switch_on or soc_fresh_enough_to_rise) }}",
        "    {{ (desired_current - current_now) | abs\n"
        "          > (rise_tolerance | float(0.5)\n"
        "             if (current_rising or not switch_on)\n"
        "             else [deadband | float(2), rise_tolerance | float(0.5)] | max)\n"
        "       and (not current_rising or not switch_on or soc_fresh_enough_to_rise)\n"
        "        or (at_boundary and setpoint_differs) }}",
    ),
    (
        "a cumulative meter passes once its counter has been reset",
        "    {{ switch_off_confirmed and charger_online and not charging_now\n"
        "       and states(e_energy) | float(0) > "
        "[effective_capacity | float(50) * 0.1, 2] | max }}",
        "    {{ false }}",
    ),
    (
        "the arrival trigger fires on every coordinate update",
        "  - trigger: template\n    id: car_arrived\n    for:\n      seconds: 30\n",
        "  - trigger: template\n    id: car_arrived\n",
    ),
    # ---- behaviour repaired in 1.4.3 ----
    (
        # The fifth night: the tracker lied from 06:07 to the end, and once
        # the charge stopped there was no current left to contradict it.
        "a wandering tracker explains a session that already finished",
        "    {% elif car_left and (switch_on or (in_window and not target_reached"
        " and plugged_in)) %}машина не дома",
        "    {% elif car_left %}машина не дома",
    ),
    (
        # The other half: an absent car really is why no session starts.
        "an absent car stops explaining why the charge never began",
        "    {% elif car_left and (switch_on or (in_window and not target_reached"
        " and plugged_in)) %}машина не дома",
        "    {% elif car_left and switch_on %}машина не дома",
    ),
    (
        "the planned current carries no headroom over what it computed",
        "      {{ (raw * (1 + [headroom_pct | float(0), 0] | max / 100)) | round(3) }}",
        "      {{ raw | round(3) }}",
    ),
    # ---- behaviour repaired in 1.4.4 ----
    (
        # The sixth night: window_start and tick fired 204 ms apart and both
        # reached number.set_value, because the setpoint entity had not moved
        # yet and every entity-based check said the gap was served.
        "two triggers firing together each send the same command",
        "  run_is_a_race: >-\n"
        "    {{ command_gap | float(60) > 0 and run_age | float(999999) < 1 }}",
        '  run_is_a_race: "{{ false }}"',
    ),
    (
        # The guard must not fire on a fresh install, where last_triggered is
        # present but None and the naive subtraction raises.
        "a never-triggered automation reads as having just run",
        "    {{ 999999 if lt is none else (now() - lt).total_seconds() | round(3) }}",
        "    {{ 0 if lt is none else (now() - lt).total_seconds() | round(3) }}",
    ),
    (
        # The start-of-session exception bypasses the throttle entirely, and
        # the race happened in exactly that branch.
        "the race guard is bypassed while the charger is still off",
        "       and not run_is_a_race\n"
        "       and (gap_elapsed or not switch_on) }}",
        "       and (gap_elapsed or not switch_on or run_is_a_race) }}",
    ),
    (
        # The sixth night again: branch 2 lowers the flag before the station
        # confirms off, and the blueprint saw its own stop as a stranger's.
        "our own half-finished stop is left to a stranger's rules",
        "  finishing_own_stop: >-\n"
        "    {{ switch_on and not switch_gap_elapsed and session_flag_readable\n"
        "       and not session_owned }}",
        '  finishing_own_stop: "{{ false }}"',
    ),
    (
        # And the protection it must not swallow: a real manual session has an
        # old switch, so the age is what tells the two apart.
        "a freshly flipped manual switch counts as our own stop",
        "    {{ switch_on and not switch_gap_elapsed and session_flag_readable\n"
        "       and not session_owned }}",
        "    {{ switch_on and session_flag_readable and not session_owned }}",
    ),
    (
        # A dropout takes the switch to unavailable, not to off, and the gentle
        # finish used to lift its cap along with it — at 99% charge.
        "a dropout lifts the gentle finish along with the switch",
        "    {% if gentle_finish_active and not switch_off_confirmed\n"
        "          and current_now | float(0) >= num_min %}",
        "    {% if gentle_finish_active and switch_on\n"
        "          and current_now | float(0) >= num_min %}",
    ),
    (
        "the gentle finish also blocks lowering the current",
        "      {{ [bounded, current_now | float(0)] | min | round(2) }}",
        "      {{ current_now | float(0) | round(2) }}",
    ),
    (
        "the gentle finish holds the current down during an emergency top-up",
        "    {{ gentle_finish_soc | float(100) < 100\n"
        "       and not emergency and not cold_mode",
        "    {{ gentle_finish_soc | float(100) < 100",
    ),
    (
        "a dropout counts as proof the car is no longer plugged in",
        '  physically_present: "{{ charging_now and not switch_off_confirmed }}"',
        '  physically_present: "{{ charging_now and not switch_on }}"',
    ),
    (
        "the shortfall is measured while the setpoint is still settling",
        "          or current_now | float(0) <= 0 or actual_current | float(0) <= 0.5\n"
        "          or not gap_elapsed %}",
        "          or current_now | float(0) <= 0 or actual_current | float(0) <= 0.5 %}",
    ),
    (
        "the snapshot claims commands that no branch can send",
        "        'записать_ток': needs_write and should_charge,",
        "        'записать_ток': needs_write,",
    ),
    (
        "the stop entry drops the evidence the stop was judged on",
        "                    трекер={{ tracker_state }}"
        "{{ ' (дома)' if car_home else '' }},\n",
        "",
    ),
]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory) -> Path:
    """A throwaway copy of the repository, so mutations never touch the original."""
    target = tmp_path_factory.mktemp("mutation-sandbox") / "repo"
    shutil.copytree(
        REPO_ROOT,
        target,
        # ``.direnv`` and ``_tmp`` are the expensive ones: a direnv environment
        # runs to hundreds of megabytes and downloaded traces are not small
        # either. Copying them made every mutation run drag and produced
        # spurious cleanup warnings from paths pytest could not remove.
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "*.pyc",
            ".direnv", ".venv", "venv", "_tmp", "htmlcov", ".coverage",
        ),
    )
    return target


def _run_suite(sandbox: Path) -> int:
    """Run the suite inside the sandbox, excluding this file, and count failures.

    Deliberately single-process. The outer job is already parallel (one worker
    per mutation), so spawning workers here too would oversubscribe the machine
    and make each inner run slower, not faster. ``-x`` is the real saving: we
    only need to know *whether* the suite noticed, and a caught mutation usually
    fails within the first seconds instead of grinding through all 700 tests.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-x",
            "-p", "no:cacheprovider",
            "--deselect", "tests/test_mutations.py",
            "tests/",
        ],
        capture_output=True,
        text=True,
        cwd=sandbox,
        check=False,
    )
    match = re.search(r"(\d+) failed", result.stdout)
    return int(match.group(1)) if match else 0


@pytest.mark.parametrize(
    "label,original,corrupted", MUTATIONS, ids=[m[0] for m in MUTATIONS]
)
def test_every_mutation_still_matches_the_blueprint(label, original, corrupted):
    """A mutation whose fragment has drifted out of the blueprint applies to
    nothing, and the run then "passes" while testing nothing at all. That has
    happened more than once after refactoring, so the check runs with the fast
    tests rather than only inside the ten-minute mutation job.

    Matching exactly once also matters: ``str.replace(..., 1)`` would silently
    corrupt the first of several occurrences, which is rarely the intended one.
    """
    source = BLUEPRINT_PATH.read_text(encoding="utf-8")
    assert source.count(original) == 1, (
        f"mutation {label!r} matches {source.count(original)} places - update it"
    )
    assert corrupted != original


@pytest.mark.slow
@pytest.mark.parametrize(
    "label,original,corrupted", MUTATIONS, ids=[m[0] for m in MUTATIONS]
)
def test_the_suite_detects_a_broken_blueprint(
    sandbox, label, original, corrupted
):
    blueprint = (
        sandbox / "blueprints" / "automation" / "ev_smart_charging"
        / "ev_smart_charging.yaml"
    )
    pristine = blueprint.read_text(encoding="utf-8")
    assert original in pristine, (
        f"mutation {label!r} no longer matches the blueprint - update it"
    )

    blueprint.write_text(pristine.replace(original, corrupted, 1), encoding="utf-8")
    try:
        failures = _run_suite(sandbox)
    finally:
        blueprint.write_text(pristine, encoding="utf-8")

    assert failures > 0, f"nothing failed when {label} - this behaviour is untested"


@pytest.mark.slow
def test_the_unmutated_suite_is_green_inside_the_sandbox(sandbox):
    """Guards against the sandbox itself being broken, which would make every
    mutation look caught."""
    assert _run_suite(sandbox) == 0
