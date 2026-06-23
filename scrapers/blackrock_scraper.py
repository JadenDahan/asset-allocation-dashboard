"""
scrapers/blackrock_scraper.py
==============================
BlackRock BII — Weekly Commentary scraper.

BlackRock's weekly commentary PDF is publicly available — no login
required. This scraper finds the latest PDF link on the BII page,
downloads it, extracts the granular views table, and returns signals.

Returns a dict that update_data.py writes into allocations.json.
"""

import re
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BII_WEEKLY_PAGE = (
    "https://www.blackrock.com/us/individual/insights/"
    "blackrock-investment-institute/weekly-commentary"
)
BII_BASE = "https://www.blackrock.com"

# ── Asset label → canonical ID ──
BL_ASSET_MAP = {
    "united states":             "us-eq",
    "u.s. equity":               "us-eq",
    "u.s. equities":             "us-eq",
    "europe":                    "dev-intl",
    "uk":                        "dev-intl",
    "united kingdom":            "dev-intl",
    "japan":                     "dev-intl",
    "emerging markets":          "em-eq",
    "china":                     "em-eq",
    "short u.s. treasuries":     "govt-us",
    "long u.s. treasuries":      "govt-us",
    "u.s. treasuries":           "govt-us",
    "global inflation":          "intl-debt",
    "euro area govt":            "intl-debt",
    "uk gilts":                  "intl-debt",
    "japanese govt":             "intl-debt",
    "international":             "intl-debt",
    "u.s. agency mbs":           "ig-credit",
    "short-term ig":             "ig-credit",
    "long-term ig":              "ig-credit",
    "investment grade":          "ig-credit",
    "global high yield":         "hy-bonds",
    "high yield":                "hy-bonds",
    "asia credit":               "em-bonds",
    "emerging hard currency":    "em-bonds",
    "emerging local currency":   "em-bonds",
}

BL_SIGNAL_MAP = {
    "overweight":    "OW",
    "underweight":   "UW",
    "neutral":       "N",
}

# BII uses a bar/arrow system in PDFs — these phrases appear near signals
BL_CONVICTION_MAP = {
    "high conviction overweight":  "HOW",
    "high conviction underweight": "HUW",
}


def fetch_blackrock():
    """
    Main entry point. Returns:
    {
      "status":    "live" | "failed",
      "message":   str,
      "retrieved": str,
      "broad":     { "equity": int, "debt": int, "cash": int, "note": str },
      "signals":   { "us-eq": "OW", ... }
    }
    """
    logger.info("BlackRock: fetching BII weekly page to find latest PDF")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    # ── Step 1: Load the BII weekly commentary page ──
    try:
        page_r = session.get(BII_WEEKLY_PAGE, timeout=30)
        page_r.raise_for_status()
    except Exception as e:
        return _failed(f"Could not load BII weekly page: {e}")

    # ── Step 2: Find the PDF link ──
    pdf_url = _find_pdf_link(page_r.text)

    if not pdf_url:
        # Try a known URL pattern with today's date
        from datetime import date
        d = date.today()
        # BII typically publishes on Mondays — try recent dates
        for days_back in range(0, 10):
            from datetime import timedelta
            candidate_date = d - timedelta(days=days_back)
            date_str = candidate_date.strftime("%Y%m%d")
            candidate_url = (
                f"https://www.blackrock.com/us/individual/literature/market-commentary/"
                f"weekly-investment-commentary-en-us-{date_str}-strong-earnings-key-as-rates-stay-high.pdf"
            )
            try:
                test = session.head(candidate_url, timeout=10)
                if test.status_code == 200:
                    pdf_url = candidate_url
                    logger.info("BlackRock: found PDF via date pattern: %s", pdf_url)
                    break
            except Exception:
                continue

    if not pdf_url:
        return _failed(
            "BlackRock: could not find the weekly commentary PDF link on the BII page. "
            "The page structure may have changed. Signals from last update are preserved."
        )

    # ── Step 3: Download the PDF ──
    logger.info("BlackRock: downloading PDF: %s", pdf_url)
    try:
        pdf_r = session.get(pdf_url, timeout=60)
        pdf_r.raise_for_status()
        pdf_bytes = pdf_r.content
    except Exception as e:
        return _failed(f"BlackRock: PDF download failed: {e}")

    # ── Step 4: Extract text from PDF ──
    try:
        text = _extract_pdf_text(pdf_bytes)
    except Exception as e:
        return _failed(f"BlackRock: PDF text extraction failed: {e}")

    # ── Step 5: Parse signals from extracted text ──
    signals, broad = _parse_bii_text(text)

    if not signals:
        return _failed(
            "BlackRock: PDF downloaded and text extracted, but could not parse "
            "the granular views table. PDF format may have changed."
        )

    logger.info("BlackRock: extracted %d signals from PDF", len(signals))
    return {
        "status":    "live",
        "message":   f"Successfully retrieved from public BII PDF. URL: {pdf_url}",
        "retrieved": pdf_url,
        "broad":     broad,
        "signals":   signals
    }


