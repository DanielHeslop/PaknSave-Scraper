"""After a restart: carry on a scrape that a power cut interrupted.

Run from cron at boot (`@reboot`). It looks at each store's run for today:

  * progress saved by the scraper (data/checkpoints/<store>.json) -> run the store's usual
    wrapper script again; the scraper carries on from the last finished step;
  * today's log has no RESULT line, no saved progress and a snapshot already written -> the
    scrape had finished and only the posting was cut; post the snapshot (scraper.post_run);
  * today's log has no RESULT line and nothing saved -> run the wrapper again from the start.

It does nothing after 22:00, waits for the clock to be corrected (the Pi has no clock
battery), starts a store at most MAX_ATTEMPTS_PER_DAY times a day, and never starts while
another scrape holds the lock (scraper/lock.py).

    python3 -m scraper.resume_after_boot            # do it
    python3 -m scraper.resume_after_boot --dry-run  # only say what it would do
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from scraper import category_tree, checkpoint, lock
from scraper.price_runs import REPO_ROOT, SNAPSHOT_SUBDIRS

CUTOFF_HOUR = 22
MAX_ATTEMPTS_PER_DAY = 3
CLOCK_WAIT_SECONDS = 10 * 60
CLOCK_POLL_SECONDS = 15
DEFAULT_ENV_FILE = "~/.config/paknsave-scraper/env"

# store -> (wrapper script, log file name for a day)
STORES = {
    "paknsave": ("run_weekly_combined.sh", "weekly-combined-{day}.log"),
    "newworld": ("run_newworld.sh", "newworld-{day}.log"),
    "woolworths": ("run_woolworths.sh", "woolworths-{day}.log"),
}


@dataclass
class Action:
    store: str
    kind: str  # "resume" (run the wrapper) or "post" (post the saved snapshot)
    why: str


def say(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def clock_is_synced() -> bool:
    try:
        done = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return done.stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        return False


def wait_for_clock(synced=clock_is_synced, sleep=time.sleep, timeout=CLOCK_WAIT_SECONDS, poll=CLOCK_POLL_SECONDS) -> bool:
    waited = 0
    while not synced():
        if waited >= timeout:
            return False
        sleep(poll)
        waited += poll
    return True


def log_path(store: str, day: str, root: Path) -> Path:
    return root / "logs" / STORES[store][1].format(day=day)


def snapshot_path(store: str, day: str, root: Path) -> Path:
    return root / "data" / SNAPSHOT_SUBDIRS[store] / f"{day}.json"


def last_result_state(path: Path) -> str:
    """"none" (no log), "finished" (last real line is a RESULT line) or "cut off".

    A "RESULT: SKIPPED" line is written by a run that found another one going; it says
    nothing about the run that was already under way, so it is passed over.
    """
    try:
        lines = [line.strip() for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return "none"
    lines = [line for line in lines if not line.startswith("RESULT: SKIPPED")]
    if not lines:
        return "none"
    return "finished" if lines[-1].startswith("RESULT:") else "cut off"


def plan(store: str, day: str, root: Path) -> Action | None:
    """What to do for this store's run today, if it was cut off."""
    if checkpoint.exists_today(store):
        return Action(store, "resume", "progress was saved earlier today and the run never finished")
    state = last_result_state(log_path(store, day, root))
    if state != "cut off":
        return None
    if snapshot_path(store, day, root).exists():
        return Action(store, "post", "today's log stops without a result, but the snapshot is saved - only posting was cut")
    return Action(store, "resume", "today's log stops without a result and nothing was saved - starting again")


def _attempts_file() -> Path:
    return Path(category_tree.STATE_DIR) / "resume_attempts.json"


def attempts_today(day: str) -> dict:
    try:
        data = json.loads(_attempts_file().read_text())
        if data.get("day") == day and isinstance(data.get("attempts"), dict):
            return data["attempts"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def record_attempt(store: str, day: str) -> int:
    attempts = attempts_today(day)
    attempts[store] = int(attempts.get(store, 0)) + 1
    path = _attempts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"day": day, "attempts": attempts}))
    tmp.replace(path)
    return attempts[store]


def load_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines of the env file (values are never printed)."""
    env: dict[str, str] = {}
    try:
        text = Path(path).expanduser().read_text()
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(line + "\n")


def run_action(action: Action, day: str, root: Path, run=subprocess.run, env_file: str = DEFAULT_ENV_FILE) -> int:
    log_file = log_path(action.store, day, root)
    stamp = datetime.now().strftime("%H:%M")
    if action.kind == "resume":
        script = root / STORES[action.store][0]
        _append(log_file, f"\n=== Started again after a restart at {stamp} ({action.why}) ===")
        return run(["bash", str(script)], cwd=str(root), check=False).returncode
    _append(log_file, f"\n=== Posting the saved snapshot after a restart at {stamp} ({action.why}) ===")
    command = [sys.executable, "-m", "scraper.post_run", "--site", action.store, "--date", day]
    env = {**os.environ, **load_env_file(env_file)}
    with open(log_file, "a") as out:
        code = run(command, cwd=str(root), env=env, stdout=out, stderr=subprocess.STDOUT, check=False).returncode
    _append(log_file, "RESULT: SUCCESS - saved snapshot posted after a restart" if code == 0 else f"RESULT: FAILED - posting after a restart exited with code {code}")
    return code


def main(
    argv: list[str] | None = None,
    root: Path = Path(REPO_ROOT),
    now=datetime.now,
    run=subprocess.run,
    synced=clock_is_synced,
    sleep=time.sleep,
    env_file: str = DEFAULT_ENV_FILE,
) -> int:
    parser = argparse.ArgumentParser(description="Carry on a scrape a power cut interrupted.")
    parser.add_argument("--dry-run", action="store_true", help="Say what would be done; start nothing.")
    args = parser.parse_args(argv)

    say("Checking for a scrape cut off by a restart." + (" (dry run)" if args.dry_run else ""))
    if not wait_for_clock(synced, sleep):
        say("The clock was not corrected after 10 minutes, so the date cannot be trusted. Doing nothing.")
        return 1
    moment = now()
    day = moment.date().isoformat()
    if moment.hour >= CUTOFF_HOUR:
        say(f"It is after {CUTOFF_HOUR}:00 - too late to start a scrape. Doing nothing.")
        return 0
    actions = [a for a in (plan(store, day, root) for store in STORES) if a]
    if not actions:
        say("No scrape was cut off today. Nothing to do.")
        return 0
    code = 0
    for action in actions:
        say(f"{action.store}: {action.why}.")
        if lock.is_held():
            say(f"{action.store}: not started - {lock.holder()} is running.")
            continue
        tried = int(attempts_today(day).get(action.store, 0))
        if tried >= MAX_ATTEMPTS_PER_DAY:
            say(f"{action.store}: not started - already tried {tried} times today.")
            continue
        verb = "start the full wrapper again" if action.kind == "resume" else "post the saved snapshot"
        if args.dry_run:
            say(f"{action.store}: would {verb}.")
            continue
        n = record_attempt(action.store, day)
        say(f"{action.store}: starting ({verb}), attempt {n} of {MAX_ATTEMPTS_PER_DAY}.")
        result = run_action(action, day, root, run, env_file)
        say(f"{action.store}: finished with exit code {result}.")
        code = code or (0 if result in (0, lock.EXIT_LOCKED) else result)
    return code


if __name__ == "__main__":
    sys.exit(main())
