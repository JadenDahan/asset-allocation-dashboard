"""
scrapers/ndr_scraper.py
========================
Ned Davis Research — House Views scraper.

Logs into ndr.com using NDR_USER and NDR_PASS environment variables
(stored as GitHub Secrets), navigates to the Model Portfolios /
Dynamic Allocation Strategy page, and extracts current signals.

NDR's portal is JavaScript-rendered so we use Playwright (headless
Chromium) which GitHub Actions supports natively.

Returns a dict that update_data.py writes into allocations.json.
"""

import os
import re
import json
import logging

logger = logging.getLogger(__name__)

# ── Credentials come from GitHub Secrets ──
NDR_USER = os.environ.get("NDR_USER", "")
NDR_PASS = os.environ.get("NDR_PASS", "")

# ── NDR portal URLs ──
NDR_LOGIN     = "https://www.ndr.com/group/ndr"
NDR_PORTFOLIO = "https://www.ndr.com/hs/model-portfolios"
NDR_SIGNALS   = "https://info.ndr.com/ndr-signals"

# ── Asset label → canonical ID mapping ──
NDR_ASSET_MAP = {
    "large-cap u.s.":            "us-lc",
    "large cap u.s.":            "us-lc",
    "u.s. large cap":            "us-lc",
    "u.s. large-cap":            "us-lc",
    "small-cap u.s.":            "us-sc",
    "small cap u.s.":            "us-sc",
    "u.s. small cap":            "us-sc",
    "u.s. small-cap":            "us-sc",
    "u.s. tech/growth":          "us-gr",
    "u.s. growth":               "us-gr",
    "growth":                    "us-gr",
    "u.s. value":                "us-val",
    "value":                     "us-val",
    "u.s. equities":             "us-eq",
    "u.s. equity":               "us-eq",
    "global equities":           "intl-eq",
    "international equities":    "intl-eq",
    "international equity":      "intl-eq",
    "developed international":   "dev-intl",
    "eafe":                      "dev-intl",
    "emerging markets equity":   "em-eq",
    "emerging markets equities": "em-eq",
    "emerging market":           "em-eq",
    "long-term u.s. treasurys":  "govt-us",
    "long-term u.s. treasuries": "govt-us",
    "u.s. treasuries":           "govt-us",
    "government bonds":          "govt-us",
    "international bonds":       "intl-debt",
    "international debt":        "intl-debt",
    "investment grade":          "ig-credit",
    "ig credit":                 "ig-credit",
    "high yield":                "hy-bonds",
    "emerging markets debt":     "em-bonds",
    "em debt":                   "em-bonds",
    "gold":                      "gold",
    "commodities":               "oil",
    "oil":                       "oil",
    "energy":                    "oil",
    "cash":                      None,  # handled separately in broad
}

# ── Signal language → code mapping ──
NDR_SIGNAL_MAP = {
    "heavy overweight":   "HOW",
    "strong overweight":  "HOW",
    "maximum overweight": "HOW",
    "overweight":         "OW",
    "above weight":       "OW",
    "above benchmark":    "OW",
    "neutral":            "N",
    "market weight":      "N",
    "benchmark weight":   "N",
    "underweight":        "UW",
    "below weight":       "UW",
    "below benchmark":    "UW",
    "heavy underweight":  "HUW",
    "strong underweight": "HUW",
    "maximum underweight":"HUW",
}


def fetch_ndr():
    """
    Main entry point.
    Returns:
    {
      "status":    "live" | "failed",
      "message":   str,
      "retrieved": str,
      "broad":     { "equity": int, "debt": int, "cash": int, "note": str },
      "signals":   { "us-eq": "OW", ... }
    }
    """
    if not NDR_USER or not NDR_PASS:
        return _failed("NDR_USER or NDR_PASS secrets not set in GitHub Secrets.")

    # Try Playwright first (handles JS-rendered pages)
    try:
        return _fetch_via_playwright()
    except ImportError:
        logger.warning("NDR: Playwright not installed, trying requests fallback")
    except Exception as e:
        logger.warning("NDR: Playwright attempt failed (%s), trying requests fallback", e)

    # Fallback: plain HTTP session (works if NDR serves HTML without JS)
    try:
        return _fetch_via_requests()
    except Exception as e:
        return _failed(f"NDR: both Playwright and requests methods failed. Last error: {e}")


