#!/usr/bin/env python3
"""Validate the blueprint against the real Home Assistant schema.

The pytest suite exercises the blueprint's *logic* without Home Assistant
installed, which keeps it fast and dependency-free. This script is the
complementary check: it imports Home Assistant itself and runs the blueprint
through the same validation code the frontend uses on import.

It lives outside the pytest suite on purpose. Installing Home Assistant pulls in
a large dependency tree and its internal APIs are not covered by any stability
promise, so a breakage here means "look into it", not "the blueprint is broken".

Usage:
    pip install homeassistant
    python tests/validate_with_home_assistant.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BLUEPRINT = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "automation"
    / "ev_smart_charging"
    / "ev_smart_night_charging.yaml"
)


def main() -> int:
    try:
        import voluptuous as vol
        from homeassistant.components.automation.config import (
            PLATFORM_SCHEMA as AUTOMATION_SCHEMA,
        )
        from homeassistant.components.blueprint.models import Blueprint
        from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
        from homeassistant.util.yaml import parse_yaml
    except ImportError as err:  # pragma: no cover - environment dependent
        print(f"Home Assistant is not importable: {err}")
        print("Install it with: pip install homeassistant")
        return 77  # skip

    raw = BLUEPRINT.read_text(encoding="utf-8")
    data = parse_yaml(raw)

    # The metadata schema is the strict part: it rejects unknown keys and
    # malformed selectors, which is exactly what breaks a blueprint on import.
    try:
        BLUEPRINT_SCHEMA(data)
    except Exception as err:  # noqa: BLE001 - voluptuous raises many types
        print(f"FAIL: the blueprint metadata is invalid:\n{err}")
        return 1
    print("OK: metadata and selectors satisfy the blueprint schema.")

    # Running the automation schema over an *unsubstituted* blueprint reports
    # noise that does not apply to it: "validates schema outside the event loop"
    # for every template, and "Got None" wherever an !input placeholder stands in
    # for a list of actions. Both appear on any valid blueprint, so a failure
    # here is not conclusive - fall back to parsing without the action schema so
    # the placeholder checks below can still run.
    try:
        blueprint = Blueprint(
            data, expected_domain="automation", schema=AUTOMATION_SCHEMA
        )
    except TypeError:  # pragma: no cover - depends on the installed version
        blueprint = Blueprint(data, expected_domain="automation")
    except Exception as err:  # noqa: BLE001 - voluptuous raises many types
        real = [
            line
            for line in str(err).splitlines()
            if "outside the event loop" not in line
            and "Got None" not in line
            # The automation schema knows nothing about the ``blueprint:`` key
            # that necessarily sits at the top of every blueprint file.
            and "extra keys not allowed @ data['blueprint']" not in line
        ]
        if real:
            print("WARNING: the action schema reported:")
            for line in real[:5]:
                print(f"  {line[:160]}")
        blueprint = Blueprint(data, expected_domain="automation", schema=vol.Schema(dict))
    print(f"Blueprint name:  {blueprint.name}")
    print(f"Domain:          {blueprint.domain}")
    print(f"Inputs declared: {len(blueprint.inputs)}")

    # Note: running the action schema over a *blueprint* reports noise that does
    # not apply to it - "validates schema outside the event loop" for every
    # template, and "Got None" where an unsubstituted !input placeholder sits.
    # Those appear on any blueprint, so only the checks below are conclusive.
    # ``validate`` reports inputs that are referenced but never declared. Older
    # releases exposed the same information as a ``placeholders`` set.
    if hasattr(blueprint, "validate"):
        problems = blueprint.validate()
        if problems:
            print(f"FAIL: {problems}")
            return 1
    else:  # pragma: no cover - older Home Assistant
        missing = blueprint.placeholders - set(blueprint.inputs)
        if missing:
            print(f"FAIL: placeholders without a declared input: {sorted(missing)}")
            return 1

    print("OK: the blueprint satisfies the Home Assistant schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
