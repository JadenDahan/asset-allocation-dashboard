"""
scrapers/ndr_scraper.py
=======================
Ned Davis Research — asset allocation signal extractor.

Authentication flow (tries in order):
  1. REST API  →  POST /auth/token  →  GET /allocations/tactical
  2. Web session login (requests-html or requests + BeautifulSoup)
     if the REST API returns 404 / is not provisioned for your subscription tier.

Set env vars:
  NDR_USER     your NDR username / client ID
  NDR_PASS     your NDR password or API key
  NDR_API_URL  base URL (default: https://api.ndr.com/v2)
"""

import os
import re
import json
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NDR_API_URL  = os.environ.get("NDR_API_URL",  "https://api.ndr.com/v2")
NDR_WEB_URL  = os.environ.get("NDR_WEB_URL",  "https://www.ndr.com")
NDR_USER     = os.environ.get("NDR_USER", "")
NDR_PASS     = os.environ.get("NDR_PASS", "")

# Map NDR's internal asset class labels to our canonical IDs
# !! Update these keys to match what the NDR API/site actually returns !!
NDR_ASSET_MAP = {
    "US Equities":                     "us-eq",
    "International Equities":          "intl-eq",
    "US Large Cap":                    "us-lc",
    "US Small Cap":                    "us-sc",
    "US Growth":                       "us-gr",
    "US Value":                        "us-val",
    "Emerging Markets Equities":       "em-eq",
    "Developed International":         "dev-intl",
    "US Government Bonds":             "govt-us",
    "International Debt":              "intl-debt",
    "Investment Grade Credit":         "ig-credit",
    "High Yield":                      "hy-bonds",
    "Emerging Markets Debt":           "em-bonds",
    "Gold":                            "gold",
    "Oil / Energy":                    "oil",
}

# Map NDR's stance labels → our signal codes
NDR_SIGNAL_MAP = {
    "strong overweight":  "HOW",
    "overweight":         "OW",
    "neutral":            "N",
    "underweight":        "UW",
    "strong underweight": "HUW",
    # add synonyms as you discover them in actual API responses
    "above weight":       "OW",
    "below weight":       "UW",
    "maximum underweight":"HUW",
    "maximum overweight": "HOW",
}


def fetch_ndr():
    """
    Returns dict:
      {
        "broad":   {"equity": int, "debt": int, "cash": int, "note": str},
        "signals": {"us-eq": "OW", ...},   # keyed by canonical asset ID
      }
    Raises RuntimeError if both auth methods fail.
    """
    logger.info("NDR: attempting REST API auth…")
    try:
        return _fetch_via_api()
    except Exception as e:
        logger.warning(f"NDR REST API failed ({e}), falling back to web scrape…")

    try:
        return _fetch_via_web()
    except Exception as e:
        raise RuntimeError(f"NDR: both API and web auth failed — {e}")


# ─────────────────────────────────────────────────────
# METHOD 1 — REST API
# ─────────────────────────────────────────────────────
def _fetch_via_api():
    session = requests.Session()
    session.headers.update({"User-Agent": "AssetAllocationDashboard/1.0"})

    # --- Authenticate ---
    auth_r = session.post(
        f"{NDR_API_URL}/auth/token",
        json={"username": NDR_USER, "password": NDR_PASS},
        timeout=30,
    )
    auth_r.raise_for_status()
    token = auth_r.json().get("access_token") or auth_r.json().get("token")
    if not token:
        raise ValueError("NDR API: no token in auth response")

    session.headers["Authorization"] = f"Bearer {token}"

    # --- Pull tactical allocation ---
    alloc_r = session.get(f"{NDR_API_URL}/allocations/tactical", timeout=30)
    alloc_r.raise_for_status()
    payload = alloc_r.json()

    return _parse_api_response(payload)


