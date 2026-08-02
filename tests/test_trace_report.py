"""Tests for tools/trace_report.py.

The script is what a user runs when something went wrong at three in the
morning, and its output is what ends up pasted into an issue. That makes its
failure mode unusually costly: every defect below was found by running it on
real traces or on the kind of stray file that lives in a Downloads folder, and
every one of them either crashed the report or silently produced nothing.

The script deliberately keeps working on Python 3.9 (the system interpreter on
macOS), so these tests avoid anything newer as well.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import trace_report  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "trace_report.py"
UTC = dt.timezone.utc  # noqa: UP017 - the script targets 3.9, so no dt.UTC alias
MSK = dt.timezone(dt.timedelta(hours=3))


def write(folder: Path, name: str, payload: object) -> Path:
    path = folder / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def a_trace(*, start: str | None = "2026-08-02T00:05:00+00:00", logbook=None, steps=None):
    """A minimal document shaped like a downloaded Home Assistant trace."""
    trace: dict = {"trace": steps if steps is not None else {}}
    if start is not None:
        trace["timestamp"] = {"start": start}
    return {"trace": trace, "logbookEntries": logbook if logbook is not None else []}


def run(folder: Path, *args: str, python: str | None = None):
    return subprocess.run(
        [python or sys.executable, str(SCRIPT), str(folder), *args],
        capture_output=True, text=True,
    )


# ------------------------------------------------------------- broken input


def test_a_json_file_that_is_not_an_object_is_skipped(tmp_path):
    """A Downloads folder holds JSON from everything, not just Home Assistant.

    A bare list used to reach ``raw.get`` and abort the whole report with an
    AttributeError, losing every valid trace alongside it.
    """
    write(tmp_path, "stray.json", [1, 2, 3])
    write(tmp_path, "good.json", a_trace())
    runs = trace_report.load(tmp_path)
    assert [r["file"] for r in runs] == ["good.json"]


@pytest.mark.parametrize("payload", [[1, 2, 3], "hello", None, 42])
def test_no_top_level_shape_crashes_the_loader(tmp_path, payload):
    write(tmp_path, "x.json", payload)
    assert trace_report.load(tmp_path) == []


def test_logbook_entries_of_the_wrong_type_are_ignored(tmp_path):
    """``logbookEntries`` as a dict used to crash mid-report, after the header
    had already been printed."""
    write(tmp_path, "a.json", a_trace(logbook={"not": "a list"}))
    runs = trace_report.load(tmp_path)
    assert runs and runs[0]["logbook"] == []
    assert trace_report.logbook(runs, MSK) == []


def test_a_logbook_entry_that_is_not_a_dict_is_skipped(tmp_path):
    write(tmp_path, "a.json", a_trace(logbook=["just a string", {"message": "ok", "when": 0}]))
    rows = trace_report.logbook(trace_report.load(tmp_path), MSK)
    assert [message for _, message in rows] == ["ok"]


def test_an_unreadable_file_does_not_abort_the_report(tmp_path):
    """A directory named ``*.json`` is what an unpacked bundle looks like."""
    (tmp_path / "weird.json").mkdir()
    write(tmp_path, "good.json", a_trace())
    runs = trace_report.load(tmp_path)
    assert [r["file"] for r in runs] == ["good.json"]


def test_skipped_files_are_reported_on_stderr(tmp_path, capsys):
    """Silence here is indistinguishable from "no traces found", which is the
    single most misleading thing this script could say."""
    (tmp_path / "weird.json").mkdir()
    trace_report.load(tmp_path)
    assert "weird.json" in capsys.readouterr().err


# ------------------------------------------------------------------- clocks


def test_a_run_without_a_timestamp_still_prints(tmp_path):
    """``--vars`` used to die formatting None with a date specifier."""
    write(tmp_path, "a.json", a_trace(start=None, steps={"x": [
        {"changed_variables": {"verdict": "тест"}}]}))
    result = run(tmp_path, "--vars")
    assert result.returncode == 0, result.stderr
    assert "??-?? ??:??:??" in result.stdout
    assert "verdict = 'тест'" in result.stdout


def test_the_z_suffix_is_understood(tmp_path):
    """``fromisoformat`` only learned ``Z`` in 3.11; the script targets 3.9."""
    write(tmp_path, "a.json", a_trace(start="2026-08-02T00:00:00Z"))
    moment = trace_report.when(trace_report.load(tmp_path)[0], MSK)
    assert moment == dt.datetime(2026, 8, 2, 3, 0, tzinfo=MSK)


def test_a_naive_timestamp_is_read_as_utc_not_as_local_time(tmp_path, monkeypatch):
    """Otherwise the same file reads seven hours apart in Moscow and New York,
    and the report can no longer be compared against anyone else's."""
    write(tmp_path, "a.json", a_trace(start="2026-08-02T00:00:00"))
    moment = trace_report.when(trace_report.load(tmp_path)[0], MSK)
    assert moment == dt.datetime(2026, 8, 2, 3, 0, tzinfo=MSK)


