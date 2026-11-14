"""Post a saved snapshot to the app as a run (without scraping again).

    python -m scraper.post_run --site newworld                    # latest snapshot, posts
    python -m scraper.post_run --site paknsave --date 2026-11-08  # a given day
    python -m scraper.post_run --site woolworths --dry-run        # plan only

Uses the same run id, parts and checks as a normal run, so posting a day again
replaces its parts rather than adding a second run. Needs MAILBOX_URL and
MAILBOX_TOKEN in the environment (source the env file first) unless --dry-run.
--legacy-mailbox posts to the old mailbox instead.
"""

from __future__ import annotations

import argparse
import os
import sys

from scraper.price_runs import REPO_ROOT, SNAPSHOT_SUBDIRS, latest_snapshot, log, post_snapshot_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post a saved snapshot to the app as a price run.")
    parser.add_argument("--site", required=True, choices=sorted(SNAPSHOT_SUBDIRS))
    parser.add_argument("--date", default=None, help="Snapshot date YYYY-MM-DD (default: the latest snapshot).")
    parser.add_argument("--snapshots-dir", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Show the run id, parts and sizes; post nothing.")
    parser.add_argument("--legacy-mailbox", action="store_true", help="Post to the old mailbox instead of /api/price-runs.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    directory = args.snapshots_dir or os.path.join(REPO_ROOT, "data", SNAPSHOT_SUBDIRS[args.site])
    if args.date:
        path = os.path.join(directory, f"{args.date}.json")
    else:
        found = latest_snapshot(args.site, directory)
        if found is None:
            log(f"No snapshots found in {directory}.")
            return 1
        path = str(found)
    result = post_snapshot_file(path, args.site, dry_run=args.dry_run, legacy=args.legacy_mailbox)
    if result is None:
        return 1
    if args.dry_run:
        return 0
    log(f"SUMMARY: {result.summary() or 'nothing posted'}")
    return 0 if (result.posted or result.note == "legacy mailbox") else 1


if __name__ == "__main__":
    sys.exit(main())
