# PAK'nSAVE Price Scraper

A read-only tool that visits PAK'nSAVE category pages, records each
product's name, size, price, and unit price, and builds up a price history
over time. It never logs in, never adds anything to a cart, and never
checks out — it only reads public category pages, the same as browsing the
site yourself.

## ⚠️ Known limitation — please read before trusting the output

This project was built and its automated tests were run in a sandboxed
environment whose network policy blocks reaching `paknsave.co.nz` entirely.
That means the specific page structure this scraper looks for (particular
`data-testid` attribute names PAK'nSAVE's website uses internally) is
carried over from a reference project's documentation and **has not been
confirmed against the live site**. The logic and the tests are solid — but
the very first thing to do after installing this on a machine that *can*
reach the real site is a dry run (see below) to confirm real products show
up correctly. If the table comes back empty, the site's internal structure
has likely changed and `scraper/extract.py` will need updating to match.

## Which store this scrapes

Every run is pinned to **PAK'nSAVE Petone** (114-124 Jackson Street,
Petone, Wellington, 5012). Without this, PAK'nSAVE silently defaults a new
browser session to whatever store its own IP-based geolocation guesses -
confirmed live, it picked PAK'nSAVE Royal Oak for the machine this was
built on - so prices would depend on wherever the scraper happens to run
from, not anything explicit or stable.

The pin works by setting two cookies (`STORE_ID_V2` and `eCom_STORE_ID`,
holding the store's UUID `98ec3885-ac93-4fcb-807b-59c9055c52c4`) on every
browser context before it navigates anywhere - see
`scraper.browser.new_pinned_context()`. This mirrors exactly what
PAK'nSAVE's own "Select a store to shop from" picker does when a shopper
manually chooses a store (confirmed by diffing `context.cookies()`
before/after a real manual selection); no geolocation permission is
involved, and the store does not appear in the URL.

If this scraper should ever track a different store, change
`PETONE_STORE_ID` (and the section name/comments around it) in
`scraper/browser.py` - don't just delete the pin, or prices will silently
drift back to whatever store IP geolocation guesses next.

## Installing

You'll need Python 3.10 or newer.

```bash
pip install -r requirements.txt
playwright install chromium
```

## Running a dry run (safe — writes nothing)

```bash
python -m scraper.run
```

This visits every category page listed in `categories.txt`, prints a table
of what it found, and a plain-language summary at the end. Nothing is
saved to disk. This is the default and the safe way to try things out or
test a change.

## Running for real (saves results)

```bash
python -m scraper.run --save
```

This does everything the dry run does, plus:
- writes a dated snapshot file to `data/snapshots/` (e.g. `2026-08-29.json`)
  containing every product found that day, and
- updates `data/price_history.json`, adding a new entry for a product only
  when its price actually changed since the last time it was seen (so the
  history file doesn't fill up with a duplicate entry every single day
  nothing changed).

The `data/` folder is not committed to this repository (see `.gitignore`)
— it's your local price history, not source code.

## Publishing to the recipe app (optional)

After a `--save` run finishes writing its local files, it can also send
that run's priced products (anything with a price at all - including
"each"/pack items with no per-unit price, e.g. a single cucumber or a
6-pack of frankfurters, sent with `unit: "each"` and their raw pack-size
string) to a recipe app's mailbox endpoint, so the app's own price book
can pick up fresh prices automatically. Only products with no price
whatsoever are left out.

This is entirely optional and off by default. To turn it on, set two
environment variables before running with `--save`:

```bash
export MAILBOX_URL="https://example.com/api/price-mailbox"
export MAILBOX_TOKEN="your-token-here"
python -m scraper.run --save
```

If either `MAILBOX_URL` or `MAILBOX_TOKEN` isn't set, publishing is
silently skipped with one clear line in the output - not an error.

Sending is always best-effort and never affects the scrape's own success:
if the mailbox can't be reached, times out, or rejects the request (no
internet, a wrong token, the app being down), the run still counts as a
full success. `data/snapshots/` and `data/price_history.json` are written
first and are always the real record, whether or not this last step
works. A dry run (no `--save`) never sends anything, ever.

## Weekly ingredient search

Besides the daily category scrape above, there's a second, separate mode:
searching PAK'nSAVE by name for specific recipe ingredients, rather than
only reading fixed category pages.

```bash
python -m scraper.search_run          # dry run - prints only, saves nothing
python -m scraper.search_run --save   # writes results for real
```

