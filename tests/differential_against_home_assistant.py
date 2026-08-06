#!/usr/bin/env python3
"""Render the same templates through ``ha_sim`` and through real Home Assistant.

``ha_sim`` is a reimplementation, and a reimplementation drifts. The danger is
one-sided: an emulator *stricter* than Home Assistant costs a puzzled hour, an
emulator *softer* than Home Assistant turns a green suite into a night without
charging. That is not hypothetical - ``is_state`` on a missing entity used to
answer ``True`` here and ``False`` in Home Assistant.

``validate_with_home_assistant.py`` is the sibling of this script and checks a
different thing: that the document's *structure* (metadata, selectors, declared
inputs) satisfies the schema the frontend applies on import. It never renders a
template. This script never looks at the schema. Neither subsumes the other.

Lives outside the pytest suite for the same reason as its sibling: Home
Assistant is a large dependency whose internal APIs carry no stability promise,
so a failure here means "look into it", not "the blueprint is broken".

Usage:
    pip install homeassistant
    python tests/differential_against_home_assistant.py

Exit codes: 0 agreement, 1 divergence, 77 Home Assistant not installed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

TZ = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime(2026, 8, 5, 3, 0, tzinfo=TZ)

#: ``entity_id -> (state, attributes)``. Deliberately includes the states that
#: only ever show up when something is broken.
WORLD: dict[str, tuple[str, dict[str, Any]]] = {
    "sensor.power": ("3700", {}),
    "sensor.zero_power": ("0", {}),
    "sensor.soc": ("42.5", {}),
    "sensor.soc_frac": ("0.98", {}),
    "sensor.unavailable": ("unavailable", {}),
    "sensor.unknown": ("unknown", {}),
    "sensor.empty": ("", {}),
    "sensor.text": ("charging", {}),
    "sensor.upper": ("CHARGING", {}),
    "sensor.sci": ("4.9e-05", {}),
    "sensor.leading_zero": ("007", {}),
    "sensor.trailing_dot": ("1.", {}),
    "sensor.inf": ("inf", {}),
    "sensor.nan": ("nan", {}),
    "switch.charger": ("on", {}),
    "switch.off_charger": ("off", {}),
    "number.setpoint": ("16.0", {"min": 6.0, "max": 32.0, "step": 1.0}),
}

#: Every template is rendered by both engines and the results compared. These
#: are the constructs the blueprint actually relies on, plus the edge cases the
#: project has been burned by before.
TEMPLATES: list[str] = [
    # --- states() and friends -----------------------------------------------
    "{{ states('sensor.power') }}",
    "{{ states('sensor.missing') }}",
    "{{ states('sensor.empty') }}",
    "{{ states('sensor.unavailable') }}",
    "{{ state_attr('number.setpoint', 'min') }}",
    "{{ state_attr('number.setpoint', 'nope') }}",
    "{{ state_attr('sensor.missing', 'min') }}",
    # --- is_state: the one that had drifted ---------------------------------
    "{{ is_state('switch.charger', 'on') }}",
    "{{ is_state('switch.charger', 'off') }}",
    "{{ is_state('sensor.missing', 'unknown') }}",
    "{{ is_state('sensor.missing', 'unavailable') }}",
    "{{ is_state('sensor.unavailable', 'unavailable') }}",
    "{{ is_state('switch.charger', ['on', 'off']) }}",
    "{{ is_state('switch.charger', ['off', 'x']) }}",
    # --- has_value ----------------------------------------------------------
    "{{ has_value('sensor.power') }}",
    "{{ has_value('sensor.unavailable') }}",
    "{{ has_value('sensor.unknown') }}",
    "{{ has_value('sensor.missing') }}",
    "{{ has_value('sensor.empty') }}",
    # --- is_number: HA rejects inf and nan ----------------------------------
    "{{ is_number(states('sensor.soc')) }}",
    "{{ is_number(states('sensor.text')) }}",
    "{{ is_number(states('sensor.inf')) }}",
    "{{ is_number(states('sensor.nan')) }}",
    "{{ is_number(states('sensor.empty')) }}",
    "{{ is_number(states('sensor.sci')) }}",
    # --- float/int: HA raises without a default -----------------------------
    "{{ states('sensor.soc') | float(0) }}",
    "{{ states('sensor.text') | float(-1) }}",
    "{{ states('sensor.missing') | float(9e9) }}",
    "{{ states('sensor.empty') | float(0) }}",
    "{{ states('sensor.inf') | float(0) }}",
    "{{ states('sensor.setpoint') | int(1) }}",
    "{{ states('sensor.text') | int(1) }}",
    # --- the coercion rule that cost a whole night --------------------------
    "{{ 4.886363636525847e-05 }}",
    "{{ 0.001 / 20 }}",
    "{{ (0.001 / 20) | round(4) }}",
    "{{ states('sensor.sci') }}",
    "{{ states('sensor.leading_zero') }}",
    "{{ states('sensor.trailing_dot') }}",
    "{{ 1.0 }}",
    "{{ 42 }}",
    "{{ '007' }}",
    # --- rounding modes used by the current calculation ---------------------
    "{{ 27.4 | round(0) }}",
    "{{ 27.4 | round(0, 'ceil') }}",
    "{{ 27.6 | round(0, 'floor') }}",
    "{{ 27.456 | round(2) }}",
    "{{ (28 / 5) | round(0, 'floor') * 5 }}",
    "{{ (26.1 / 1) | round(0, 'ceil') * 1 }}",
    # --- list/min/max/filters the blueprint leans on ------------------------
    "{{ [6, 28] | max }}",
    "{{ [6, 28] | min }}",
    "{{ [[16, 6] | max, 28] | min }}",
    "{{ 'A, B , ,c' | lower }}",
    "{{ ('CHARGING, charged' | string).lower().split(',') | map('trim') "
    "| reject('eq', '') | list }}",
    "{{ 'charging' in ['charging', 'charged'] }}",
    "{{ ['a'] | join('') }}",
    "{{ [] | join('') }}",
    "{{ [] | count == 0 }}",
    "{{ (-3) | abs }}",
    "{{ 'zone.home'.split('.') | last | replace('_', ' ') }}",
    "{{ none | default('', true) }}",
    "{{ '' | default('x', true) }}",
    # --- time arithmetic ----------------------------------------------------
    "{{ today_at('06:55:00') > today_at('23:05:00') }}",
    "{{ (today_at('06:55:00') - today_at('03:00:00')).total_seconds() }}",
    "{{ (timedelta(days=1)).total_seconds() }}",
]


def _ha_sim_render(template: str) -> Any:
    # ``State`` comes from ``ha_sim`` and not from ``conftest``, even though the
    # latter re-exports it: importing ``conftest`` pulls in pytest, which this
    # script must not need. It runs in a job that installs Home Assistant and
    # nothing else, and a missing pytest turned every single comparison into
    # "<raises ModuleNotFoundError>" - 63 identical failures that looked like
    # total engine divergence rather than one absent package.
    import ha_sim
    from ha_sim import State

    world = {
        entity: State(state, attrs, NOW - dt.timedelta(hours=1))
        for entity, (state, attrs) in WORLD.items()
    }
    helpers = ha_sim._build_helpers(world, NOW)
    return ha_sim._render(template, helpers, {})


async def _ha_render_all(templates: list[str]) -> dict[str, Any]:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import template as ha_tpl

    hass = HomeAssistant("/tmp/ha-differential-config")
    try:
        for entity, (state, attrs) in WORLD.items():
            hass.states.async_set(entity, state, attrs)
        results: dict[str, Any] = {}
        for tpl in templates:
            try:
                results[tpl] = ha_tpl.Template(tpl, hass).async_render()
            except Exception as err:  # noqa: BLE001 - any failure is a result
                results[tpl] = f"<raises {type(err).__name__}>"
        return results
    finally:
        await hass.async_stop()


def _normalise(value: Any) -> Any:
    """Ignore differences that carry no meaning for the blueprint.

    ``True`` and ``1`` compare equal in Python but are different answers here,
    so booleans are tagged. Numeric types are not: ``28`` and ``28.0`` drive the
    charger identically, and Jinja's own int/float choice is incidental.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", round(float(value), 9))
    return ("other", value)


