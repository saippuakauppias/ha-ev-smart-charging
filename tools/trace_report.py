#!/usr/bin/env python3
"""Сводка по скачанным трассировкам автоматизации.

Собирает из выгруженных файлов то, ради чего их обычно и скачивают: журнал
с временем, отправленные команды и таблицу решений. Записи журнала лежат
внутри самих трассировок (``logbookEntries``), поэтому выгружать журнал
отдельно не нужно — в интерфейсе «Активность» кнопки скачивания всё равно нет.

Использование::

    python3 tools/trace_report.py ~/Downloads            # сводка за ночь
    python3 tools/trace_report.py ~/Downloads --tz 3     # часовой пояс, по умолчанию 3 (МСК)
    python3 tools/trace_report.py ~/Downloads --vars     # полная таблица переменных
    python3 tools/trace_report.py ~/Downloads --json > report.json

Скрипт ничего не отправляет наружу и не требует установленных пакетов.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

#: Переменные, которых почти всегда хватает для разбора.
INTERESTING = [
    "verdict",
    "should_charge",
    "must_stop",
    "stop_reason",
    "switch_on",
    "current_now",
    "desired_current",
    "actual_current",
    "soc",
    "charger_status",
    "car_home",
    "physically_present",
    "alarm_reason",
]

COMMANDS = (
    "number.set_value",
    "switch.turn_on",
    "switch.turn_off",
    "select.select_option",
    "input_boolean.turn_on",
    "input_boolean.turn_off",
)


def load(folder: Path) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(folder.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        trace = raw.get("trace")
        if not isinstance(trace, dict) or "trace" not in trace:
            continue
        runs.append({"file": path.name, "trace": trace, "logbook": raw.get("logbookEntries", [])})
    return runs


def when(run: dict[str, Any], tz: dt.timezone) -> dt.datetime | None:
    stamp = run["trace"].get("timestamp", {})
    start = stamp.get("start") if isinstance(stamp, dict) else None
    if not start:
        return None
    return dt.datetime.fromisoformat(start).astimezone(tz)


def variables(run: dict[str, Any]) -> dict[str, Any]:
    """Все переменные прогона.

    Home Assistant пишет в каждый шаг только изменившиеся значения, а на первом
    шаге — сразу все, поэтому объединение шагов даёт полную картину.
    """
    merged: dict[str, Any] = {}
    for steps in run["trace"].get("trace", {}).values():
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict):
                merged.update(step.get("changed_variables") or {})
    return merged


def commands(run: dict[str, Any]) -> list[str]:
    found = []
    for path, steps in run["trace"].get("trace", {}).items():
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            result = step.get("result") or {}
            params = result.get("params") or {}
            service = params.get("domain")
            action = params.get("service")
            if service and action:
                name = f"{service}.{action}"
                if name in COMMANDS:
                    found.append(name)
            elif "/choose/" in path and isinstance(result.get("choice"), int):
                continue
    return found


def logbook(runs: list[dict[str, Any]], tz: dt.timezone) -> list[tuple[dt.datetime, str]]:
    rows = set()
    for run in runs:
        for entry in run["logbook"]:
            message, stamp = entry.get("message"), entry.get("when")
            if not message or not stamp:
                continue
            # «triggered by ...» — служебная запись самой автоматизации,
            # она дублирует триггер и только засоряет ленту.
            if message.startswith("triggered by"):
                continue
            # timezone.utc, а не UTC-алиас (3.11+): скрипт запускают системным
            # Python, а он на macOS до сих пор 3.9.
            moment = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(tz)  # noqa: UP017
            rows.add((moment, " ".join(message.split())))
    return sorted(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="папка со скачанными трассировками")
    parser.add_argument("--tz", type=float, default=3.0, help="часовой пояс, часов от UTC")
    parser.add_argument("--vars", action="store_true", help="таблица переменных по прогонам")
    parser.add_argument("--json", action="store_true", help="машинночитаемый вывод")
    args = parser.parse_args()

    tz = dt.timezone(dt.timedelta(hours=args.tz))
    runs = load(args.folder)
    if not runs:
        print(f"В {args.folder} не найдено файлов трассировок.", file=sys.stderr)
        return 1
    runs.sort(key=lambda r: when(r, tz) or dt.datetime.min.replace(tzinfo=tz))

    if args.json:
        json.dump(
            [
                {
                    "time": (when(r, tz) or "").__str__(),
                    "trigger": r["trace"].get("trigger"),
                    "commands": commands(r),
                    "variables": variables(r),
                }
                for r in runs
            ],
            sys.stdout,
            ensure_ascii=False,
            indent=1,
            default=str,
        )
        return 0

    print(f"Прогонов: {len(runs)}\n")

    print("=== ЖУРНАЛ ===")
    for moment, message in logbook(runs, tz):
        print(f"{moment:%m-%d %H:%M:%S}  {message}")

    print("\n=== ОТПРАВЛЕННЫЕ КОМАНДЫ ===")
    quiet = True
    for run in runs:
        sent = commands(run)
        if sent:
            quiet = False
            print(f"{when(run, tz):%m-%d %H:%M:%S}  {', '.join(sent)}")
    if quiet:
        print("(команд не было)")

    if args.vars:
        print("\n=== ПЕРЕМЕННЫЕ ===")
        for run in runs:
            values = variables(run)
            print(f"\n{when(run, tz):%m-%d %H:%M:%S}  триггер: {run['trace'].get('trigger')}")
            for key in INTERESTING:
                if key in values:
                    print(f"    {key} = {values[key]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
