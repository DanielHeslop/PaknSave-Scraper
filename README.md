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

Each item also carries a `unit_label` field - `map_unit_label()` in
`scraper/publish.py` applied to `unit` (e.g. `"kg"` -> `"per kg"`, `"each"`
-> `"ea"`) - a purely additive, human-readable form for display, added
identically across all three chains. `unit` itself is unchanged.

Every item also carries the pack and promo fields (built by
`scraper/item_fields.py`, with pack sizes read by `scraper/pack_size.py`):

- `price` + `unit` (kg, L, each or 100sheets): the price of ONE single item
  (one egg, one wipe, one roll) charged now. `unit_label` for 100sheets is
  "per 100 sheets".
- `pack_price` / `pack_count`: the price charged now for one pack, and how
  many single items it holds (1 for a single item). Counts come only from
  explicit text (subtitle, name, Woolworths packageType/volumeSize); if none
  is found the item is sent as unit each with `pack_count` 1.
- `regular_price` / `regular_pack_price`: the non-promo prices (equal to
  `price` / `pack_price` when there is no promo).
- `promo_type` ("sale", "member" or "multibuy"), `promo_label`, `promo_ends`,
  `promo_min_qty`: present only when a promo shows. Multibuy ("2 for $5")
  leaves `price` and `pack_price` at the regular values. Only public data
  is used; nothing logs in.
- `sheets_per_item` / `total_sheets`: only when actually known.
- `unit_unparsed`: the store's own unit-price text when it could not be read.

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

## Weekly run: categories, search, publish

Each chain has one weekly job (cron: Wednesday Woolworths, Friday New World,
Sunday Pak'nSave, all 8am). Every job does three things in order, and the
publish step runs even if the search step fails:

1. **Category scrape**, including the "watch" categories (bulk foods, kitchen
   and bathroom cleaners, stock, gravy, spices, spreads, toothpaste and so on).
2. **Search step** (time-boxed to about 20 minutes).
3. **Publish** to the recipe app's mailbox, de-duplicated by product id.

```bash
python -m scraper.weekly_combined                    # Pak'nSave, dry run
python -m scraper.weekly_combined --site newworld    # New World, dry run
python -m scraper.woolworths                         # Woolworths, dry run
# add --save to write, publish and report for real
```

`python -m scraper.search_run` is the same job as `weekly_combined`.

**What to search for.** `scraper/ingredients.py` asks the app for the store's
list (`GET ?store=paknsave|newworld|woolworths`, Bearer token): search terms,
watch terms, watch categories and a skip list. Skipped terms are not searched
(watch terms always are). If the list can't be fetched the step is skipped with
one log line; the old `fetch_ingredient_list` still works.

**Which matcher decides found / not found.** After scraping, every store sends its
terms (search terms not in skip, plus watch terms) and every product scraped this
run to the app's own matcher (`POST <site>/api/match-terms`, `scraper/app_matcher.py`;
`<site>` is the origin of `MAILBOX_URL`, Bearer `MAILBOX_TOKEN`; split into calls of
at most 300 terms and 6,000 products). So the app and the Pi agree. A term the app
found only through a fallback key is logged ("tamari: found via soy sauce"). If the
call fails for any reason (network, 30s timeout, any non-200) the run logs one line
and uses the local matcher below for the rest of the run; the run never fails
because of the endpoint. Not-found is still reported only when the term's category
was scraped (Woolworths: when its search ran). The `SUMMARY:` line says which
matcher was used (`app <version>` or `local fallback`) and how many terms were
found via a fallback key.

`python -m scraper.compare_matchers --site newworld --snapshot data/newworld_snapshots/<date>.json`
(env file sourced) compares the app's matcher with the local one on a saved
snapshot using the store's current terms. It publishes and reports nothing.

**Pak'nSave and New World never use their site search.** Their robots.txt
disallows `/shop/search`, so the local backup matcher works on the products the
category scrape just collected (`scraper/search_step.py`, `scraper/matching.py`:
every main word of the term must be in the product name, with plurals and
spellings like yoghurt/yogurt folded; other forms such as powder, mix, sauce or
chips, and flavour-only mentions like "garlic & herb chicken", are rejected;
descriptors like light/dry/frozen are dropped once if nothing matches; single
generic words like dry/white/light never match alone). A term is reported **not found**
only if a category that should hold it was scraped this run and nothing matched;
otherwise it is left unreported. Terms that matched are reported **found**.
Add `covers=word,word` to a category line to say which words it holds.

**Category coverage (Pak'nSave and New World, identical label sets).** Besides
the department-level pages, each store file has one narrow category per common
product type (butter, cream cheese, sour cream, fresh cream, milk powder,
garlic/ginger, onions, potatoes, carrots, lettuce, citrus, bacon, mince, oats,
everyday cereal for Weet-Bix, tuna, canned salmon/sardines, mayonnaise, salad
dressing, pickles, tomato paste, Worcestershire, soy and Asian sauces, cooking
oil, baking nuts, fruit juice, burger buns). The words that used to sit on a
broad parent (e.g. `garlic` on vegetables) were moved to the narrow category, so
a term is "not found" only when the page that should hold it was scraped. Wine
has no category: bottles are named by brand and grape, so wine terms stay
unreported. Which term belongs to which category is recorded in
`tests/fixtures/term_categories.json` and checked by
`tests/test_category_coverage.py`, which also checks every URL against a saved
copy of each site's robots.txt (`tests/fixtures/robots_*.txt`). Matching is by
any shared word, so a generic word on a category (e.g. `cream`) can match
unrelated products.

**Woolworths** uses its public JSON API: the browse API for the watch categories
in `woolworths_categories.txt`, and the product-search API for each term (with a
short delay between requests). robots.txt disallows only `/shop/search` (the HTML
page), not `/api/v1/products`. A term is not found when the search ran and nothing
sensible came back.

**robots.txt.** Every scraper fetches robots.txt once per run per site
(`scraper/robots.py`) and skips, with a log line, any disallowed path. If
robots.txt itself can't be fetched, the run carries on. Foodstuffs page loads
are spaced a few seconds apart; Woolworths requests too.

**Logs.** Each log ends with a `SUMMARY:` line (products published, terms
searched, found, not found, skipped, categories scraped, matcher used, terms found
via a fallback key, run time) and then the `RESULT:` line.

**Secrets.** `MAILBOX_URL` and `MAILBOX_TOKEN` live in
`~/.config/paknsave-scraper/env` (outside the repo, chmod 600). The run scripts
source it and stop with a clear message if it is missing. See `env.example`.

`run_daily.sh` (category scrape only, `scraper.run`) and `run_weekly.sh` (which
now runs the same weekly job as above) still work by hand but are not in cron.

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
- `covers=word,word` — words this category holds, so a search term that
  matches nothing can fairly be reported "not found" (see above).

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
