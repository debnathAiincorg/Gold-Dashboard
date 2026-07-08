"""
Fetch today's 22K gold rate (per gram, in INR) from Tanishq, plus two
Kolkata-specific sources: ABP Live and Times of India.

The Tanishq page is rendered client-side (Salesforce Commerce Cloud
storefront), and the site fronts requests with Cloudflare Bot Management,
which returns a 403 "Access Blocked" page to plain/default headless
browsers. All sources are fetched with the same realistic-fingerprint
headless Chromium setup for consistency, and each source has its own
try/except so one broken/blocked source doesn't stop the others from
printing.

Run:
    python gold_rate.py
    python gold_rate.py --debug   # print each source's matched text before parsing

Setup (once):
    pip install -r requirements.txt
    playwright install chromium
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Windows terminals often default to a legacy codepage (cp1252) that can't
# encode the ₹ symbol; force UTF-8 stdout so the output prints instead of
# crashing with a UnicodeEncodeError.
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

NAV_TIMEOUT_MS = 30_000
SELECTOR_TIMEOUT_MS = 20_000

# Written alongside the console output for gold_dashboard.html to read.
DATA_FILE = Path(__file__).resolve().parent / "gold_rate_data.json"

# Whether each source's rate is national or city-specific (Kolkata) -- mirrors
# the note printed at the bottom of the console output.
SOURCE_SCOPE = {
    "Tanishq": "national",
    "ABP Live": "city",
    "Times of India": "city",
}

# =============================================================================
# Shared helpers
# =============================================================================


def _launch_stealth_context(playwright):
    """Launch headless Chromium with a realistic fingerprint.

    A default headless context looks obviously automated (missing UA, small
    viewport, navigator.webdriver === true) and gets blocked by bot-detection
    (confirmed against Tanishq's Cloudflare Bot Management). These settings
    make it look like a normal desktop Chrome browsing from India. Shared by
    every source below.
    """
    browser = playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


def _parse_price(text):
    """Pull the first ₹ amount out of a string (handles thousands commas)."""
    match = re.search(r"₹\s*([\d,]+)", text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


# =============================================================================
# Tanishq (existing, verified logic -- unchanged aside from an optional
# `debug` diagnostic print, which is a no-op unless --debug is passed)
# =============================================================================

TANISHQ_URL = "https://www.tanishq.co.in/gold-rate.html?lang=en_IN"

# CSS class Tanishq currently uses for the 22K rate table. If the site changes
# its markup, update this selector first (right-click the "22 Kt Gold Rate"
# table in a real browser -> Inspect -> copy the table's class name).
RATE_TABLE_SELECTOR = "table.goldrate-table-22kt"

# The row we want is the one for a single gram; the site labels it "1 G".
PER_GRAM_ROW_LABEL = "1 G"


def fetch_rendered_page():
    """Open the Tanishq gold-rate page in headless Chromium and return the
    Page object (playwright + browser + page, so the caller can close them)."""
    playwright = sync_playwright().start()
    browser, context = _launch_stealth_context(playwright)
    page = context.new_page()
    page.goto(TANISHQ_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    return playwright, browser, page


def extract_22k_rate_per_gram(page, debug=False):
    """Find the 22K rate table and pull today's per-gram price out of it."""
    # Explicit wait for the table to actually be attached & visible, instead of
    # a fixed sleep -- the JS-rendered content isn't there right after goto().
    page.wait_for_selector(
        RATE_TABLE_SELECTOR, state="visible", timeout=SELECTOR_TIMEOUT_MS
    )
    table = page.query_selector(RATE_TABLE_SELECTOR)

    row = _find_row_by_first_cell(table, PER_GRAM_ROW_LABEL)
    if row is None:
        # Fallback: markup may have relabeled "1 G" -> just take the first
        # data row, which is the smallest grammage (per-gram) row on this page.
        row = table.query_selector("tbody tr")
    if row is None:
        raise ValueError("Could not find any rate row inside the 22K table.")

    if debug:
        print(f"[DEBUG][Tanishq] matched row inner_text: {row.inner_text()!r}")

    cells = row.query_selector_all("td")
    if len(cells) < 2:
        raise ValueError("22K rate row does not have a 'Today' price column.")

    today_cell_text = cells[1].inner_text()  # "Today" column
    price = _parse_price(today_cell_text)
    if price is None:
        raise ValueError(f"Could not parse a price out of: {today_cell_text!r}")
    return price


def _find_row_by_first_cell(table, label):
    for row in table.query_selector_all("tbody tr"):
        first_cell = row.query_selector("td")
        if first_cell and first_cell.inner_text().strip().lower() == label.lower():
            return row
    return None


# =============================================================================
# ABP Live (Kolkata) -- verified against live rendered DOM
# =============================================================================

ABP_URL = "https://news.abplive.com/gold-price-in-kolkata-west-bengal"


def extract_abp_22k_per_gram(page, debug=False):
    """ABP Live shows summary cards near the top of the page:
        <div class="assets-rate"><div class="rate">
            <strong>₹132,601</strong> ... <div class="description">22 Carat Gold Rate (10 grams)</div>
        </div></div>
    We match on the description *text* ("22" + "carat"/"karat") rather than a
    specific CSS class, since these look like auto-generated/CMS class names
    that could change on redeploy. The site states the price per 10 grams,
    so we divide by 10 to normalize to per-gram.
    """
    page.wait_for_selector("div.assets-rate", state="visible", timeout=SELECTOR_TIMEOUT_MS)

    matched_text = None
    price_per_10g = None
    for card in page.query_selector_all("div.assets-rate"):
        desc_el = card.query_selector(".description")
        if not desc_el:
            continue
        desc_text = desc_el.inner_text().strip()
        if re.search(r"22\s*(carat|karat)", desc_text, re.IGNORECASE):
            strong_el = card.query_selector("strong")
            if strong_el:
                matched_text = f"{desc_text!r} -> {strong_el.inner_text()!r}"
                price_per_10g = _parse_price(strong_el.inner_text())
            break

    if price_per_10g is None:
        # Text-based fallback: the page also states the rate in a plain
        # sentence, e.g. "...₹132,601 per 10 grams for 22-carat...".
        body_text = page.inner_text("body")
        m = re.search(
            r"₹\s*([\d,]+)\s*per\s*10\s*grams?\s*for\s*22[- ]?carat",
            body_text,
            re.IGNORECASE,
        )
        if m:
            matched_text = m.group(0)
            price_per_10g = int(m.group(1).replace(",", ""))

    if debug:
        print(f"[DEBUG][ABP Live] matched text: {matched_text!r}")

    if price_per_10g is None:
        raise ValueError("Could not find a 22 Carat rate on the ABP Live page.")

    return price_per_10g / 10


# =============================================================================
# Times of India (Kolkata) -- verified against live rendered DOM
# =============================================================================

TOI_URL = "https://timesofindia.indiatimes.com/business/gold-rates-today/gold-price-in-kolkata"


def extract_toi_22k_per_gram(page, debug=False):
    """TOI doesn't render the rate in a stable table -- there isn't one on
    this page. The rate is stated repeatedly in prose/meta text instead,
    e.g. "The gold rate today in Kolkata is ... ₹13,245 per gram for 22K".
    Verified directly on the page that this is already quoted PER GRAM
    (not per 10g like ABP Live), so no unit conversion is applied here.
    """
    page.wait_for_selector("body", state="visible", timeout=SELECTOR_TIMEOUT_MS)
    body_text = page.inner_text("body")

    m = re.search(r"₹\s*([\d,]+)\s*per\s*gram\s*for\s*22K", body_text, re.IGNORECASE)
    if not m:
        # Looser fallback: "...22K gold is priced at ₹13,245..." style phrasing.
        m = re.search(r"22K[^₹]{0,40}₹\s*([\d,]+)", body_text, re.IGNORECASE)

    if debug:
        matched = m.group(0) if m else None
        print(f"[DEBUG][Times of India] matched text: {matched!r}")

    if not m:
        raise ValueError("Could not find a 22K rate mentioned on the TOI page.")

    return int(m.group(1).replace(",", ""))


# =============================================================================
# Orchestration
# =============================================================================

# (label, url, extractor, note-shown-next-to-a-successful-price)
ADDITIONAL_SOURCES = (
    ("ABP Live", ABP_URL, extract_abp_22k_per_gram, "converted from per-10g"),
    ("Times of India", TOI_URL, extract_toi_22k_per_gram, None),
)


def fetch_source_price(url, extractor, debug=False):
    """Launch a fresh stealth browser, navigate to url, and run extractor(page)."""
    playwright = sync_playwright().start()
    browser = None
    try:
        browser, context = _launch_stealth_context(playwright)
        page = context.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if response is not None and response.status != 200:
            raise ValueError(
                f"HTTP {response.status} -- request was likely blocked (bot protection)."
            )
        return extractor(page, debug=debug)
    finally:
        if browser is not None:
            browser.close()
        playwright.stop()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch the 22K gold rate per gram from Tanishq and two Kolkata sources."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print each source's matched row/text before parsing it.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    results = []  # (label, price_per_gram_or_None, note_or_None, error_or_None)

    # --- Tanishq: existing, verified fetch/extract logic, untouched --------
    playwright = browser = None
    try:
        playwright, browser, page = fetch_rendered_page()
        price = extract_22k_rate_per_gram(page, debug=args.debug)
        results.append(("Tanishq", price, None, None))
    except PlaywrightTimeoutError as exc:
        results.append(("Tanishq", None, None, f"timed out waiting for the page/rate table to load ({exc})"))
    except PlaywrightError as exc:
        results.append(("Tanishq", None, None, f"could not load the page (network/browser error): {exc}"))
    except ValueError as exc:
        results.append(("Tanishq", None, None, f"page loaded but the 22K rate could not be found: {exc}"))
    except Exception as exc:  # noqa: BLE001 - keep one source's failure isolated
        results.append(("Tanishq", None, None, f"unexpected failure: {exc}"))
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    # --- Additional Kolkata sources -----------------------------------------
    for label, url, extractor, note in ADDITIONAL_SOURCES:
        try:
            price = fetch_source_price(url, extractor, debug=args.debug)
            results.append((label, price, note, None))
        except PlaywrightTimeoutError as exc:
            results.append((label, None, None, f"timed out waiting for content to load ({exc})"))
        except PlaywrightError as exc:
            results.append((label, None, None, f"could not load the page (network/browser error): {exc}"))
        except ValueError as exc:
            results.append((label, None, None, str(exc)))
        except Exception as exc:  # noqa: BLE001 - keep one source's failure isolated
            results.append((label, None, None, f"unexpected error: {exc}"))

    # --- Print combined results, one source at a time ------------------------
    timestamp = datetime.now().strftime("%d-%b-%Y %I:%M %p")
    print(f"22K Gold Rate (per gram) — {timestamp}")
    label_width = max(len(label) for label, *_ in results)
    any_success = False
    for label, price, note, error in results:
        padded_label = label.ljust(label_width)
        if error is not None:
            print(f"{padded_label} : ERROR - {error}")
        else:
            any_success = True
            suffix = f" ({note})" if note else ""
            print(f"{padded_label} : ₹{price:,.0f}{suffix}")
    print()
    print(
        "Note: Tanishq shows a national rate (not city-specific); "
        "ABP Live and Times of India are Kolkata-specific."
    )

    # --- Also write results to JSON for gold_dashboard.html (additive; the
    # console output above is unchanged by this) -----------------------------
    data = {
        "timestamp": timestamp,
        "sources": [
            {
                "name": label,
                "rate_per_gram": price,
                "note": note,
                "scope": SOURCE_SCOPE.get(label),
            }
            for label, price, note, error in results
            if error is None
        ],
    }
    try:
        DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"Warning: could not write {DATA_FILE.name}: {exc}")

    if not any_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
