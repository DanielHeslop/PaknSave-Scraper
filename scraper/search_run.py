"""Search-driven ingredient lookup for Pak'nSave / New World.

The sites' own search pages (/shop/search) are disallowed by robots.txt, so
this no longer requests them. The search now happens locally: the job scrapes
the category pages, then matches the app's search and watch terms against the
products it just collected (scraper/search_step.py), reports found / not-found
to the app, and publishes. That is exactly scraper.weekly_combined's job, so
this entry point simply runs it.

    python -m scraper.search_run --site paknsave          # dry run
    python -m scraper.search_run --site newworld --save   # for real
"""

from __future__ import annotations

import asyncio
import sys

from scraper.weekly_combined import main_async, parse_args


def main() -> None:
    sys.exit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()
