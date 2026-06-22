"""
scrapers/bca_scraper.py
========================
BCA Research — Global Asset Allocation service signal extractor.

Authentication flow (tried in order):
  1. BCA REST API  →  POST /auth  →  GET /reports/asset-allocation/latest
  2. Web session login + PDF download + text extraction (PyMuPDF / pdfminer)
  3. Web session login + HTML report page scrape

Set env vars:
  BCA_USER     your BCA subscriber username / ID
  BCA_PASS     your BCA password or API key
  BCA_API_URL  base URL (default: https://api.bcaresearch.com/v1)
"""

import os
import re
import io
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BCA_API_URL = os.environ.get("BCA_API_URL", "https://api.bcaresearch.com/v1")
BCA_WEB_URL = os.environ.get("BCA_WEB_URL", "https://portal.bcaresearch.com")
BCA_USER    = os.environ.get("BCA_USER", "")
BCA_PASS    = os.environ.get("BCA_PASS", "")

BCA_ASSET_MAP = {
    "U.S. Equities":                "us-eq",
    "US Equities":                  "us-eq",
    "International Equities":       "intl-eq",
    "U.S. Large Cap":               "us-lc",
    "U.S. Small Cap":               "us-sc",
    "U.S. Growth":                  "us-gr",
    "U.S. Value":                   "us-val",
    "Emerging Markets Equity":      "em-eq",
    "EM Equities":                  "em-eq",
    "EAFE":                         "dev-intl",
    "Developed Markets ex-US":      "dev-intl",
    "U.S. Government Bonds":        "govt-us",
    "US Treasuries":                "govt-us",
    "International Bonds":          "intl-debt",
    "Investment Grade Credit":      "ig-credit",
    "IG Credit":                    "ig-credit",
    "High Yield":                   "hy-bonds",
    "Emerging Markets Debt":        "em-bonds",
    "Gold":                         "gold",
    "Commodities / Oil":            "oil",
    "Energy":                       "oil",
}

BCA_SIGNAL_MAP = {
    "maximum overweight":   "HOW",
    "heavy overweight":     "HOW",
    "overweight":           "OW",
    "slight overweight":    "OW",
    "neutral":              "N",
    "slight underweight":   "UW",
    "underweight":          "UW",
    "heavy underweight":    "HUW",
    "maximum underweight":  "HUW",
    # BCA sometimes uses +2/+1/0/-1/-2 scoring
    "+2": "HOW", "+1": "OW", "0": "N", "-1": "UW", "-2": "HUW",
}


def fetch_bca():
    """
    Returns {"broad": {...}, "signals": {"us-eq": "OW", ...}}
    """
    logger.info("BCA: attempting REST API auth…")
    try:
        return _fetch_via_api()
    except Exception as e:
        logger.warning(f"BCA REST API failed ({e}), trying web portal…")

    try:
        return _fetch_via_web()
    except Exception as e:
        raise RuntimeError(f"BCA: all methods failed — {e}")


# ─────────────────────────────────────────────────────
# METHOD 1 — REST API
# ─────────────────────────────────────────────────────
def _fetch_via_api():
    session = requests.Session()
    session.headers.update({"User-Agent": "AssetAllocationDashboard/1.0"})

    # Authenticate
    auth_r = session.post(
        f"{BCA_API_URL}/auth",
        json={"subscriber_id": BCA_USER, "api_key": BCA_PASS},
        timeout=30,
    )
    auth_r.raise_for_status()
    token = (
        auth_r.json().get("access_token")
        or auth_r.json().get("token")
        or auth_r.json().get("api_token")
    )
    if not token:
        raise ValueError("BCA API: no token in auth response")

    session.headers["Authorization"] = f"Bearer {token}"
    session.headers["X-BCA-Token"]   = token  # BCA may use custom header

    # Fetch latest GAA report
    r = session.get(f"{BCA_API_URL}/reports/asset-allocation/latest", timeout=30)
    r.raise_for_status()
    return _parse_api_response(r.json())


def _parse_api_response(payload):
    """Parse BCA API JSON — adapt field names to real response."""
    broad = {
        "equity": payload.get("broad_allocation", {}).get("equity", 0),
        "debt":   payload.get("broad_allocation", {}).get("fixed_income", 0),
        "cash":   payload.get("broad_allocation", {}).get("cash", 0),
        "note":   payload.get("summary", ""),
    }
    signals = {}
    for item in payload.get("tactical_views", []):
        label    = item.get("asset_class", "").strip()
        # BCA may return numeric score or label
        raw      = str(item.get("view", item.get("score", "0"))).strip().lower()
        asset_id = BCA_ASSET_MAP.get(label)
        signal   = BCA_SIGNAL_MAP.get(raw, "N")
        if asset_id:
            signals[asset_id] = signal
    return {"broad": broad, "signals": signals}


