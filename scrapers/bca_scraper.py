"""
scrapers/bca_scraper.py
========================
BCA Research — Global Investment Strategy FullView Portfolio scraper.

Logs into bcaresearch.com using BCA_USER and BCA_PASS environment
variables (stored as GitHub Secrets), navigates to the GIS FullView
Portfolio page, and extracts current tactical asset allocation signals.

Returns a dict that update_data.py writes into allocations.json.
"""

import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Credentials come from GitHub Secrets (never hardcoded) ──
BCA_USER = os.environ.get("BCA_USER", "")
BCA_PASS = os.environ.get("BCA_PASS", "")

# ── BCA portal URLs ──
BCA_BASE     = "https://www.bcaresearch.com"
BCA_LOGIN    = "https://www.bcaresearch.com/user/login"
BCA_GIS_HOME = "https://www.bcaresearch.com/site/gis/home"
BCA_GAA_HOME = "https://www.bcaresearch.com/site/gaa/home"
BCA_ALLOC    = "https://www.bcaresearch.com/topic?name=Asset+Allocation"

# ── Map BCA's asset class labels to our canonical IDs ──
# Update these if BCA changes their terminology
BCA_ASSET_MAP = {
    "us equities":               "us-eq",
    "u.s. equities":             "us-eq",
    "united states":             "us-eq",
    "international equities":    "intl-eq",
    "developed markets":         "dev-intl",
    "eafe":                      "dev-intl",
    "europe":                    "dev-intl",
    "u.s. large":                "us-lc",
    "large cap":                 "us-lc",
    "large-cap":                 "us-lc",
    "u.s. small":                "us-sc",
    "small cap":                 "us-sc",
    "small-cap":                 "us-sc",
    "u.s. growth":               "us-gr",
    "growth":                    "us-gr",
    "u.s. value":                "us-val",
    "value":                     "us-val",
    "emerging markets equity":   "em-eq",
    "em equities":               "em-eq",
    "emerging markets":          "em-eq",
    "u.s. treasuries":           "govt-us",
    "government bonds":          "govt-us",
    "us government":             "govt-us",
    "international bonds":       "intl-debt",
    "international debt":        "intl-debt",
    "investment grade":          "ig-credit",
    "ig credit":                 "ig-credit",
    "high yield":                "hy-bonds",
    "emerging markets debt":     "em-bonds",
    "em bonds":                  "em-bonds",
    "hard currency":             "em-bonds",
    "gold":                      "gold",
    "oil":                       "oil",
    "energy":                    "oil",
    "commodities":               "oil",
}

# ── Map BCA's signal language to our codes ──
BCA_SIGNAL_MAP = {
    "maximum overweight":  "HOW",
    "strong overweight":   "HOW",
    "heavy overweight":    "HOW",
    "overweight":          "OW",
    "slight overweight":   "OW",
    "above benchmark":     "OW",
    "neutral":             "N",
    "market weight":       "N",
    "benchmark":           "N",
    "slight underweight":  "UW",
    "underweight":         "UW",
    "below benchmark":     "UW",
    "strong underweight":  "HUW",
    "heavy underweight":   "HUW",
    "maximum underweight": "HUW",
    # MacroQuant numeric scores
    "+2": "HOW",
    "+1": "OW",
    "0":  "N",
    "-1": "UW",
    "-2": "HUW",
}


