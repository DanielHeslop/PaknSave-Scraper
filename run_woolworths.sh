#!/usr/bin/env bash
# Weekly wrapper for the Woolworths NZ scrape, meant to be driven by cron.
# Independent of run_weekly_combined.sh's PAK'nSAVE run - separate chain,
# separate transport (plain HTTP, no browser) - see scraper/woolworths.py's
# docstring. Scheduled on a different day (Wednesday) than the PAK'nSAVE
# job (Sunday) so the Pi isn't making two chains' worth of requests
# back-to-back on the same day from the same IP.
#
# - Runs the Woolworths scraper with --save.
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
LOG_FILE="logs/woolworths-$(date +%F).log"

# Run the Woolworths scraper, capturing both stdout and stderr into the log
# file. set -o pipefail (above) means $? below reflects the scraper's exit
# code, not tee's.
python3 -m scraper.woolworths --save >>"$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
    # scraper/woolworths.py prints a "Products saved              : N" line
    # as part of its WOOLWORTHS RUN SUMMARY on every successful run.
    SAVED="$(grep -oE 'Products saved[[:space:]]*:[[:space:]]*[0-9]+' "$LOG_FILE" | tail -n 1 | grep -oE '[0-9]+$')"
    if [ -n "$SAVED" ]; then
        echo "RESULT: SUCCESS - ${SAVED} products saved" >>"$LOG_FILE"
    else
        echo "RESULT: FAILED - scraper exited 0 but no product count found in output" >>"$LOG_FILE"
    fi
else
    # Try to surface a short reason from the tail of the log; fall back to
    # the exit code if nothing obviously error-shaped is there.
    REASON="$(tail -n 30 "$LOG_FILE" | grep -m1 -E 'UNEXPECTED ERROR|BLOCKED|Error|Traceback|Exception' )"
    if [ -z "$REASON" ]; then
        REASON="scraper exited with code ${EXIT_CODE}"
    fi
    echo "RESULT: FAILED - ${REASON}" >>"$LOG_FILE"
fi

exit "$EXIT_CODE"