def _find_pdf_link(html):
    """Find the most recent BII weekly commentary PDF link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "weekly-investment-commentary" in href and href.endswith(".pdf"):
            return href if href.startswith("http") else BII_BASE + href
    # Also check for any PDF links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "market-commentary" in href and ".pdf" in href:
            return href if href.startswith("http") else BII_BASE + href
    return None


def _extract_pdf_text(pdf_bytes):
    """Extract text from PDF bytes using PyMuPDF (fitz) or pdfminer."""
    try:
        import fitz  # PyMuPDF
        doc  = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        logger.info("BlackRock: extracted PDF text via PyMuPDF (%d chars)", len(text))
        return text
    except ImportError:
        pass

    try:
        import io
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        out = io.StringIO()
        extract_text_to_fp(io.BytesIO(pdf_bytes), out, laparams=LAParams())
        text = out.getvalue()
        logger.info("BlackRock: extracted PDF text via pdfminer (%d chars)", len(text))
        return text
    except ImportError:
        raise RuntimeError(
            "Neither PyMuPDF nor pdfminer.six is installed. "
            "Add 'pymupdf' to requirements.txt."
        )


def _parse_bii_text(text):
    """
    Parse BII granular views table from extracted PDF text.
    BII's table format (from June 15 2026 PDF):

      Asset View Commentary
      Equities
      United States  [OW arrow]  We are overweight...
      Europe         [N arrow]   We are neutral...
      ...
    """
    signals = {}
    broad = {
        "equity": 64, "debt": 28, "cash": 8,
        "note": "BlackRock BII — retrieved from public weekly commentary PDF"
    }

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # BII PDF has asset name on one line, then OW/N/UW on next or same line
    for i, line in enumerate(lines):
        line_lower = line.lower()

        # Match asset class
        asset_id = None
        for phrase, aid in BL_ASSET_MAP.items():
            if phrase in line_lower:
                asset_id = aid
                break

        if not asset_id:
            continue

        # Look for signal in this line and the next 3 lines
        context = " ".join(lines[i:i+4]).lower()

        signal = None
        # Check high conviction first
        for phrase, code in BL_CONVICTION_MAP.items():
            if phrase in context:
                signal = code
                break

        if not signal:
            # BII text: "We are overweight" / "We are neutral" / "We are underweight"
            if "we are overweight" in context or "▲ overweight" in context:
                signal = "OW"
            elif "we are underweight" in context or "▼ underweight" in context:
                signal = "UW"
            elif "we are neutral" in context or "— neutral" in context:
                signal = "N"

        if not signal:
            for phrase, code in BL_SIGNAL_MAP.items():
                if phrase in context:
                    signal = code
                    break

        if signal and asset_id:
            # Don't overwrite a more specific signal
            if asset_id not in signals:
                signals[asset_id] = signal

    return signals, broad


def _failed(message):
    logger.error("BlackRock: %s", message)
    return {
        "status":    "failed",
        "message":   message,
        "retrieved": "none",
        "broad":     {"equity": 64, "debt": 28, "cash": 8, "note": "Fallback — BlackRock retrieval failed"},
        "signals":   {}
    }
