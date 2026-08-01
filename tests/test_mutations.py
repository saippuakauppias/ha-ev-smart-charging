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
        "{{ car_home or (not location_known) }}",
        "{{ car_home }}",
    ),
    (
        "charging starts while the car is away",
        "and (allow_continue if switch_on else allow_start) }}",
        "}}",
    ),
    (
        "command throttling is bypassed",
        '  gap_elapsed: "{{ current_age | float(0) >= command_gap | float(60) }}"',
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
        "battery health is not clamped to a sane range",
        "{{ [[states(e_soh) | float(100), 50] | max, 130] | min }}",
        "{{ states(e_soh) | float(100) }}",
    ),
    (
        "the time reserve is not subtracted from the budget",
        "{% set sec = (sp - n).total_seconds() - (reserve_minutes | float(0)) * 60 %}",
        "{% set sec = (sp - n).total_seconds() %}",
    ),
    (
        "the deadband blocks reaching the boundary current",
        "or (at_boundary and setpoint_differs) }}",
        "}}",
    ),
    # ---- behaviour added or repaired in 1.1.0 ----
    (
        "the charge budget ignores the number of phases",
        "{{ (watts / ([voltage * phases_n, 1] | max)) | round(3) }}",
        "{{ (watts / ([voltage, 1] | max)) | round(3) }}",
    ),
    (
        "charging efficiency is ignored when sizing the budget",
        "{{ (delta / 100 * (effective_capacity | float(50))) "
        "/ ([efficiency | float(88), 1] | max / 100) }}",
        "{{ (delta / 100 * (effective_capacity | float(50))) }}",
    ),
    (
        "the plan is stretched across the whole day outside the window",
        "{% elif emergency %}\n      {{ 0.0834 }}",
        "{% elif emergency %}\n      {{ 24 }}",
    ),
    (
        "an unreadable charger status is taken for a plugged-in cable",
        "{{ charger_status not in list_unplugged if status_known else switch_on }}",
        "{{ charger_status not in list_unplugged if status_known else true }}",
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
        "and states(e_energy) | float(0) <= energy_sane_max | float(100) }}",
        "and states(e_energy) | float(0) <= energy_target | float(0) * 3 }}",
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
        '  needs_turn_on: "{{ not switch_on and switch_gap_elapsed }}"',
        '  needs_turn_on: "{{ not switch_on }}"',
    ),
    (
        "a hand-started session has its current overridden",
        "{{ want_write and not foreign_session and (gap_elapsed or not switch_on) }}",
        "{{ want_write and (gap_elapsed or not switch_on) }}",
    ),
    (
        "a hand-started session is switched off at the end of the window",
        "and (charger_fault or not foreign_session)",
        "and true",
    ),
    # ---- the actions block, reachable since run_actions() exists ----
    (
        "the charger is switched on before the current is set",
        "          # 3. включение\n"
        "          - if:\n"
        "              - condition: template\n"
        '                value_template: "{{ needs_turn_on }}"\n'
        "            then:\n"
        "              - action: switch.turn_on",
        "          # 3. включение\n"
        "          - if:\n"
        "              - condition: template\n"
        '                value_template: "{{ true }}"\n'
        "            then:\n"
        "              - action: switch.turn_on",
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
        "{% if not charger_online %}станция офлайн",
        "{% if false %}станция офлайн",
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
]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory) -> Path:
    """A throwaway copy of the repository, so mutations never touch the original."""
    target = tmp_path_factory.mktemp("mutation-sandbox") / "repo"
    shutil.copytree(
        REPO_ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "*.pyc"
        ),
    )
    return target


def _run_suite(sandbox: Path) -> int:
    """Run the suite inside the sandbox, excluding this file, and count failures."""
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
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


@pytest.mark.slow
@pytest.mark.parametrize(
    "label,original,corrupted", MUTATIONS, ids=[m[0] for m in MUTATIONS]
)
def test_the_suite_detects_a_broken_blueprint(
    sandbox, label, original, corrupted
):
    blueprint = (
        sandbox / "blueprints" / "automation" / "ev_smart_charging"
        / "ev_smart_night_charging.yaml"
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
