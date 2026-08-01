"""Structural checks on the blueprint document itself.

These catch the classes of mistake that are easy to make while editing a large
YAML file by hand: an input that is declared but never wired up, a default that
falls outside its own selector range, a leftover debugging artefact.
"""

from __future__ import annotations

import re

import pytest
from ha_sim import InputRef

SELECTOR_TYPES = {
    "entity", "number", "boolean", "text", "time", "select", "action", "target",
    "duration", "icon", "device", "area", "object",
}


def test_document_has_expected_top_level_keys(blueprint):
    keys = set(blueprint.document)
    assert {"blueprint", "mode", "triggers", "variables", "actions"} <= keys


def test_domain_is_automation(blueprint):
    assert blueprint.meta["domain"] == "automation"


def test_minimum_home_assistant_version_is_declared(blueprint):
    assert blueprint.meta["homeassistant"]["min_version"] == "2024.10.0"


def test_mode_is_restart(blueprint):
    """A fresh trigger must cancel any pending throttling delay."""
    assert blueprint.document["mode"] == "restart"


def test_every_declared_input_is_used(blueprint):
    unused = set(blueprint.inputs) - blueprint.referenced_inputs()
    assert not unused, f"declared but never referenced: {sorted(unused)}"


def test_every_referenced_input_is_declared(blueprint):
    undeclared = blueprint.referenced_inputs() - set(blueprint.inputs)
    assert not undeclared, f"referenced but never declared: {sorted(undeclared)}"


def test_only_hard_requirements_lack_defaults(blueprint):
    """Everything optional must be pre-filled so the blueprint imports cleanly."""
    assert blueprint.required_inputs() == {
        "charger_switch",
        "charger_current_number",
        "charger_status_sensor",
    }


def test_all_inputs_live_inside_a_section(blueprint):
    top_level = set(blueprint.meta["input"]) - set(blueprint.sections)
    assert not top_level, f"inputs outside any section: {sorted(top_level)}"


def test_every_input_has_a_name_and_selector(blueprint):
    for name, schema in blueprint.inputs.items():
        assert "name" in schema, f"{name} has no display name"
        assert "selector" in schema, f"{name} has no selector"
        selector_type = next(iter(schema["selector"]))
        assert selector_type in SELECTOR_TYPES, f"{name}: odd selector {selector_type}"


def test_numeric_defaults_fall_inside_their_selector_range(blueprint):
    for name, schema in blueprint.inputs.items():
        selector = schema["selector"].get("number")
        if selector is None or "default" not in schema:
            continue
        value = schema["default"]
        assert selector["min"] <= value <= selector["max"], (
            f"{name}: default {value} outside [{selector['min']}, {selector['max']}]"
        )


def test_select_defaults_are_valid_options(blueprint):
    for name, schema in blueprint.inputs.items():
        selector = schema["selector"].get("select")
        if selector is None or "default" not in schema:
            continue
        options = {
            option["value"] if isinstance(option, dict) else option
            for option in selector["options"]
        }
        default = schema["default"]
        values = default if isinstance(default, list) else [default]
        assert set(values) <= options, f"{name}: default not among options"


def test_optional_entity_inputs_default_to_empty_list(blueprint):
    """``| join('')`` in the templates relies on this convention."""
    for name, schema in blueprint.inputs.items():
        if "entity" not in schema["selector"] or "default" not in schema:
            continue
        default = schema["default"]
        if isinstance(default, str):
            # Only the zone picker is allowed a concrete default.
            assert name == "home_zone", f"{name} defaults to a hard-coded entity"
        else:
            assert default == [], f"{name} should default to []"


def test_every_trigger_has_an_id(blueprint):
    for trigger in blueprint.triggers:
        assert "id" in trigger, f"trigger without id: {trigger}"


