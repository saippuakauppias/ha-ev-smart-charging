"""Offline emulator of the Home Assistant template engine, just enough to test blueprints.

The blueprint under test keeps all of its decision logic inside the ``variables:``
block and the template triggers. That means the logic can be exercised without a
running Home Assistant instance: load the YAML, resolve ``!input`` references,
render every variable in order against a fake world, and inspect the result.

This module deliberately implements only the subset of the HA template API that
the blueprint actually uses. If the blueprint starts using a new helper, add it
here rather than loosening the tests.
"""

from __future__ import annotations

import ast
import datetime as dt
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment

#: Mirrors Home Assistant's own entity id validation.
_VALID_ENTITY_ID = re.compile(r"^(?!.+__)([a-z][a-z0-9_]*)\.([a-z0-9_]+)$")


class TemplateError(Exception):
    """Raised where Home Assistant would raise ``TemplateError``."""

# Europe/Moscow style fixed offset. A fixed offset keeps the tests deterministic
# and independent of the tzdata version installed on the CI runner.
TZ = dt.timezone(dt.timedelta(hours=3))


class InputRef:
    """Marker for a ``!input <name>`` node in the blueprint YAML."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"InputRef({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, InputRef) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("InputRef", self.name))


class BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that understands the ``!input`` tag."""


BlueprintLoader.add_constructor(
    "!input", lambda loader, node: InputRef(loader.construct_scalar(node))
)


@dataclass
class State:
    """A single entity state, mirroring the attributes the blueprint reads."""

    state: Any
    attributes: dict[str, Any] = field(default_factory=dict)
    last_changed: dt.datetime | None = None

    def __post_init__(self) -> None:
        self.state = str(self.state)


class _StatesAccessor:
    """Implements both ``states('x.y')`` and ``states['x.y']`` like HA does."""

    def __init__(self, world: dict[str, State]) -> None:
        self._world = world

    def __call__(self, entity_id: str) -> str:
        obj = self._world.get(entity_id)
        return obj.state if obj is not None else "unknown"

    def __getitem__(self, entity_id: str) -> State | None:
        # Home Assistant raises on a malformed entity id here rather than
        # returning None. An empty string is the realistic case: it is what an
        # unfilled optional input normalises to, and a blueprint that indexes
        # ``states['']`` would crash the whole render in production.
        if not _VALID_ENTITY_ID.match(str(entity_id)):
            raise TemplateError(f"Invalid entity ID '{entity_id}'")
        return self._world.get(entity_id)


#: Templates are compiled once and reused. Compilation dominates the runtime of
#: the closed-loop night simulation, which renders the same ~90 templates on
#: every tick.
_ENV = Environment()


def _forgiving_round(value: Any, precision: int = 0, method: str = "common") -> Any:
    """Home Assistant's ``round`` filter, which returns an int at precision 0."""
    number = float(value)
    if method == "ceil":
        rounded = math.ceil(number * 10**precision) / 10**precision
    elif method == "floor":
        rounded = math.floor(number * 10**precision) / 10**precision
    elif method == "half":
        rounded = round(number * 2) / 2
    else:
        rounded = round(number, precision)
    return int(rounded) if precision == 0 and method != "half" else rounded


_ENV.filters["round"] = _forgiving_round
_COMPILED: dict[str, Any] = {}


def _compile(source: str):
    template = _COMPILED.get(source)
    if template is None:
        template = _ENV.from_string(source)
        _COMPILED[source] = template
    return template


