#!/usr/bin/env bash
# Weekly wrapper for the combined category-scrape + ingredient-search run,
# meant to be driven by cron. Replaces the separate run_daily.sh (category
# scrape) and run_weekly.sh (ingredient search) cron jobs with a single
# weekly job - see scraper/weekly_combined.py's docstring and README.md's
# "Weekly ingredient search" section for why the two were merged and how
# redundant by-name searches are skipped.
#
# run_daily.sh and run_weekly.sh themselves are unchanged and still work if
# run by hand - only their cron schedule was retired in favour of this.
#
# - Runs the combined scraper with --save.
# - Writes all output (stdout + stderr) to a dated log file under logs/.
# - Always appends one final "RESULT: ..." line so a human can check the
#   last line of any week's log and instantly know success/failure, without
#   reading the whole thing.
set -uo pipefail

export MAILBOX_URL="https://my-recipe-manager.netlify.app/api/price-mailbox"
export MAILBOX_TOKEN="pns-scrape-7f3k9x2m4q8w1z6v"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p logs
LOG_FILE="logs/weekly-combined-$(date +%F).log"

# Run the combined scraper, capturing both stdout and stderr into the log
# file. set -o pipefail (above) means $? below reflects the scraper's exit
# code, not tee's.
python3 -m scraper.weekly_combined --save >>"$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    # scraper/weekly_combined.py prints both scraper.run's "Products saved"
    # line (category scrape) and scraper.search_run's "Ingredients matched"
    # line (search part) as part of its output.
    SAVED="$(grep -oE 'Products saved[[:space:]]*:[[:space:]]*[0-9]+' "$LOG_FILE" | tail -n 1 | grep -oE '[0-9]+$')"
    MATCHED="$(grep -oE 'Ingredients matched[[:space:]]*:[[:space:]]*[0-9]+' "$LOG_FILE" | tail -n 1 | grep -oE '[0-9]+$')"
    if [ -n "$SAVED" ] && [ -n "$MATCHED" ]; then
        echo "RESULT: SUCCESS - ${SAVED} category products saved, ${MATCHED} ingredients matched by search" >>"$LOG_FILE"
    else
        echo "RESULT: FAILED - scraper exited 0 but expected summary counts not found in output" >>"$LOG_FILE"
    fi
else
    # Try to surface a short reason from the tail of the log; fall back to
    # the exit code if nothing obviously error-shaped is there.
    REASON="$(tail -n 30 "$LOG_FILE" | grep -m1 -E 'UNEXPECTED ERROR|Error|Traceback|Exception' )"
    if [ -z "$REASON" ]; then
        REASON="scraper exited with code ${EXIT_CODE}"
    fi
    echo "RESULT: FAILED - ${REASON}" >>"$LOG_FILE"
fi

exit "$EXIT_CODE"