**Where the ingredient names come from.** The recipe app (the same one
`--save` can publish prices to) exposes a read-only list of every
ingredient name used across its recipes, at
`https://my-recipe-manager.netlify.app/api/ingredient-list`. Each run fetches
that list fresh - `scraper/ingredients.py` only ever reads it, never writes
back. If the list can't be fetched, the run stops there; there's nothing to
search for without it.

**How matching works.** For each ingredient name, the scraper searches
`https://www.paknsave.co.nz/shop/search?q=<name>&sf=shopping` (the same
mechanism, and the same `data-testid` product-tile structure, as the
category pages `scraper/extract.py` already reads) and takes the top
result - but only if it's a confident match. `scraper/matching.py` checks
whether the ingredient name and the top result's product name share at
least one meaningful word (e.g. "brown sugar, tightly packed" matching
"Pams Soft Brown Sugar" on "brown" and "sugar"). If nothing shared turns
up, or there are no results at all, that ingredient is skipped with an
honest **"no confident match"** - not an error. This is expected and
normal for ingredients that are imports, homemade items ("store-bought or
homemade pesto"), or things PAK'nSAVE simply doesn't sell - the run's
summary lists every skipped ingredient and why.

**Why this runs weekly, not daily.** Recipe ingredient prices don't need
same-day freshness the way a daily category scrape does, and searching by
name is one extra page load per ingredient rather than a handful of fixed
category pages - so this runs weekly instead of being folded into the
daily job. On top of that, `data/search_cache.json` tracks the date each
ingredient was last successfully found and skips re-searching anything
found within the last 7 days (`--stale-days` to change this), so even a
weekly run only actually searches what's actually gone stale.

**What `--save` does**, in addition to everything the dry run prints:
- writes a dated snapshot to `data/search_snapshots/`, separate from the
  daily scrape's `data/snapshots/`,
- adds new entries to the same `data/price_history.json` the daily scrape
  updates (a search-found product is still the same product),
- updates `data/search_cache.json` with today's date for every ingredient
  matched this run, and
- best-effort publishes matched products to the recipe app's mailbox, via
  the same `MAILBOX_URL`/`MAILBOX_TOKEN` publishing step described above.

A dry run never touches `search_cache.json` - only a `--save` run advances
which ingredients count as "recently found".

**Running it on a schedule.** `run_weekly.sh` wraps `python -m
scraper.search_run --save` the same way `run_daily.sh` wraps the category
scrape - logging to a dated file under `logs/` and ending it with a
`RESULT: ...` line. It's driven by its own cron entry, separate from and
slower than the daily job:

```
0 7 * * *   /path/to/run_daily.sh    # every day, 7:00am
0 8 * * 0   /path/to/run_weekly.sh   # Sundays only, 8:00am
```

## Adding a new category to scrape

Open `categories.txt` and add a line with the category page's URL. For example:

```
https://www.paknsave.co.nz/shop/category/pantry/pasta-rice--noodles category=pasta
```

Optional extras you can add after the URL:
- `pages=3` — also scrape the next 2 pages of this category (PAK'nSAVE
  paginates long category listings).
- `category=name` — label these products with a specific category name
  instead of the one automatically taken from the URL.

Lines starting with `#` are comments and are ignored.

## Fixing a product with a wrong or missing size

Sometimes a specific product's size can't be read correctly from the page.
Rather than changing the scraper's general logic for one odd product, add a
line to `overrides.txt` naming that product's ID:

```
P5019270 1020g
```

This tells the scraper "for this specific product, always use this size" —
and it will also recalculate that product's unit price to match.

If a specific product should be left out of the results entirely (e.g. it's
listed in the wrong category, or isn't a real product), write:

```
P1234567 skip
```

You can find a product's ID in the printed table's `ID` column, or in a
snapshot file in `data/snapshots/`.

## Running the tests

```bash
PYTHONPATH=. python tests/test_unit_price.py
PYTHONPATH=. python tests/test_extract.py
```

`test_unit_price.py` checks the price-rescaling math (e.g. "$0.49/100g"
correctly becomes "$4.90/kg"). `test_extract.py` runs the page-reading logic
against a saved sample page in `tests/fixtures/` rather than the live site,
so it can run anywhere without needing internet access.

## How results are recorded

Every product is saved with whatever fields could actually be read — a
blank size or unit price is a normal, honest result (for example, items
sold "each" genuinely have no comparable per-unit price), never a guess or
a placeholder number.

If a whole page fails to load, that page is skipped and the rest of the run
continues. If a specific product can't be read at all (e.g. no valid
price), only that one product is dropped, and the run's summary explains
why.
