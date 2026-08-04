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
    "charger_online",
    "car_home",
    "physically_present",
    # Владение сессией: во вторую реальную ночь блюпринт семь часов считал
    # собственную зарядку чужой, и по остальным полям это было незаметно —
    # вердикт «зарядка начата вручную» выглядел законным решением.
    "session_owned",
    "foreign_session",
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
            # Читаем байтами: трассировки — UTF-8 с кириллицей, а read_text()
            # взял бы кодировку из локали. Под LC_ALL=C (ssh без проброса
            # локали, cron, docker) это давало UnicodeDecodeError на каждом
            # файле, и except ниже молча съедал все трассировки разом.
            raw = json.loads(path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            # Про пропуск говорим вслух: молчание здесь неотличимо от
            # «файлов нет», а это самый частый повод открыть issue.
            print(f"пропущен {path.name}: {exc}", file=sys.stderr)
            continue
        # Верхний уровень может быть чем угодно: в папке загрузок лежит
        # посторонний JSON, в том числе списки и строки.
        if not isinstance(raw, dict):
            continue
        trace = raw.get("trace")
        if not isinstance(trace, dict) or "trace" not in trace:
            continue
        entries = raw.get("logbookEntries")
        runs.append({
            "file": path.name,
            "trace": trace,
            "logbook": entries if isinstance(entries, list) else [],
        })
    return runs


def when(run: dict[str, Any], tz: dt.timezone) -> dt.datetime | None:
    stamp = run["trace"].get("timestamp", {})
    start = stamp.get("start") if isinstance(stamp, dict) else None
    if not isinstance(start, str) or not start:
        return None
    # Суффикс «Z» fromisoformat понимает только с 3.11, а скрипт рассчитан
    # на системный Python (на macOS это 3.9).
    try:
        parsed = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Время без зоны — это UTC: так его пишет Home Assistant. Без явной
    # подстановки astimezone() взял бы зону машины, и один и тот же файл
    # читался бы по-разному в Москве и в Нью-Йорке.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)  # noqa: UP017
    return parsed.astimezone(tz)


def stamp_of(run: dict[str, Any], tz: dt.timezone) -> str:
    """Время прогона для печати. Трассировка без отметки времени — не повод
    ронять весь отчёт: её остальное содержимое всё ещё полезно."""
    moment = when(run, tz)
    return f"{moment:%m-%d %H:%M:%S}" if moment else "??-?? ??:??:??"


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
    for steps in run["trace"].get("trace", {}).values():
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
    return found


def logbook(runs: list[dict[str, Any]], tz: dt.timezone) -> list[tuple[dt.datetime, str]]:
    # Одна и та же запись журнала попадает в несколько скачанных файлов,
    # поэтому дедуплицируем. Ключ включает имя файла-источника не целиком:
    # повтор внутри одного прогона — это реальное повторное срабатывание,
    # и терять его нельзя, а вот копию из соседнего файла — нужно.
    rows: set[tuple[dt.datetime, str]] = set()
    for run in runs:
        seen_here: dict[tuple[dt.datetime, str], int] = {}
        for entry in run["logbook"]:
            if not isinstance(entry, dict):
                continue
            message, stamp = entry.get("message"), entry.get("when")
            if not message or not isinstance(message, str):
                continue
            if not isinstance(stamp, (int, float)):
                continue
            # «triggered by ...» — служебная запись самой автоматизации,
            # она дублирует триггер и только засоряет ленту.
            if message.startswith("triggered by"):
                continue
            # timezone.utc, а не UTC-алиас (3.11+): скрипт запускают системным
            # Python, а он на macOS до сих пор 3.9.
            try:
                moment = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(tz)  # noqa: UP017
            except (OverflowError, OSError, ValueError):
                continue
            key = (moment, " ".join(message.split()))
            # В пределах одного файла одинаковые пары «время + текст» — разные
            # события (mode: restart легко даёт пачку за одну секунду), поэтому
            # сдвигаем повтор на микросекунду, чтобы он пережил дедупликацию
            # между файлами и при этом встал в ленту сразу за оригиналом.
            seen_here[key] = seen_here.get(key, 0) + 1
            shift = seen_here[key] - 1
            rows.add((moment + dt.timedelta(microseconds=shift), key[1]))
    return sorted(rows)


def anomalies(runs: list[dict[str, Any]], tz: dt.timezone) -> list[str]:
    """Что стоит заметить в ночи, не вчитываясь во все прогоны.

    Ленту журнала глазами не осилить: за ночь набегает под сотню строк, и
    решающая деталь в них выглядит как штатная работа. Вторую реальную ночь
    выдавала одна строка — «зарядка начата вручную» при том, что зарядку
    начинала сама автоматизация, — и заметить её среди прочих было нечем.
    """
    found: list[str] = []
    offline_since: dt.datetime | None = None
    foreign_since: dt.datetime | None = None
    shortfalls: list[float] = []

    for run in runs:
        moment, values = when(run, tz), variables(run)
        if moment is None:
            continue

        # Чужая сессия, длящаяся часами, почти наверняка своя: человек,
        # включивший зарядку руками, не делает этого каждую ночь подряд.
        if values.get("foreign_session") is True:
            foreign_since = foreign_since or moment
        elif foreign_since is not None:
            hours = (moment - foreign_since).total_seconds() / 3600
            if hours >= 1:
                found.append(
                    f"{foreign_since:%m-%d %H:%M} — {moment:%H:%M} "
                    f"({hours:.1f} ч) сессия считалась чужой: ток не регулировался"
                )
            foreign_since = None

        if values.get("charger_online") is False:
            offline_since = offline_since or moment
        elif offline_since is not None:
            found.append(f"{offline_since:%m-%d %H:%M:%S} станция теряла связь")
            offline_since = None

        setpoint, actual = values.get("current_now"), values.get("actual_current")
        # Нулевой ток при стоящей уставке — это просто выключенная станция,
        # а не недодача: считать её в среднее значило бы утопить настоящий
        # разрыв в часах простоя.
        if (isinstance(setpoint, (int, float)) and isinstance(actual, (int, float))
                and setpoint > 0 and actual > 0.5):
            shortfalls.append(setpoint - actual)

    if foreign_since is not None:
        found.append(
            f"{foreign_since:%m-%d %H:%M} и до конца выгрузки сессия считалась чужой"
        )
    if offline_since is not None:
        found.append(f"{offline_since:%m-%d %H:%M:%S} станция потеряла связь")

    # Недодача тока: станция систематически отдаёт меньше заказанного, и это
    # прямо стоит времени зарядки. Компенсируется параметром «Запас по току»:
    # КПД для этого не годится, он отвечает за потери в батарее, а не
    # за смещение станции.
    if shortfalls:
        average = sum(shortfalls) / len(shortfalls)
        if average > 0.5:
            found.append(
                f"станция недодаёт в среднем {average:.2f} А "
                f"(по {len(shortfalls)} замерам) — стоит поднять «Запас по току»"
            )
    return found


def offset_hours(value: str) -> float:
    """Часовой пояс как смещение от UTC. Опечатка вроде «33» вместо «3»
    должна давать понятный отказ, а не стек вызовов из недр datetime."""
    try:
        hours = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно число, а не {value!r}") from None
    if not -24 < hours < 24:
        raise argparse.ArgumentTypeError(f"смещение {hours} вне диапазона (-24, 24)")
    return hours


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        # Без этого argparse схлопывает докстринг в один абзац и примеры
        # команд слипаются в нечитаемую кашу.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("folder", type=Path, help="папка со скачанными трассировками")
    parser.add_argument("--tz", type=offset_hours, default=3.0,
                        help="часовой пояс, часов от UTC (по умолчанию 3)")
    parser.add_argument("--vars", action="store_true", help="таблица переменных по прогонам")
    parser.add_argument("--json", action="store_true", help="машинночитаемый вывод")
    args = parser.parse_args()

    # Отчёт почти целиком на кириллице, а под ASCII-локалью print() ломался бы
    # на первой же строке (или деградировал в \uXXXX).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    tz = dt.timezone(dt.timedelta(hours=args.tz))
    if not args.folder.is_dir():
        print(f"{args.folder} — не папка.", file=sys.stderr)
        return 1
    runs = load(args.folder)
    if not runs:
        print(
            f"В {args.folder} не найдено файлов трассировок "
            f"(поиск идёт только в самой папке, без вложенных).",
            file=sys.stderr,
        )
        return 1
    runs.sort(key=lambda r: when(r, tz) or dt.datetime.min.replace(tzinfo=tz))

    if args.json:
        json.dump(
            [
                {
                    "time": str(moment) if (moment := when(r, tz)) else None,
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
            print(f"{stamp_of(run, tz)}  {', '.join(sent)}")
    if quiet:
        print("(команд не было)")

    print("\n=== НА ЧТО ОБРАТИТЬ ВНИМАНИЕ ===")
    notes = anomalies(runs, tz)
    for line in notes:
        print(line)
    if not notes:
        print("(ничего необычного)")

    if args.vars:
        print("\n=== ПЕРЕМЕННЫЕ ===")
        for run in runs:
            values = variables(run)
            print(f"\n{stamp_of(run, tz)}  триггер: {run['trace'].get('trigger')}")
            for key in INTERESTING:
                if key in values:
                    print(f"    {key} = {values[key]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