def _parse_api_response(payload):
    """
    Parse NDR REST response into our standard format.
    Adapt field names to match the real API response structure.
    """
    broad = {
        # Field names are illustrative — map to real response keys
        "equity": payload.get("weights", {}).get("equity", 0),
        "debt":   payload.get("weights", {}).get("fixed_income", 0),
        "cash":   payload.get("weights", {}).get("cash", 0),
        "note":   payload.get("commentary", {}).get("summary", ""),
    }

    signals = {}
    for item in payload.get("asset_classes", []):
        label  = item.get("name", "").strip()
        stance = item.get("tactical_view", "").strip().lower()
        asset_id = NDR_ASSET_MAP.get(label)
        signal   = NDR_SIGNAL_MAP.get(stance, "N")
        if asset_id:
            signals[asset_id] = signal

    return {"broad": broad, "signals": signals}


# ─────────────────────────────────────────────────────
# METHOD 2 — Web session scrape (fallback)
# ─────────────────────────────────────────────────────
def _fetch_via_web():
    """
    Logs into the NDR subscriber portal via HTTP session cookies,
    then scrapes the institutional model portfolio page for signals.

    You must inspect the login form on ndr.com to get the correct
    field names and action URL — update the constants below.
    """
    LOGIN_URL      = f"{NDR_WEB_URL}/login"
    PORTFOLIO_URL  = f"{NDR_WEB_URL}/institutional/model-portfolio"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36"
    })

    # Load the login page first (to capture any CSRF token)
    login_page = session.get(LOGIN_URL, timeout=30)
    soup = BeautifulSoup(login_page.text, "html.parser")
    csrf = ""
    csrf_input = soup.find("input", {"name": re.compile(r"csrf|_token", re.I)})
    if csrf_input:
        csrf = csrf_input.get("value", "")

    # POST credentials
    login_r = session.post(
        LOGIN_URL,
        data={
            "username":   NDR_USER,  # update field name to match NDR's form
            "password":   NDR_PASS,
            "_csrf_token": csrf,
        },
        timeout=30,
        allow_redirects=True,
    )
    if "logout" not in login_r.text.lower() and "dashboard" not in login_r.url.lower():
        raise ValueError("NDR web login appeared to fail — check credentials or form field names")

    logger.info("NDR: web login succeeded, fetching portfolio page…")

    # Fetch the model portfolio / allocation page
    page_r = session.get(PORTFOLIO_URL, timeout=30)
    page_r.raise_for_status()

    return _parse_web_page(page_r.text)


def _parse_web_page(html):
    """
    Parse NDR's model portfolio HTML page.
    !! This is a TEMPLATE — you must inspect the real page DOM and
    update the CSS selectors / table column indices below. !!
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Broad weights ---
    # Adjust selector to match the actual page structure
    broad = {"equity": 0, "debt": 0, "cash": 0, "note": "Sourced from NDR web portal"}
    broad_table = soup.find("table", class_=re.compile(r"broad|allocation|weights", re.I))
    if broad_table:
        for row in broad_table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) >= 2:
                label = cells[0].lower()
                val   = _parse_pct(cells[1])
                if "equity"      in label: broad["equity"] = val
                elif "bond" in label or "fixed" in label: broad["debt"] = val
                elif "cash"      in label: broad["cash"]   = val

    # --- Asset class signals ---
    signals = {}
    signal_table = soup.find("table", class_=re.compile(r"tactical|signals|model", re.I))
    if signal_table:
        for row in signal_table.find_all("tr")[1:]:  # skip header
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) >= 2:
                label  = cells[0]
                stance = cells[1].lower()
                asset_id = NDR_ASSET_MAP.get(label)
                signal   = NDR_SIGNAL_MAP.get(stance, "N")
                if asset_id:
                    signals[asset_id] = signal

    return {"broad": broad, "signals": signals}


def _parse_pct(s):
    """Extract numeric percentage from strings like '57%', '57.0', '57'."""
    try:
        return int(float(re.sub(r"[^\d.]", "", s)))
    except Exception:
        return 0
