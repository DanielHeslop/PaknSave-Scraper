"""Power log: what the Pi says about its supply voltage, at the start and end of every run.

`vcgencmd get_throttled` gives a flag word (bit 0 undervoltage now, bit 16 undervoltage has
happened since boot, and so on). It needs /dev/vcio, which is root-only on this Pi, so when
it cannot run the same facts are taken from the kernel: the rpi_volt sensor (undervoltage
now) and the kernel log ("Undervoltage detected!" lines since boot). The log line and the
SUMMARY say which source was used.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HWMON_DIR = Path("/sys/class/hwmon")
COMMAND_TIMEOUT_SECONDS = 10

UNDERVOLTAGE_NOW = 1 << 0
FREQ_CAPPED_NOW = 1 << 1
THROTTLED_NOW = 1 << 2
TEMP_LIMIT_NOW = 1 << 3
UNDERVOLTAGE_SINCE_BOOT = 1 << 16
FREQ_CAPPED_SINCE_BOOT = 1 << 17
THROTTLED_SINCE_BOOT = 1 << 18
TEMP_LIMIT_SINCE_BOOT = 1 << 19


@dataclass
class PowerReading:
    source: str  # "vcgencmd", "kernel" or "none"
    flags: int | None = None  # the get_throttled word, when vcgencmd could be read
    undervoltage_now: bool | None = None
    undervoltage_since_boot: bool | None = None
    other_problems: tuple[str, ...] = ()  # throttling / temperature words
    note: str = ""  # why vcgencmd could not be used

    @property
    def value(self) -> str:
        """The reading as a short token: 0x50005, or kernel / unknown when vcgencmd was unavailable."""
        if self.flags is not None:
            return f"0x{self.flags:x}"
        return "kernel-only" if self.source == "kernel" else "unknown"

    @property
    def undervoltage(self) -> bool:
        return bool(self.undervoltage_now or self.undervoltage_since_boot)

    def words(self) -> str:
        if self.undervoltage_now:
            text = "UNDERVOLTAGE right now (the power supply is dipping too low)"
        elif self.undervoltage_since_boot:
            text = "UNDERVOLTAGE has happened since boot (the power supply dipped too low)"
        elif self.undervoltage_now is None and self.undervoltage_since_boot is None:
            return "power state could not be read"
        else:
            text = "no undervoltage since boot"
        if self.other_problems:
            text += "; " + ", ".join(self.other_problems)
        return text

    def line(self, label: str) -> str:
        how = {
            "vcgencmd": f"vcgencmd get_throttled=0x{self.flags:x}" if self.flags is not None else "vcgencmd",
            "kernel": f"vcgencmd could not be used ({self.note}); read from the kernel instead",
            "none": f"vcgencmd could not be used ({self.note}); the kernel could not be read either",
        }[self.source]
        return f"Power {label}: {how} - {self.words()}."


def decode_flags(flags: int) -> PowerReading:
    other = []
    if flags & THROTTLED_NOW:
        other.append("the CPU is being slowed right now")
    elif flags & THROTTLED_SINCE_BOOT:
        other.append("the CPU has been slowed since boot")
    if flags & TEMP_LIMIT_NOW:
        other.append("at the temperature limit right now")
    elif flags & TEMP_LIMIT_SINCE_BOOT:
        other.append("has hit the temperature limit since boot")
    return PowerReading(
        "vcgencmd",
        flags=flags,
        undervoltage_now=bool(flags & UNDERVOLTAGE_NOW),
        undervoltage_since_boot=bool(flags & (UNDERVOLTAGE_SINCE_BOOT | UNDERVOLTAGE_NOW)),
        other_problems=tuple(other),
    )


def _run(command: list[str]) -> str:
    done = subprocess.run(
        command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[0] if (done.stderr or done.stdout).strip() else f"exit {done.returncode}")
    return done.stdout


def _vcgencmd() -> PowerReading:
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", _run(["vcgencmd", "get_throttled"]))
    if not match:
        raise RuntimeError("unexpected output")
    return decode_flags(int(match.group(1), 16))


def _kernel_undervoltage_now(hwmon_dir: Path) -> bool | None:
    for sensor in sorted(hwmon_dir.glob("hwmon*")):
        try:
            if (sensor / "name").read_text().strip() == "rpi_volt":
                return (sensor / "in0_lcrit_alarm").read_text().strip() == "1"
        except OSError:
            continue
    return None


def _kernel_warnings_since_boot() -> int | None:
    for command in (["journalctl", "-k", "-b", "0", "--no-pager", "-q"], ["dmesg"]):
        try:
            return sum(1 for line in _run(command).splitlines() if "Undervoltage detected" in line)
        except Exception:
            continue
    return None


def _kernel(note: str, hwmon_dir: Path) -> PowerReading:
    now = _kernel_undervoltage_now(hwmon_dir)
    warnings = _kernel_warnings_since_boot()
    if now is None and warnings is None:
        return PowerReading("none", note=note)
    since_boot = None if warnings is None and not now else bool(now or (warnings or 0) > 0)
    return PowerReading("kernel", undervoltage_now=now, undervoltage_since_boot=since_boot, note=note)


def read_power(hwmon_dir: Path = HWMON_DIR) -> PowerReading:
    """The current power state. Never raises."""
    try:
        return _vcgencmd()
    except Exception as e:
        note = str(e) or type(e).__name__
        if len(note) > 80:
            note = note[:77] + "..."
    try:
        return _kernel(note, hwmon_dir)
    except Exception:
        return PowerReading("none", note=note)


# Swapped out by the test setup so no test reads the real Pi.
reader = read_power


def log_power(label: str, log) -> PowerReading:
    reading = reader()
    log(reading.line(label))
    return reading


def summary_part(start: PowerReading | None, end: PowerReading | None) -> str | None:
    """For the SUMMARY line, e.g. 'power start 0x0, end 0x0 (no undervoltage since boot)'."""
    if start is None or end is None:
        return None
    return f"power start {start.value}, end {end.value} ({end.words()})"
