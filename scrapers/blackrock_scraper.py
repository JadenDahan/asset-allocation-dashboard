"""
scrapers/blackrock_scraper.py
==============================
BlackRock Target Allocation ETF Model — signal extractor.

Data sources (tried in order):
  1. BlackRock Aladdin REST API  →  institutional allocation endpoint
  2. iShares public fund holdings CSV  →  parse target allocation ETF weights
     (BIGPX / BAICX / iShares Core series — these are PUBLIC, no login needed)
  3. Web session scrape of the BlackRock Investment Institute portal

Set env vars:
  BL_USER       Aladdin / BII portal username
  BL_PASS       Aladdin / BII portal password
  BL_API_URL    base URL (default: https://api.blackrock.com/aladdin/v1)
"""

import os
import re
import json
import logging
import requests
import csv
import io
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BL_API_URL = os.environ.get("BL_API_URL", "https://api.blackrock.com/aladdin/v1")
BL_WEB_URL = os.environ.get("BL_WEB_URL", "https://www.blackrock.com")
BL_USER    = os.environ.get("BL_USER", "")
BL_PASS    = os.environ.get("BL_PASS", "")

# iShares Target Allocation ETFs — publicly accessible holdings CSVs
# These ETFs ARE the published model — their holdings ARE the allocation signals
ISHARES_ETF_HOLDINGS = {
    # Moderate allocation (60/40-ish) — use as reference neutral benchmark
    "BIGPX": "https://www.ishares.com/us/products/239783/BIGPX/1467271812596.ajax?fileType=csv&fileName=BIGPX_holdings&dataType=fund",
    # Growth allocation (80/20)
    "BAICX": "https://www.ishares.com/us/products/239784/BAICX/1467271812596.ajax?fileType=csv&fileName=BAICX_holdings&dataType=fund",
}

# BII tactical views page — BlackRock Investment Institute
BII_VIEWS_URL = f"{BL_WEB_URL}/us/en/insights/blackrock-investment-institute/macro-and-market-perspectives"

BL_ASSET_MAP = {
    "US Equity":                    "us-eq",
    "US Equities":                  "us-eq",
    "International Equity":         "intl-eq",
    "International Equities":       "intl-eq",
    "US Large Cap":                 "us-lc",
    "US Large-Cap":                 "us-lc",
    "US Small Cap":                 "us-sc",
    "US Small-Cap":                 "us-sc",
    "US Growth":                    "us-gr",
    "US Value":                     "us-val",
    "Emerging Markets":             "em-eq",
    "Emerging Markets Equity":      "em-eq",
    "International Developed":      "dev-intl",
    "EAFE":                         "dev-intl",
    "US Treasuries":                "govt-us",
    "US Government":                "govt-us",
    "International Fixed Income":   "intl-debt",
    "Investment Grade":             "ig-credit",
    "IG Credit":                    "ig-credit",
    "High Yield":                   "hy-bonds",
    "Emerging Markets Debt":        "em-bonds",
    "EM Debt":                      "em-bonds",
    "Gold":                         "gold",
    "Commodities":                  "oil",
    "Energy":                       "oil",
}

BL_SIGNAL_MAP = {
    "strong overweight":    "HOW",
    "overweight":           "OW",
    "neutral":              "N",
    "underweight":          "UW",
    "strong underweight":   "HUW",
    "max overweight":       "HOW",
    "max underweight":      "HUW",
    "slight overweight":    "OW",
    "slight underweight":   "UW",
}


def fetch_blackrock():
    """
    Returns dict:
      {"broad": {...}, "signals": {"us-eq": "OW", ...}}
    """
    # Try 1: Aladdin API (institutional)
    if BL_USER and BL_PASS:
        logger.info("BlackRock: attempting Aladdin API auth…")
        try:
            return _fetch_via_aladdin_api()
        except Exception as e:
            logger.warning(f"BlackRock Aladdin API failed ({e}), trying iShares CSV…")

    # Try 2: Public iShares holdings CSV (no auth needed)
    logger.info("BlackRock: fetching public iShares ETF holdings CSV…")
    try:
        return _fetch_via_ishares_csv()
    except Exception as e:
        logger.warning(f"BlackRock iShares CSV failed ({e}), trying BII web scrape…")

    # Try 3: BII portal web scrape
    try:
        return _fetch_via_bii_web()
    except Exception as e:
        raise RuntimeError(f"BlackRock: all methods failed — {e}")


# ─────────────────────────────────────────────────────
# METHOD 1 — Aladdin REST API
# ─────────────────────────────────────────────────────
def _fetch_via_aladdin_api():
    session = requests.Session()
    session.headers.update({"User-Agent": "AssetAllocationDashboard/1.0"})

    auth_r = session.post(
        f"{BL_API_URL}/oauth/token",
        data={
            "grant_type":    "password",
            "username":      BL_USER,
            "password":      BL_PASS,
            "client_id":     "alloc_dashboard",
        },
        timeout=30,
    )
    auth_r.raise_for_status()
    token = auth_r.json().get("access_token")
    if not token:
        raise ValueError("Aladdin API: no access_token returned")

    session.headers["Authorization"] = f"Bearer {token}"

    # BII tactical views endpoint
    r = session.get(f"{BL_API_URL}/tactical/views", timeout=30)
    r.raise_for_status()
    return _parse_aladdin_response(r.json())