def test_an_unparseable_timestamp_does_not_raise(tmp_path):
    write(tmp_path, "a.json", a_trace(start="не дата"))
    assert trace_report.when(trace_report.load(tmp_path)[0], MSK) is None


@pytest.mark.parametrize("bad", ["99", "-30", "abc"])
def test_an_out_of_range_timezone_is_refused_politely(tmp_path, bad):
    write(tmp_path, "a.json", a_trace())
    result = run(tmp_path, "--tz", bad)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--tz" in result.stderr


# -------------------------------------------------------------- the logbook


def test_the_same_entry_in_two_files_is_shown_once(tmp_path):
    """Consecutive traces repeat each other's logbook entries."""
    entry = {"message": "запускаем зарядку", "when": 1785628800.0}
    write(tmp_path, "a.json", a_trace(logbook=[entry]))
    write(tmp_path, "b.json", a_trace(logbook=[entry]))
    rows = trace_report.logbook(trace_report.load(tmp_path), MSK)
    assert len(rows) == 1


def test_two_real_events_in_the_same_second_both_survive(tmp_path):
    """``mode: restart`` fires bursts; the real traces contain four entries
    inside three seconds. Deduplicating on (time, text) alone would eat them."""
    entry = {"message": "ток в норме", "when": 1785628800.0}
    write(tmp_path, "a.json", a_trace(logbook=[entry, dict(entry)]))
    rows = trace_report.logbook(trace_report.load(tmp_path), MSK)
    assert len(rows) == 2


def test_the_automation_own_trigger_line_is_dropped(tmp_path):
    write(tmp_path, "a.json", a_trace(logbook=[
        {"message": "triggered by state of sensor.x", "when": 1785628800.0},
        {"message": "полезное", "when": 1785628801.0},
    ]))
    rows = trace_report.logbook(trace_report.load(tmp_path), MSK)
    assert [message for _, message in rows] == ["полезное"]


# ------------------------------------------------------------------ output


def test_commands_are_extracted(tmp_path):
    write(tmp_path, "a.json", a_trace(steps={"action/1": [
        {"result": {"params": {"domain": "number", "service": "set_value"}}}]}))
    assert trace_report.commands(trace_report.load(tmp_path)[0]) == ["number.set_value"]


def test_unrelated_services_are_not_listed_as_commands(tmp_path):
    write(tmp_path, "a.json", a_trace(steps={"action/1": [
        {"result": {"params": {"domain": "persistent_notification", "service": "create"}}}]}))
    assert trace_report.commands(trace_report.load(tmp_path)[0]) == []


def test_variables_merge_across_steps(tmp_path):
    """Home Assistant records only what changed, except on the first step."""
    write(tmp_path, "a.json", a_trace(steps={"a": [{"changed_variables": {"soc": 51}}],
                                               "b": [{"changed_variables": {"verdict": "ok"}}]}))
    merged = trace_report.variables(trace_report.load(tmp_path)[0])
    assert merged == {"soc": 51, "verdict": "ok"}


def test_json_output_is_valid_and_keeps_cyrillic(tmp_path):
    write(tmp_path, "a.json", a_trace(steps={"a": [
        {"changed_variables": {"verdict": "ставим ток"}}]}))
    result = run(tmp_path, "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload[0]["variables"]["verdict"] == "ставим ток"


def test_json_output_uses_null_for_a_missing_time(tmp_path):
    write(tmp_path, "a.json", a_trace(start=None))
    payload = json.loads(run(tmp_path, "--json").stdout)
    assert payload[0]["time"] is None


def test_an_empty_folder_explains_that_the_search_is_not_recursive(tmp_path):
    (tmp_path / "nested").mkdir()
    write(tmp_path / "nested", "a.json", a_trace())
    result = run(tmp_path)
    assert result.returncode == 1
    assert "вложенных" in result.stderr


def test_a_path_that_is_not_a_folder_is_refused(tmp_path):
    path = write(tmp_path, "a.json", a_trace())
    result = run(path)
    assert result.returncode == 1
    assert "не папка" in result.stderr


# ------------------------------------------------------- the hostile locale


def test_the_report_survives_an_ascii_locale(tmp_path):
    """Under ``LC_ALL=C`` -- ssh without locale forwarding, cron, Docker --
    ``read_text()`` used to raise on every UTF-8 trace, and the resulting
    "no traces found" was indistinguishable from an empty folder."""
    write(tmp_path, "a.json", a_trace(logbook=[
        {"message": "запускаем зарядку", "when": 1785628800.0}]))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C",
             "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert "Прогонов: 1" in result.stdout
    assert "запускаем зарядку" in result.stdout
