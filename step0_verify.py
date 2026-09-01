#!/usr/bin/env python3
"""
step0_verify.py

Standalone sanity check: opens a headless Playwright browser, navigates to the
PAK'nSAVE vegetables category page, and reports whether specific HTML
markers we plan to scrape actually exist on the live page today.

This machine has no display, so headless=True is the only viable mode.

Run:
    python3 step0_verify.py
"""

import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://www.paknsave.co.nz/shop/category/fruit-and-vegetables/vegetables"

# Regex for PAK'nSAVE's own displayed unit price, e.g. "$4.99/1kg" or "$0.50/100g"
UNIT_PRICE_RE = re.compile(r"\$\d+(?:\.\d+)?\s*/\s*\d*[a-zA-Z]+")


def confirmed(label, detail):
    print(f"[CONFIRMED] {label}")
    print(f"    {detail}")


def not_found(label, detail):
    print(f"[NOT FOUND] {label}")
    print(f"    {detail}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"Navigating to: {URL}")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        # Give client-side rendering time to populate product tiles.
        try:
            page.wait_for_selector('[data-testid*="-EA-000"], [data-testid*="-KGM-000"], [data-testid="price-dollars"]', timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

        html = page.content()
        print(f"\nTotal HTML length after render: {len(html)} chars\n")
        print("=" * 70)

        # (a) data-testid="price-dollars"
        loc = page.locator('[data-testid="price-dollars"]')
        count = loc.count()
        if count > 0:
            text = loc.first.inner_text().strip()
            outer = loc.first.evaluate("el => el.outerHTML")
            confirmed(
                '(a) data-testid="price-dollars" exists and contains a price',
                f'count={count}, first text="{text}", html: {outer[:200]}',
            )
        else:
            not_found(
                '(a) data-testid="price-dollars"',
                "No elements matched this selector.",
            )

        # (b) data-testid="price-cents"
        loc = page.locator('[data-testid="price-cents"]')
        count = loc.count()
        if count > 0:
            text = loc.first.inner_text().strip()
            outer = loc.first.evaluate("el => el.outerHTML")
            confirmed(
                '(b) data-testid="price-cents" exists',
                f'count={count}, first text="{text}", html: {outer[:200]}',
            )
        else:
            not_found(
                '(b) data-testid="price-cents"',
                "No elements matched this selector.",
            )

        # (c) data-testid="product-subtitle"
        loc = page.locator('[data-testid="product-subtitle"]')
        count = loc.count()
        if count > 0:
            text = loc.first.inner_text().strip()
            outer = loc.first.evaluate("el => el.outerHTML")
            confirmed(
                '(c) data-testid="product-subtitle" exists and contains a size',
                f'count={count}, first text="{text}", html: {outer[:200]}',
            )
        else:
            not_found(
                '(c) data-testid="product-subtitle"',
                "No elements matched this selector.",
            )

        # (d) data-testid containing "-EA-000" or "-KGM-000"
        loc = page.locator('[data-testid*="-EA-000"], [data-testid*="-KGM-000"]')
        count = loc.count()
        if count > 0:
            first_testid = loc.first.get_attribute("data-testid")
            confirmed(
                '(d) product tile data-testid contains "-EA-000" or "-KGM-000"',
                f'count={count}, first data-testid="{first_testid}"',
            )
        else:
            not_found(
                '(d) data-testid containing "-EA-000" or "-KGM-000"',
                "No elements matched either substring selector.",
            )

        # (e) "$X.XX/Yunit" pattern anywhere in page text
        body_text = page.locator("body").inner_text()
        matches = UNIT_PRICE_RE.findall(body_text)
        if matches:
            confirmed(
                '(e) "$X.XX/Yunit" style unit-price text present on the page',
                f'count={len(matches)}, examples: {matches[:5]}',
            )
        else:
            not_found(
                '(e) "$X.XX/Yunit" unit-price pattern',
                "Regex found no matches in the rendered page's visible text.",
            )

        print("=" * 70)

        all_not_found = all(
            [
                page.locator('[data-testid="price-dollars"]').count() == 0,
                page.locator('[data-testid="price-cents"]').count() == 0,
                page.locator('[data-testid="product-subtitle"]').count() == 0,
                page.locator('[data-testid*="-EA-000"], [data-testid*="-KGM-000"]').count() == 0,
                len(matches) == 0,
            ]
        )

        if all_not_found:
            print("\nAll five checks came back NOT FOUND. Checking whether the raw")
            print("HTML contains real product data or is an empty JS-rendering shell...\n")

            # Heuristics for "real content" vs "empty shell"
            price_dollar_signs = html.count("$")
            has_product_word = "product" in html.lower()
            has_react_root = bool(re.search(r'id="root"|id="__next"|ng-version', html))
            body_visible_len = len(body_text.strip())

            is_cloudflare_challenge = bool(
                re.search(r"security verification|Cloudflare|Ray ID|cf-error", html, re.IGNORECASE)
                or re.search(r"security verification|Cloudflare|Ray ID", body_text, re.IGNORECASE)
            )

            print(f"  '$' characters in raw HTML: {price_dollar_signs}")
            print(f"  Contains the word 'product' (case-insensitive): {has_product_word}")
            print(f"  Looks like an SPA root div (root/__next/ng-version): {has_react_root}")
            print(f"  Rendered visible body text length: {body_visible_len} chars")
            print(f"  Looks like a Cloudflare bot-check/interstitial page: {is_cloudflare_challenge}")
            print(f"  Full visible body text:\n{body_text.strip()!r}")

            if is_cloudflare_challenge:
                print(
                    "\n  => Page is a CLOUDFLARE BOT-CHECK INTERSTITIAL, not our target "
                    "category page and not a slow-rendering SPA. Waiting longer will not "
                    "help — this is anti-bot detection blocking the plain headless request, "
                    "not the labels having changed or the content simply loading slowly."
                )
            elif price_dollar_signs == 0 and body_visible_len < 500:
                print(
                    "\n  => Page looks like an EMPTY SHELL: little/no visible text or "
                    "prices even after waiting. Content may need more time, a different "
                    "wait condition, or is blocked (e.g. bot detection/redirect)."
                )
            else:
                print(
                    "\n  => Page DOES contain visible text/prices, but not under the "
                    "expected data-testid labels — labels have likely changed rather "
                    "than the content being missing."
                )

        browser.close()


if __name__ == "__main__":
    sys.exit(main())