def test_expected_triggers_are_present(blueprint):
    assert set(blueprint.trigger_ids()) == {
        "window_start",
        "window_stop",
        "tick",
        "charger_status",
        "ha_start",
        "car_arrived",
        "target_hit",
    }


def test_trigger_variables_cover_the_template_triggers(blueprint):
    """Template triggers may only read ``trigger_variables``, never ``variables``."""
    declared = set(blueprint.trigger_variables)
    for trigger in blueprint.triggers:
        template = trigger.get("value_template")
        if not template:
            continue
        for name in re.findall(r"\btv_[a-z_]+\b", template):
            assert name in declared, f"{trigger['id']} uses undeclared {name}"


def test_no_bare_variables_leak_into_trigger_templates(blueprint):
    """A trigger referencing e.g. ``e_battery`` would silently be undefined."""
    variable_names = set(blueprint.variables)
    for trigger in blueprint.triggers:
        template = trigger.get("value_template", "")
        for name in variable_names:
            assert not re.search(rf"\b{re.escape(name)}\b", template), (
                f"trigger {trigger['id']} references action-scope variable {name}"
            )


def test_service_calls_tolerate_failures(blueprint):
    """A dead cloud integration must not abort the run mid-sequence."""
    calls: list[dict] = []

    def scan(node):
        if isinstance(node, dict):
            if "action" in node and isinstance(node["action"], str):
                calls.append(node)
            for value in node.values():
                scan(value)
        elif isinstance(node, list):
            for value in node:
                scan(value)

    scan(blueprint.actions)
    assert calls, "no service calls found - did the action block change shape?"
    for call in calls:
        assert call.get("continue_on_error") is True, (
            f"{call['action']} is missing continue_on_error"
        )


def test_turn_off_is_never_delayed(blueprint):
    """Stopping is a safety action and must not sit behind a throttling delay."""
    stop_branch = blueprint.actions[-1]["choose"][1]["sequence"]
    first = stop_branch[0]
    assert first["action"] == "switch.turn_off"


def test_no_refresh_buttons_are_pressed(blueprint):
    raw = blueprint.path.read_text(encoding="utf-8")
    assert "button.press" not in raw
    assert "homeassistant.update_entity" not in raw


@pytest.mark.parametrize(
    "artefact",
    ["voyah", "Voyah", "TODO", "FIXME", "XXX", "localhost", "127.0.0.1"],
)
def test_no_vendor_names_or_debug_leftovers(blueprint, artefact):
    raw = blueprint.path.read_text(encoding="utf-8")
    assert artefact not in raw, f"{artefact!r} leaked into the published blueprint"


def test_description_declares_a_version(blueprint):
    assert re.search(r"Версия \d+\.\d+\.\d+", blueprint.meta["description"])


def test_no_variable_shadows_a_template_helper(blueprint):
    """A variable called ``states`` or ``now`` would silently shadow the helper
    of the same name in every template rendered after it."""
    reserved = {
        "states", "state_attr", "is_state", "has_value", "is_number",
        "now", "utcnow", "today_at", "timedelta",
    }
    clashes = reserved & set(blueprint.variables)
    assert not clashes, f"variables shadow template helpers: {sorted(clashes)}"

    trigger_clashes = reserved & set(blueprint.trigger_variables)
    assert not trigger_clashes, (
        f"trigger variables shadow template helpers: {sorted(trigger_clashes)}"
    )


def test_variables_are_defined_before_they_are_used(blueprint):
    """HA renders ``variables:`` top to bottom; forward references render empty."""
    seen: set[str] = set()
    for name, node in blueprint.variables.items():
        if isinstance(node, InputRef):
            seen.add(name)
            continue
        if isinstance(node, str):
            for candidate in blueprint.variables:
                if candidate in seen or candidate == name:
                    continue
                if re.search(rf"\b{re.escape(candidate)}\b", node):
                    raise AssertionError(
                        f"{name} references {candidate}, which is defined later"
                    )
        seen.add(name)
