"""The power log: `vcgencmd get_throttled` at the start and end of a run, in words, and in the SUMMARY."""

from __future__ import annotations

import sys

import pytest

from scraper import category_tree, power, woolworths
from scraper.ingredients import StoreLists
from scraper.price_runs import RunResult
from scraper.search_step import SearchOutcome, summary_line
from tests.test_woolworths_flow import _p


def _vcgencmd_says(monkeypatch, text):
    monkeypatch.setattr(power, "_run", lambda command: text)


def test_a_healthy_reading_says_no_undervoltage(monkeypatch, tmp_path):
    _vcgencmd_says(monkeypatch, "throttled=0x0\n")
    reading = power.read_power(tmp_path)
    assert reading.value == "0x0" and not reading.undervoltage
    assert reading.line("at start") == "Power at start: vcgencmd get_throttled=0x0 - no undervoltage since boot."


def test_undervoltage_since_boot_is_said_in_plain_words(monkeypatch, tmp_path):
    _vcgencmd_says(monkeypatch, "throttled=0x50000\n")
    reading = power.read_power(tmp_path)
    assert reading.undervoltage and not reading.undervoltage_now
    assert "UNDERVOLTAGE has happened since boot" in reading.line("at end")
    assert "0x50000" in reading.line("at end")


def test_undervoltage_right_now_and_throttling(monkeypatch, tmp_path):
    _vcgencmd_says(monkeypatch, "throttled=0x50005\n")
    text = power.read_power(tmp_path).words()
    assert "UNDERVOLTAGE right now" in text and "slowed right now" in text


def test_the_temperature_bits_are_not_taken_for_a_power_problem():
    reading = power.decode_flags(0x80000)
    assert not reading.undervoltage and "temperature limit" in reading.words()


def _fake_kernel(tmp_path, alarm):
    sensor = tmp_path / "hwmon3"
    sensor.mkdir()
    (sensor / "name").write_text("rpi_volt\n")
    (sensor / "in0_lcrit_alarm").write_text(f"{alarm}\n")
    return tmp_path


def _vcgencmd_fails(monkeypatch, kernel_log=""):
    def fake(command):
        if command[0] == "vcgencmd":
            raise RuntimeError("Can't open device file: /dev/vcio")
        return kernel_log

    monkeypatch.setattr(power, "_run", fake)


def test_when_vcgencmd_cannot_run_the_kernel_is_asked_instead(monkeypatch, tmp_path):
    _vcgencmd_fails(monkeypatch, "[ 5.1] other\n[ 900.2] hwmon hwmon3: Undervoltage detected!\n")
    reading = power.read_power(_fake_kernel(tmp_path, 0))
    assert reading.source == "kernel" and reading.undervoltage_since_boot and not reading.undervoltage_now
    line = reading.line("at start")
    assert "vcgencmd could not be used" in line and "/dev/vcio" in line and "UNDERVOLTAGE has happened since boot" in line


def test_kernel_reading_with_no_warnings_is_healthy(monkeypatch, tmp_path):
    _vcgencmd_fails(monkeypatch, "[ 5.1] nothing to see\n")
    reading = power.read_power(_fake_kernel(tmp_path, 0))
    assert not reading.undervoltage and reading.words() == "no undervoltage since boot"


def test_kernel_alarm_means_undervoltage_now(monkeypatch, tmp_path):
    _vcgencmd_fails(monkeypatch, "")
    reading = power.read_power(_fake_kernel(tmp_path, 1))
    assert reading.undervoltage_now and "right now" in reading.words()


def test_nothing_readable_is_said_honestly_and_never_raises(monkeypatch, tmp_path):
    def fake(command):
        raise RuntimeError("no way")

    monkeypatch.setattr(power, "_run", fake)
    reading = power.read_power(tmp_path / "missing")
    assert reading.source == "none" and not reading.undervoltage
    assert "could not be read" in reading.line("at end")
    assert power.summary_part(reading, reading) == "power start unknown, end unknown (power state could not be read)"


def test_log_power_writes_one_labelled_line():
    lines = []
    reading = power.log_power("at start", lines.append)
    assert lines == ["Power at start: vcgencmd get_throttled=0x0 - no undervoltage since boot."] and reading.value == "0x0"


def test_summary_line_carries_the_power_part():
    start, end = power.decode_flags(0), power.decode_flags(0x50000)
    line = summary_line(5, SearchOutcome(), 3, 61.0, power=power.summary_part(start, end))
    assert "run time 1m01s, power start 0x0, end 0x50000 (UNDERVOLTAGE has happened since boot" in line
    assert "power" not in summary_line(5, SearchOutcome(), 3, 61.0)


def test_a_woolworths_run_logs_power_at_start_and_end_and_in_the_summary(monkeypatch, tmp_path, capsys):
    readings = iter([power.decode_flags(0), power.decode_flags(0x50000)])
    monkeypatch.setattr(power, "reader", lambda: next(readings))
    monkeypatch.setattr(category_tree, "STATE_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(woolworths, "browse_products", lambda path, mp, label, stat=None: ([_p("1", "Sugar")], [], 1))
    monkeypatch.setattr(woolworths, "search_products", lambda t, size: ([_p("1", "Sugar")], []))
    monkeypatch.setattr(woolworths, "fetch_store_lists", lambda s, u: StoreLists(store="woolworths", search_terms=["sugar"], watch_terms=[], skip=frozenset()))
    monkeypatch.setattr(woolworths, "publish_run", lambda products, *a, **kw: RunResult(items=len(products)))
    monkeypatch.setattr(woolworths, "report_outcome", lambda s, o: None)
    cats = tmp_path / "c.txt"
    cats.write_text("pantry/baking\n")
    monkeypatch.setattr(sys, "argv", ["x", "--save", "--categories", str(cats), "--snapshots-dir", str(tmp_path / "snap"), "--history-path", str(tmp_path / "h.json")])

    assert woolworths.main() == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert any(l == "Power at start: vcgencmd get_throttled=0x0 - no undervoltage since boot." for l in lines)
    assert any(l.startswith("Power at end: vcgencmd get_throttled=0x50000 - UNDERVOLTAGE has happened since boot") for l in lines)
    assert lines[-1].startswith("SUMMARY:") and "power start 0x0, end 0x50000 (UNDERVOLTAGE has happened since boot" in lines[-1]