def _build_helpers(world: dict[str, State], now: dt.datetime) -> dict[str, Any]:
    """The subset of the Home Assistant template API the blueprint relies on."""
    states = _StatesAccessor(world)

    def state_attr(entity_id: str, attribute: str) -> Any:
        obj = world.get(entity_id)
        return obj.attributes.get(attribute) if obj is not None else None

    def is_state(entity_id: str, value: str) -> bool:
        return states(entity_id) == value

    def has_value(entity_id: str) -> bool:
        return states(entity_id) not in ("unknown", "unavailable")

    def is_number(value: Any) -> bool:
        # Home Assistant rejects infinities and NaN here. Accepting them would
        # let a broken sensor reading through and hide a real crash: ``'inf'``
        # passes ``float()`` but blows up the first comparison downstream.
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number)

    def today_at(time_str: str = "00:00:00") -> dt.datetime:
        parts = [int(p) for p in str(time_str).split(":")]
        parts += [0] * (3 - len(parts))
        return now.replace(
            hour=parts[0], minute=parts[1], second=parts[2], microsecond=0
        )

    return {
        "states": states,
        "state_attr": state_attr,
        "is_state": is_state,
        "has_value": has_value,
        "is_number": is_number,
        "now": lambda: now,
        "utcnow": lambda: now.astimezone(dt.UTC),
        "today_at": today_at,
        "timedelta": dt.timedelta,
    }


def _coerce(rendered: str) -> Any:
    """Turn a rendered template back into a native Python value, as HA does."""
    text = rendered.strip()
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def _render(
    template: Any, helpers: dict[str, Any], context: dict[str, Any]
) -> Any:
    if not isinstance(template, str):
        return template
    if "{{" not in template and "{%" not in template:
        return template
    return _coerce(_compile(template).render(**helpers, **context))


class MissingInput(KeyError):
    """Raised when a required blueprint input has neither a value nor a default."""