def fetch_bca():
    """
    Main entry point. Returns:
    {
      "status":     "live" | "failed",
      "message":    "human-readable description of what happened",
      "retrieved":  "page title or URL that was read",
      "broad": { "equity": int, "debt": int, "cash": int, "note": str },
      "signals": { "us-eq": "OW", ... }
    }
    """
    if not BCA_USER or not BCA_PASS:
        return _failed("BCA_USER or BCA_PASS secrets not set in GitHub Secrets.")

    logger.info("BCA: starting login to %s", BCA_LOGIN)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    })

    # ── Step 1: Load login page to get CSRF token ──
    try:
        login_page = session.get(BCA_LOGIN, timeout=30)
        login_page.raise_for_status()
    except Exception as e:
        return _failed(f"Could not load BCA login page: {e}")

    soup = BeautifulSoup(login_page.text, "html.parser")

    # Extract all hidden form fields (CSRF token etc.)
    form = soup.find("form")
    hidden_fields = {}
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            val  = inp.get("value", "")
            if name:
                hidden_fields[name] = val

    logger.info("BCA: found %d hidden form fields", len(hidden_fields))

    # ── Step 2: Submit login credentials ──
    login_payload = {
        "name":  BCA_USER,   # BCA uses 'name' for username field
        "pass":  BCA_PASS,   # BCA uses 'pass' for password field
        "op":    "Log in",
        **hidden_fields
    }

    try:
        login_r = session.post(
            BCA_LOGIN,
            data=login_payload,
            timeout=30,
            allow_redirects=True
        )
        login_r.raise_for_status()
    except Exception as e:
        return _failed(f"BCA login POST failed: {e}")

    # ── Step 3: Verify we are logged in ──
    # BCA redirects to home on success; login page stays if credentials wrong
    if "/user/login" in login_r.url or "incorrect" in login_r.text.lower():
        return _failed(
            "BCA login failed — credentials were rejected. "
            "Check BCA_USER and BCA_PASS in GitHub Secrets. "
            f"Final URL was: {login_r.url}"
        )

    logger.info("BCA: login succeeded. Fetching GIS asset allocation page...")

    # ── Step 4: Fetch the asset allocation topic page ──
    # This page lists recent GIS reports with signal summaries
    try:
        alloc_r = session.get(BCA_ALLOC, timeout=30)
        alloc_r.raise_for_status()
    except Exception as e:
        return _failed(f"BCA: logged in but could not load allocation page: {e}")

    # ── Step 5: Also try the GIS home for the FullView Portfolio ──
    try:
        gis_r = session.get(BCA_GIS_HOME, timeout=30)
        gis_r.raise_for_status()
        gis_html = gis_r.text
    except Exception as e:
        logger.warning("BCA: could not load GIS home, using allocation page only: %s", e)
        gis_html = ""

    # ── Step 6: Parse signals from whichever page has the most content ──
    combined_html = alloc_r.text + gis_html
    signals, broad, retrieved_from = _parse_signals(combined_html, session)

    if not signals:
        return _failed(
            "BCA: logged in and loaded pages successfully, but could not parse "
            "signal table from the HTML. The page layout may have changed. "
            "Manual update required — log in at bcaresearch.com and read the "
            "Global Investment Strategy FullView Portfolio."
        )

    logger.info("BCA: successfully extracted %d signals", len(signals))
    return {
        "status":    "live",
        "message":   f"Successfully retrieved from BCA GIS. Read from: {retrieved_from}",
        "retrieved": retrieved_from,
        "broad":     broad,
        "signals":   signals
    }


def _parse_signals(html, session=None):
    """
    Parse asset allocation signals from BCA's HTML.
    Tries multiple strategies in order of reliability.
    Returns (signals_dict, broad_dict, source_description)
    """
    soup = BeautifulSoup(html, "html.parser")
    signals = {}
    broad = {
        "equity": 55, "debt": 34, "cash": 11,
        "note": "BCA GIS FullView Portfolio — retrieved from bcaresearch.com"
    }

    # ── Strategy A: Find a table with allocation/signal columns ──
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) >= 2:
                label  = cells[0].lower()
                stance = cells[1].lower() if len(cells) > 1 else ""
                asset_id = _map_asset(label)
                signal   = _map_signal(stance)
                if asset_id and signal:
                    signals[asset_id] = signal

    if signals:
        return signals, broad, "BCA HTML table (bcaresearch.com)"

    # ── Strategy B: Scan text for "asset: signal" patterns ──
    full_text = soup.get_text(separator="\n")
    lines = full_text.split("\n")
    for line in lines:
        line_lower = line.lower().strip()
        # Look for MacroQuant summary lines like:
        # "MacroQuant recommends a slight underweight position in equities"
        for asset_phrase, asset_id in BCA_ASSET_MAP.items():
            if asset_phrase in line_lower:
                for signal_phrase, signal_code in BCA_SIGNAL_MAP.items():
                    if signal_phrase in line_lower:
                        signals[asset_id] = signal_code
                        break

        # Parse broad allocation percentages
        eq_m = re.search(r"equit\w*[:\s]+(\d+)\s*%", line, re.I)
        fi_m = re.search(r"(fixed income|bond|debt)[:\s]+(\d+)\s*%", line, re.I)
        ca_m = re.search(r"cash[:\s]+(\d+)\s*%", line, re.I)
        if eq_m: broad["equity"] = int(eq_m.group(1))
        if fi_m: broad["debt"]   = int(fi_m.group(2))
        if ca_m: broad["cash"]   = int(ca_m.group(1))

    source = "BCA text extraction (bcaresearch.com)" if signals else "parse failed"
    return signals, broad, source


def _map_asset(label):
    """Map a free-text asset label to a canonical asset ID."""
    label = label.lower().strip()
    for phrase, asset_id in BCA_ASSET_MAP.items():
        if phrase in label:
            return asset_id
    return None


def _map_signal(stance):
    """Map a free-text signal description to a signal code."""
    stance = stance.lower().strip()
    for phrase, code in BCA_SIGNAL_MAP.items():
        if phrase in stance:
            return code
    return None


def _failed(message):
    """Return a standardised failure result."""
    logger.error("BCA: %s", message)
    return {
        "status":    "failed",
        "message":   message,
        "retrieved": "none",
        "broad":     {"equity": 55, "debt": 34, "cash": 11, "note": "Fallback — BCA retrieval failed"},
        "signals":   {}
    }