# ─────────────────────────────────────────────────────────────────
# METHOD 1: Playwright (headless Chromium — handles JS-rendered pages)
# GitHub Actions installs this via requirements.txt
# ─────────────────────────────────────────────────────────────────
def _fetch_via_playwright():
    from playwright.sync_api import sync_playwright

    logger.info("NDR: starting Playwright headless browser")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()

        # ── Navigate to NDR login ──
        logger.info("NDR: loading login page %s", NDR_LOGIN)
        page.goto(NDR_LOGIN, wait_until="networkidle", timeout=60000)

        # ── Wait for login form ──
        # NDR uses a standard username/password form
        page.wait_for_selector("input[name='login'], input[type='email'], input[name='email']",
                               timeout=15000)

        # ── Fill credentials ──
        # Try common NDR field names — update if their form changes
        for selector in ["input[name='login']", "input[name='email']", "input[type='email']"]:
            if page.query_selector(selector):
                page.fill(selector, NDR_USER)
                break

        for selector in ["input[name='password']", "input[type='password']"]:
            if page.query_selector(selector):
                page.fill(selector, NDR_PASS)
                break

        # ── Submit form ──
        page.click("button[type='submit'], input[type='submit'], button:has-text('Log in'), button:has-text('Sign in')")
        page.wait_for_load_state("networkidle", timeout=30000)

        current_url = page.url
        logger.info("NDR: post-login URL: %s", current_url)

        # ── Verify login succeeded ──
        if "login" in current_url.lower() or "sign-in" in current_url.lower():
            browser.close()
            return _failed(
                f"NDR login failed — credentials were rejected or form fields changed. "
                f"Final URL: {current_url}. Check NDR_USER and NDR_PASS in GitHub Secrets."
            )

        logger.info("NDR: login succeeded. Navigating to model portfolios...")

        # ── Navigate to Dynamic Allocation Strategy ──
        page.goto(NDR_PORTFOLIO, wait_until="networkidle", timeout=30000)

        # Wait for the allocation table to render
        try:
            page.wait_for_selector("table, .allocation-table, .model-portfolio, .asset-weights",
                                   timeout=15000)
        except Exception:
            logger.warning("NDR: allocation table selector not found, proceeding with full page")

        html_content = page.content()
        title        = page.title()
        browser.close()

    signals, broad = _parse_ndr_html(html_content)

    if not signals:
        return _failed(
            "NDR: Playwright logged in and navigated successfully, but could not parse "
            "signals from the model portfolio page. The page layout may have changed. "
            f"Page title was: '{title}'. Manual update required."
        )

    logger.info("NDR: extracted %d signals via Playwright", len(signals))
    return {
        "status":    "live",
        "message":   f"Successfully retrieved via Playwright headless browser. Page: {title}",
        "retrieved": f"NDR Model Portfolios — {title}",
        "broad":     broad,
        "signals":   signals
    }


# ─────────────────────────────────────────────────────────────────
# METHOD 2: Plain HTTP session (fallback, may not work on JS pages)
# ─────────────────────────────────────────────────────────────────
def _fetch_via_requests():
    import requests
    from bs4 import BeautifulSoup

    logger.info("NDR: attempting requests-based login")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    })

    # Load login page to capture hidden fields
    login_page = session.get(NDR_LOGIN, timeout=30)
    login_page.raise_for_status()
    soup = BeautifulSoup(login_page.text, "html.parser")

    hidden = {}
    form = soup.find("form")
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            if inp.get("name"):
                hidden[inp["name"]] = inp.get("value", "")

    payload = {
        "login":    NDR_USER,
        "email":    NDR_USER,
        "password": NDR_PASS,
        **hidden
    }

    r = session.post(NDR_LOGIN, data=payload, timeout=30, allow_redirects=True)
    r.raise_for_status()

    if "login" in r.url.lower():
        return _failed(f"NDR requests login failed. Final URL: {r.url}")

    portfolio_r = session.get(NDR_PORTFOLIO, timeout=30)
    portfolio_r.raise_for_status()

    signals, broad = _parse_ndr_html(portfolio_r.text)

    if not signals:
        return _failed("NDR: requests login worked but page content could not be parsed.")

    return {
        "status":    "live",
        "message":   "Retrieved via HTTP session (requests). Note: JS-rendered content may be incomplete.",
        "retrieved": NDR_PORTFOLIO,
        "broad":     broad,
        "signals":   signals
    }


# ─────────────────────────────────────────────────────────────────
# PARSER — works on HTML from either method
# ─────────────────────────────────────────────────────────────────
def _parse_ndr_html(html):
    """
    Extract signals and broad allocation from NDR HTML.
    Returns (signals_dict, broad_dict).
    Update the selectors below if NDR changes their page layout.
    """
    from bs4 import BeautifulSoup

    soup    = BeautifulSoup(html, "html.parser")
    signals = {}
    broad   = {
        "equity": 57, "debt": 31, "cash": 12,
        "note": "NDR Dynamic Allocation Strategy — retrieved from ndr.com"
    }

    # ── Try to find a weights / allocation table ──
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            label  = cells[0].lower()
            stance = cells[-1].lower()  # signal is typically in last column

            asset_id = _map_ndr_asset(label)
            signal   = _map_ndr_signal(stance)

            if asset_id and signal:
                signals[asset_id] = signal

            # Parse broad allocation percentages
            pct = re.search(r"(\d+\.?\d*)\s*%", stance)
            if pct:
                val = float(pct.group(1))
                if "equity" in label or "stock" in label:
                    broad["equity"] = int(val)
                elif "bond" in label or "fixed" in label or "debt" in label:
                    broad["debt"] = int(val)
                elif "cash" in label or "money market" in label:
                    broad["cash"] = int(val)

    # ── Scan full text for signal language if table parse found nothing ──
    if not signals:
        text = soup.get_text(separator="\n")
        for line in text.split("\n"):
            line_lower = line.lower().strip()
            for asset_phrase, asset_id in NDR_ASSET_MAP.items():
                if asset_phrase in line_lower and asset_id:
                    for sig_phrase, sig_code in NDR_SIGNAL_MAP.items():
                        if sig_phrase in line_lower:
                            signals[asset_id] = sig_code
                            break

    return signals, broad


def _map_ndr_asset(label):
    label = label.lower().strip()
    for phrase, asset_id in NDR_ASSET_MAP.items():
        if phrase in label:
            return asset_id
    return None


def _map_ndr_signal(stance):
    stance = stance.lower().strip()
    for phrase, code in NDR_SIGNAL_MAP.items():
        if phrase in stance:
            return code
    return None


def _failed(message):
    logger.error("NDR: %s", message)
    return {
        "status":    "failed",
        "message":   message,
        "retrieved": "none",
        "broad":     {"equity": 57, "debt": 31, "cash": 12, "note": "Fallback — NDR retrieval failed"},
        "signals":   {}
    }
