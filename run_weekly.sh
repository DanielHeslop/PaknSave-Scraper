#!/usr/bin/env bash
# Weekly wrapper for the search-driven ingredient scrape, meant to be driven
# by cron. Separate from run_daily.sh's category scrape - see README.md's
# "Weekly ingredient search" section for why this runs on its own slower
# schedule instead of being folded into the daily job.
#
# - Runs the search-driven scraper with --save.
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
LOG_FILE="logs/weekly-$(date +%F).log"

# Run the search scraper, capturing both stdout and stderr into the log
# file. set -o pipefail (above) means $? below reflects the scraper's exit
# code, not tee's.
python3 -m scraper.search_run --save >>"$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    # scraper/search_run.py prints an "Ingredients matched        : N" line
    # as part of its SEARCH RUN SUMMARY on every successful run.
    MATCHED="$(grep -oE 'Ingredients matched[[:space:]]*:[[:space:]]*[0-9]+' "$LOG_FILE" | tail -n 1 | grep -oE '[0-9]+$')"
    if [ -n "$MATCHED" ]; then
        echo "RESULT: SUCCESS - ${MATCHED} ingredients matched" >>"$LOG_FILE"
    else
        echo "RESULT: FAILED - scraper exited 0 but no matched count found in output" >>"$LOG_FILE"
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
