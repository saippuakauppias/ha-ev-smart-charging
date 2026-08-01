"""Mutation testing: do the tests actually catch a broken blueprint?

A green suite proves nothing on its own - tests can be vacuous. This file
deliberately corrupts one piece of the blueprint at a time and asserts that the
rest of the suite notices. Each mutation corresponds to a promise made in the
README, so a surviving mutation means that promise is untested.

This is the check that found the missing outer clamp on the current: the value
was clamped twice, and the inner clamp masked the outer one for every ceiling
that happened to be a multiple of the step.

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
        "current is not clamped after snapping to the step",
        "{{ [[snapped, num_min] | max, num_max] | min | round(2) }}",
        "{{ [snapped, num_min] | max | round(2) }}",
    ),
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
        "{{ allow_start or (not location_known) }}",
        "{{ allow_start }}",
    ),
    (
        "charging starts while the car is away",
        "and (allow_continue if switch_on else allow_start) }}",
        "}}",
    ),
    (
        "command throttling is bypassed",
        "{{ [command_gap | float(60) - current_age | float(0), 0] | max | int }}",
        "{{ 0 }}",
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
        "{% if live_voltage >= 175 %}sensor",
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
        "a manually started session is cut off at the end of the window",
        "or ((not in_window) and stop_at_window_end and session_owned)",
        "or ((not in_window) and stop_at_window_end)",
    ),
    (
        "the time reserve is not subtracted from the budget",
        "{% set sec = (sp - now()).total_seconds() "
        "- (reserve_minutes | float(0)) * 60 %}",
        "{% set sec = (sp - now()).total_seconds() %}",
    ),
    (
        "the deadband blocks reaching the boundary current",
        "or (at_boundary and desired_current != current_now) }}",
        "}}",
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