class Blueprint:
    """A loaded blueprint, ready to be evaluated against a fake world."""

    def __init__(self, document: dict[str, Any], path: Path) -> None:
        self.path = path
        self.document = document
        self.meta = document["blueprint"]
        self.variables: dict[str, Any] = document.get("variables", {})
        self.trigger_variables: dict[str, Any] = document.get("trigger_variables", {})
        self.triggers: list[dict[str, Any]] = document.get("triggers", [])
        self.actions: list[Any] = document.get("actions", [])
        self.inputs = self._flatten_inputs(self.meta.get("input", {}))

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, path: str | Path) -> Blueprint:
        path = Path(path)
        with path.open(encoding="utf-8") as handle:
            document = yaml.load(handle, Loader=BlueprintLoader)
        return cls(document, path)

    @staticmethod
    def _flatten_inputs(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Flatten the section/input tree into ``{input_name: schema}``."""
        flat: dict[str, dict[str, Any]] = {}

        def walk(node: dict[str, Any]) -> None:
            for key, value in node.items():
                if not isinstance(value, dict):
                    continue
                is_section = "input" in value and "selector" not in value
                if is_section:
                    walk(value["input"])
                else:
                    flat[key] = value

        walk(raw)
        return flat

    @property
    def sections(self) -> dict[str, dict[str, Any]]:
        return {
            name: node
            for name, node in self.meta.get("input", {}).items()
            if isinstance(node, dict) and "input" in node and "selector" not in node
        }

    # -------------------------------------------------------------- analysis

    def referenced_inputs(self) -> set[str]:
        """Every input name reachable through an ``!input`` node outside metadata."""
        found: set[str] = set()

        def scan(node: Any) -> None:
            if isinstance(node, InputRef):
                found.add(node.name)
            elif isinstance(node, dict):
                for value in node.values():
                    scan(value)
            elif isinstance(node, list):
                for value in node:
                    scan(value)

        scan({k: v for k, v in self.document.items() if k != "blueprint"})
        return found

    def defaults(self) -> dict[str, Any]:
        return {
            name: schema["default"]
            for name, schema in self.inputs.items()
            if "default" in schema
        }

    def required_inputs(self) -> set[str]:
        return {name for name, schema in self.inputs.items() if "default" not in schema}

    # ------------------------------------------------------------- rendering

    def resolve_inputs(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        values = self.defaults()
        values.update(overrides or {})
        missing = self.required_inputs() - set(values)
        if missing:
            raise MissingInput(f"missing required inputs: {sorted(missing)}")
        unknown = set(values) - set(self.inputs)
        if unknown:
            raise KeyError(f"unknown inputs supplied: {sorted(unknown)}")
        return values

    def evaluate(
        self,
        *,
        world: dict[str, State],
        now: dt.datetime,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render every blueprint variable in order and return the context."""
        resolved = self.resolve_inputs(inputs)
        helpers = _build_helpers(world, now)
        context: dict[str, Any] = {}
        for name, node in self.variables.items():
            if isinstance(node, InputRef):
                context[name] = resolved[node.name]
            else:
                context[name] = _render(node, helpers, context)
        return context

    def trigger_ids(self) -> list[str]:
        return [t["id"] for t in self.triggers if "id" in t]

    def run_actions(
        self,
        *,
        world: dict[str, State],
        now: dt.datetime,
        inputs: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Walk the ``actions:`` block and return the calls it would make.

        The variables block is the brain of the blueprint and is covered
        exhaustively elsewhere; this exists to check the part that actually
        talks to the charger: which service is called, in what order, with what
        payload, and where the delays sit. Hook inputs render as
        ``{"hook": <name>}`` so tests can assert that a notification fired
        without caring what the user put in it.
        """
        helpers = _build_helpers(world, now)
        context = self.evaluate(world=world, now=now, inputs=inputs)
        calls: list[dict[str, Any]] = []

        def truthy(value: Any) -> bool:
            return value not in (False, None, "", "False", "false", 0)

        def check(conditions: Any) -> bool:
            for condition in conditions or []:
                if "value_template" not in condition:
                    raise NotImplementedError(f"unsupported condition: {condition}")
                if not truthy(_render(condition["value_template"], helpers, context)):
                    return False
            return True

        def walk(steps: Any) -> None:
            if steps is None:
                return
            if isinstance(steps, InputRef):
                calls.append({"hook": steps.name})
                return
            for step in steps:
                if isinstance(step, InputRef):
                    calls.append({"hook": step.name})
                elif "choose" in step:
                    for option in step["choose"] or []:
                        if check(option.get("conditions")):
                            walk(option.get("sequence"))
                            break
                    else:
                        walk(step.get("default"))
                elif "if" in step:
                    walk(step["then"] if check(step["if"]) else step.get("else"))
                elif "sequence" in step:
                    walk(step["sequence"])
                elif "delay" in step:
                    delay = step["delay"]
                    seconds = (
                        delay.get("seconds", 0) if isinstance(delay, dict) else delay
                    )
                    calls.append(
                        {"delay": _render(seconds, helpers, context)}
                    )
                elif "action" in step:
                    calls.append(
                        {
                            "action": step["action"],
                            "entity_id": _render(
                                (step.get("target") or {}).get("entity_id"),
                                helpers,
                                context,
                            ),
                            "data": {
                                key: _render(value, helpers, context)
                                for key, value in (step.get("data") or {}).items()
                            },
                        }
                    )
                else:
                    raise NotImplementedError(f"unsupported step: {sorted(step)}")

        walk(self.document.get("actions"))
        return calls

    def fire_trigger(
        self,
        trigger_id: str,
        *,
        world: dict[str, State],
        now: dt.datetime,
        inputs: dict[str, Any] | None = None,
    ) -> Any:
        """Render a template trigger and return whether it would fire."""
        matches = [
            t
            for t in self.triggers
            if t.get("id") == trigger_id and "value_template" in t
        ]
        if not matches:
            raise LookupError(f"no template trigger with id {trigger_id!r}")
        resolved = self.resolve_inputs(inputs)
        helpers = _build_helpers(world, now)
        context = {
            name: (resolved[node.name] if isinstance(node, InputRef) else node)
            for name, node in self.trigger_variables.items()
        }
        return _render(matches[0]["value_template"], helpers, context)


# --------------------------------------------------------------------- utils


def moment(
    hour: int, minute: int = 0, *, day: int = 3, month: int = 8, year: int = 2026
) -> dt.datetime:
    """Build a timezone-aware datetime. Defaults to Monday, 3 August 2026."""
    return dt.datetime(year, month, day, hour, minute, tzinfo=TZ)


def ago(reference: dt.datetime, *, seconds: float = 0, minutes: float = 0,
        hours: float = 0) -> dt.datetime:
    return reference - dt.timedelta(seconds=seconds, minutes=minutes, hours=hours)