def main() -> int:
    try:
        import homeassistant  # noqa: F401
    except ImportError as err:
        print(f"Home Assistant is not importable: {err}")
        print("Install it with: pip install homeassistant")
        return 77

    # Load the emulator once, before any comparison. A missing dependency here
    # is an environment problem, not a divergence, and it has to be said so:
    # the per-template ``except`` below would otherwise turn one absent package
    # into 63 identical "<raises ModuleNotFoundError>" rows that read like the
    # two engines disagreeing about everything.
    try:
        _ha_sim_render("{{ 1 }}")
    except ModuleNotFoundError as err:
        print(f"ha_sim could not be loaded: no module named {err.name!r}")
        print("The emulator needs its own dependencies, not just Home Assistant:")
        print("    pip install -r requirements-dev.txt")
        return 2

    ha_results = asyncio.run(_ha_render_all(TEMPLATES))

    divergences: list[tuple[str, Any, Any]] = []
    for tpl in TEMPLATES:
        try:
            sim = _ha_sim_render(tpl)
        except Exception as err:  # noqa: BLE001 - any failure is a result
            sim = f"<raises {type(err).__name__}>"
        real = ha_results[tpl]
        if _normalise(sim) != _normalise(real):
            divergences.append((tpl, sim, real))

    print(f"templates compared: {len(TEMPLATES)}")
    if not divergences:
        print("OK: ha_sim agrees with Home Assistant on every template.")
        return 0

    print(f"FAIL: {len(divergences)} divergence(s) between ha_sim and Home Assistant\n")
    for tpl, sim, real in divergences:
        print(f"  template: {tpl}")
        print(f"    ha_sim:         {sim!r} ({type(sim).__name__})")
        print(f"    home assistant: {real!r} ({type(real).__name__})")
    print(
        "\nAn emulator SOFTER than Home Assistant is the dangerous direction:\n"
        "the suite stays green and the blueprint breaks at night."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
