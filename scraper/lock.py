"""One scrape at a time: a lock file in the data directory.

Taken by every entry point that scrapes (a cron run, a resume after a restart, a run by
hand). The lock is held by an open file, so it goes away with the process, including when
the Pi loses power or the process is killed - there is never a stale lock to clear.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from scraper import category_tree

EXIT_LOCKED = 75  # EX_TEMPFAIL: not run, try again later


class ScrapeLocked(Exception):
    """Another scrape holds the lock; the message says which."""


def lock_path() -> Path:
    return Path(category_tree.STATE_DIR) / "scrape.lock"


def holder() -> str:
    try:
        return lock_path().read_text().strip() or "another scrape"
    except OSError:
        return "another scrape"


def is_held() -> bool:
    path = lock_path()
    if not path.exists():
        return False
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


@contextmanager
def scrape_lock(what: str):
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ScrapeLocked(f"{holder()} is running") from None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{what} (pid {os.getpid()}, started {datetime.now().isoformat(timespec='seconds')})")
        handle.flush()
        yield
    finally:
        try:
            handle.seek(0)
            handle.truncate()
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def run_locked(what: str, run, log) -> int:
    """Run `run()` (returns an exit code) under the lock; EXIT_LOCKED if another scrape holds it."""
    try:
        with scrape_lock(what):
            return run()
    except ScrapeLocked as e:
        log(f"Not started: {e} (lock file {lock_path()}). Only one scrape runs at a time.")
        return EXIT_LOCKED