# ─────────────────────────────────────────────────────
# METHOD 2 — Web portal session + page scrape
# ─────────────────────────────────────────────────────
def _fetch_via_web():
    """
    Logs in to BCA Research subscriber portal,
    navigates to the Global Asset Allocation report page,
    and parses the signal table.
    """
    LOGIN_URL  = f"{BCA_WEB_URL}/login"
    REPORT_URL = f"{BCA_WEB_URL}/research/global-asset-allocation"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Safari/537.36",
    })

    # 1. Load login page → capture CSRF / hidden fields
    login_page = session.get(LOGIN_URL, timeout=30)
    login_page.raise_for_status()
    soup  = BeautifulSoup(login_page.text, "html.parser")
    form  = soup.find("form")
    csrf  = ""
    extra = {}
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            extra[inp.get("name", "")] = inp.get("value", "")
        csrf_inp = form.find("input", {"name": re.compile(r"csrf|token", re.I)})
        if csrf_inp:
            csrf = csrf_inp.get("value", "")

    # 2. POST credentials
    post_data = {
        "email":    BCA_USER,   # update to BCA's actual field name
        "password": BCA_PASS,
        **extra,
    }
    login_r = session.post(
        LOGIN_URL,
        data=post_data,
        timeout=30,
        allow_redirects=True,
    )

    # Sanity-check we're logged in
    if "login" in login_r.url or "sign-in" in login_r.url:
        raise ValueError("BCA web login failed — check credentials or form field names")
    logger.info("BCA: web login succeeded")

    # 3. Fetch the GAA report page
    report_r = session.get(REPORT_URL, timeout=30)
    report_r.raise_for_status()

    # Try PDF download first if available
    pdf_link = _find_pdf_link(report_r.text, BCA_WEB_URL)
    if pdf_link:
        logger.info(f"BCA: downloading PDF from {pdf_link}…")
        try:
            return _parse_pdf(session.get(pdf_link, timeout=60).content)
        except Exception as e:
            logger.warning(f"BCA: PDF parse failed ({e}), falling back to HTML parse")

    return _parse_report_html(report_r.text)


def _find_pdf_link(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.endswith(".pdf") or "pdf" in href.lower():
            return href if href.startswith("http") else base_url + href
    return None


def _parse_pdf(pdf_bytes):
    """
    Extract text from BCA PDF report and parse signals.
    Requires:  pip install pymupdf
    Falls back to pdfminer if pymupdf not available.
    """
    text = ""
    try:
        import fitz  # PyMuPDF
        doc  = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
    except ImportError:
        try:
            from pdfminer.high_level import extract_text_to_fp
            from pdfminer.layout import LAParams
            out = io.StringIO()
            extract_text_to_fp(io.BytesIO(pdf_bytes), out, laparams=LAParams())
            text = out.getvalue()
        except ImportError:
            raise RuntimeError("Install pymupdf or pdfminer.six for PDF parsing: pip install pymupdf")

    return _parse_signal_text(text)


def _parse_signal_text(text):
    """
    Parse signal text extracted from PDF or HTML.
    Looks for patterns like:  "U.S. Equities  Overweight"
    or score tables:          "U.S. Equities  +1"
    """
    signals  = {}
    broad    = {"equity": 50, "debt": 36, "cash": 14, "note": "Sourced from BCA Research web portal"}

    lines = text.split("\n")
    for line in lines:
        line = line.strip()
        for label, asset_id in BCA_ASSET_MAP.items():
            if label.lower() in line.lower():
                # Look for signal keyword in the same line
                line_lower = line.lower()
                for signal_text, signal_code in BCA_SIGNAL_MAP.items():
                    if signal_text in line_lower:
                        signals[asset_id] = signal_code
                        break

        # Parse broad allocation line e.g. "Equities: 50%"
        eq_match = re.search(r"equit\w+[:\s]+(\d+)\s*%", line, re.I)
        fi_match = re.search(r"(fixed income|bonds?|debt)[:\s]+(\d+)\s*%", line, re.I)
        ca_match = re.search(r"cash[:\s]+(\d+)\s*%", line, re.I)
        if eq_match: broad["equity"] = int(eq_match.group(1))
        if fi_match: broad["debt"]   = int(fi_match.group(2))
        if ca_match: broad["cash"]   = int(ca_match.group(1))

    return {"broad": broad, "signals": signals}


def _parse_report_html(html):
    """
    Fallback HTML parser for the BCA GAA report page.
    !! Update CSS selectors to match real BCA portal DOM !!
    """
    soup    = BeautifulSoup(html, "html.parser")
    signals = {}
    broad   = {"equity": 50, "debt": 36, "cash": 14, "note": "Sourced from BCA Research web portal"}

    # Try to find the signal table
    tables = soup.find_all("table")
    for table in tables:
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
            if len(cells) >= 2:
                label    = cells[0]
                stance   = cells[1].lower() if len(cells) > 1 else ""
                asset_id = BCA_ASSET_MAP.get(label)
                signal   = BCA_SIGNAL_MAP.get(stance, "N")
                if asset_id:
                    signals[asset_id] = signal

    return {"broad": broad, "signals": signals}