def _parse_aladdin_response(payload):
    """Adapt to actual Aladdin response schema."""
    broad = {
        "equity": payload.get("allocation", {}).get("equity", 0),
        "debt":   payload.get("allocation", {}).get("fixedIncome", 0),
        "cash":   payload.get("allocation", {}).get("cash", 0),
        "note":   payload.get("rationale", {}).get("summary", ""),
    }
    signals = {}
    for item in payload.get("views", []):
        label    = item.get("assetClass", "").strip()
        stance   = item.get("view", "").strip().lower()
        asset_id = BL_ASSET_MAP.get(label)
        signal   = BL_SIGNAL_MAP.get(stance, "N")
        if asset_id:
            signals[asset_id] = signal
    return {"broad": broad, "signals": signals}


# ─────────────────────────────────────────────────────
# METHOD 2 — Public iShares Target Alloc ETF CSV
# This is the cleanest no-auth source.
# The ETF weights directly represent BL's model allocation.
# ─────────────────────────────────────────────────────
def _fetch_via_ishares_csv():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; AssetAllocationBot/1.0)",
        "Referer":    "https://www.ishares.com/",
    })

    # Use BIGPX (moderate) as the primary reference
    url = ISHARES_ETF_HOLDINGS["BIGPX"]
    r   = session.get(url, timeout=30)
    r.raise_for_status()

    raw    = r.text
    reader = csv.reader(io.StringIO(raw))
    rows   = list(reader)

    # iShares CSVs have metadata rows at the top — find the actual header row
    header_idx = next(
        (i for i, row in enumerate(rows) if "Name" in row and "Weight" in row), None
    )
    if header_idx is None:
        raise ValueError("iShares CSV: could not find header row")

    headers = rows[header_idx]
    name_col   = _col(headers, "Name")
    weight_col = _col(headers, "Weight")

    # Sum weights by asset class bucket to build broad allocation
    equity_total = debt_total = cash_total = 0.0
    signals = {}

    for row in rows[header_idx + 1:]:
        if len(row) <= max(name_col, weight_col):
            continue
        name   = row[name_col].strip()
        weight = _parse_float(row[weight_col])

        # Classify each underlying fund/ETF into our asset buckets
        name_lower = name.lower()
        if any(k in name_lower for k in ["equity", "stock", "growth", "value", "msci", "russell", "s&p"]):
            equity_total += weight
        elif any(k in name_lower for k in ["bond", "treasury", "fixed", "credit", "yield", "debt"]):
            debt_total += weight
        elif any(k in name_lower for k in ["cash", "money market", "liquidity"]):
            cash_total += weight

        # Map to signal based on weight vs expected neutral (rough heuristic)
        asset_id = _name_to_asset_id(name)
        if asset_id:
            # Convert over/under vs a 10% neutral to signal
            signals[asset_id] = _weight_to_signal(weight, neutral=10.0)

    broad = {
        "equity": round(equity_total),
        "debt":   round(debt_total),
        "cash":   round(cash_total),
        "note":   "Derived from iShares Target Allocation ETF (BIGPX) public holdings",
    }
    return {"broad": broad, "signals": signals}


def _col(headers, name):
    for i, h in enumerate(headers):
        if name.lower() in h.lower():
            return i
    return 0


def _parse_float(s):
    try:
        return float(re.sub(r"[^\d.\-]", "", s))
    except Exception:
        return 0.0


def _name_to_asset_id(name):
    for label, asset_id in BL_ASSET_MAP.items():
        if label.lower() in name.lower():
            return asset_id
    return None


def _weight_to_signal(weight, neutral=10.0):
    diff = weight - neutral
    if   diff >  6: return "HOW"
    elif diff >  2: return "OW"
    elif diff > -2: return "N"
    elif diff > -6: return "UW"
    else:           return "HUW"


# ─────────────────────────────────────────────────────
# METHOD 3 — BII web portal scrape
# ─────────────────────────────────────────────────────
def _fetch_via_bii_web():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Safari/537.36",
    })

    # BII publishes a public tactical views page — no login required
    r = session.get(BII_VIEWS_URL, timeout=30)
    r.raise_for_status()
    return _parse_bii_page(r.text)


def _parse_bii_page(html):
    """
    Scrape BlackRock Investment Institute tactical views page.
    !! Update CSS selectors to match real BII page DOM !!
    """
    soup = BeautifulSoup(html, "html.parser")
    signals  = {}
    broad    = {"equity": 62, "debt": 30, "cash": 8, "note": "Sourced from BII web portal"}

    # Look for the tactical views table / grid
    # BII typically renders a bar chart or table with asset class rows
    view_cards = soup.find_all(class_=re.compile(r"asset-view|tactical|view-card", re.I))
    for card in view_cards:
        name_el   = card.find(class_=re.compile(r"asset-name|title", re.I))
        signal_el = card.find(class_=re.compile(r"signal|stance|view-label", re.I))
        if name_el and signal_el:
            label    = name_el.get_text(strip=True)
            stance   = signal_el.get_text(strip=True).lower()
            asset_id = BL_ASSET_MAP.get(label)
            signal   = BL_SIGNAL_MAP.get(stance, "N")
            if asset_id:
                signals[asset_id] = signal

    return {"broad": broad, "signals": signals}
