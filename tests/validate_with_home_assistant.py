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
        from homeassistant.components.blueprint.models import Blueprint
        from homeassistant.util.yaml import parse_yaml
    except ImportError as err:  # pragma: no cover - environment dependent
        print(f"Home Assistant is not importable: {err}")
        print("Install it with: pip install homeassistant")
        return 77  # skip

    raw = BLUEPRINT.read_text(encoding="utf-8")
    data = parse_yaml(raw)

    blueprint = Blueprint(data, expected_domain="automation")
    print(f"Blueprint name:  {blueprint.name}")
    print(f"Domain:          {blueprint.domain}")
    print(f"Inputs declared: {len(blueprint.inputs)}")

    missing = blueprint.placeholders - set(blueprint.inputs)
    if missing:
        print(f"FAIL: placeholders without a declared input: {sorted(missing)}")
        return 1

    unused = set(blueprint.inputs) - blueprint.placeholders
    if unused:
        print(f"FAIL: inputs that are never referenced: {sorted(unused)}")
        return 1

    print("OK: the blueprint satisfies the Home Assistant schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
