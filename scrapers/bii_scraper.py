"""
scrapers/bii_scraper.py
========================
Fetches BlackRock BII asset class views from the public outlook page:
https://www.blackrock.com/corporate/insights/blackrock-investment-institute/publications/outlook#asset-class-views

No login required. BII publishes directional signals (Overweight /
Neutral / Underweight) which are mapped to scores directly.
High conviction overweight/underweight mapped to Heavy OW/UW (+2/-2).
"""

import re, logging
log = logging.getLogger(__name__)

BII_URL = ("https://www.blackrock.com/corporate/insights/"
           "blackrock-investment-institute/publications/outlook")

BII_WEEKLY = ("https://www.blackrock.com/corporate/literature/whitepaper/bii-global-outlook-in-charts.pdf")

# Map BII asset labels → canonical IDs
# BII covers broad asset classes, not always sub-asset level
BII_MAP = {
    # Primary
    "equities":               "equities",
    "global equities":        "equities",
    "stocks":                 "equities",
    "bonds":                  "bonds",
    "fixed income":           "bonds",
    "gold":                   "gold",
    # Sub-asset — BII provides these in the granular views table
    "united states":          "us-lc",   # BII calls it "United States" under Equities
    "u.s.":                   "us-lc",
    "us equity":              "us-lc",
    "europe":                 "dev-intl",
    "japan":                  "dev-intl",
    "uk":                     "dev-intl",
    "eafe":                   "dev-intl",
    "developed market":       "dev-intl",
    "emerging market":        "em-eq",
    "em equity":              "em-eq",
    "u.s. treasur":           "govt-us",
    "us treasur":             "govt-us",
    "government bond":        "govt-us",
    "euro area govt":         "intl-debt",
    "uk gilt":                "intl-debt",
    "japanese govt":          "intl-debt",
    "international bond":     "intl-debt",
    "investment grade":       "ig-credit",
    "ig credit":              "ig-credit",
    "u.s. agency mbs":        "ig-credit",  # BII treats MBS as IG-adjacent
    "high yield":             "hy-bonds",
    "global high yield":      "hy-bonds",
    "emerging hard currency": "em-bonds",
    "em debt":                "em-bonds",
}

# BII signal text → score
BII_SIGNAL = {
    "high conviction overweight":  2,
    "high conviction underweight": -2,
    "overweight":                  1,
    "neutral":                     0,
    "underweight":                 -1,
}


def fetch_bii():
    """
    Returns:
    {
      "status": "live" | "failed",
      "message": str,
      "retrieved": str,
      "assets": {
        "equities": {"signal": "Overweight", "score": 1},
        ...
      }
    }
    """
    import requests
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Try the main outlook page first
    assets = {}
    source_url = BII_URL

    try:
        log.info("BII: fetching %s", BII_URL)
        r = requests.get(BII_URL, headers=headers, timeout=30)
        r.raise_for_status()
        assets = _parse_html(r.text)
        source_url = BII_URL
    except Exception as e:
        log.warning("BII: main page failed (%s), trying weekly commentary", e)

    # Try weekly commentary as fallback
    if not assets:
        try:
            log.info("BII: fetching weekly commentary %s", BII_WEEKLY)
            r = requests.get(BII_WEEKLY, headers=headers, timeout=30)
            r.raise_for_status()
            assets = _parse_html(r.text)
            source_url = BII_WEEKLY
        except Exception as e:
            return _fail(f"Both BII pages failed: {e}")

    if not assets:
        return _fail(
            "BII pages loaded but no signal table found. "
            "BlackRock may have updated their page structure. "
            "Manual update required — visit blackrock.com/corporate/insights/"
            "blackrock-investment-institute/publications/outlook"
        )

    log.info("BII: extracted %d signals from %s", len(assets), source_url)
    return {
        "status":    "live",
        "message":   f"Retrieved from public BII page. Source: {source_url}",
        "retrieved": source_url,
        "assets":    assets
    }


def _parse_html(html):
    """
    Parse BII's asset class views table.
    BII format:
      Asset | Signal (Overweight / Neutral / Underweight)
      with optional "High conviction" modifier

    Returns: { asset_id: {"signal": str, "score": int} }
    """
    from bs4 import BeautifulSoup
    soup   = BeautifulSoup(html, "html.parser")
    result = {}

    # Strategy A: Look for structured signal tables
    for table in soup.find_all("table"):
        text = table.get_text(separator=" ").lower()
        if not any(word in text for word in
                   ["overweight", "underweight", "neutral"]):
            continue

        for row in table.find_all("tr"):
            cells = [td.get_text(separator=" ", strip=True)
                     for td in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            label    = cells[0].lower().strip()
            asset_id = _map(label)
            if not asset_id:
                continue

            # Join all cells into one string to find the signal
            context  = " ".join(cells).lower()
            sig, score = _parse_signal(context)
            if sig:
                result[asset_id] = {"signal": sig, "score": score}

    # Strategy B: Scan divs / sections with signal keywords
    if not result:
        for el in soup.find_all(
            ["div", "section", "li", "p"],
            string=re.compile(
                r"(overweight|underweight|neutral)", re.I
            )
        ):
            text     = el.get_text(separator=" ", strip=True)
            label    = text.lower()
            asset_id = _map(label)
            if not asset_id:
                continue
            sig, score = _parse_signal(label)
            if sig and asset_id not in result:
                result[asset_id] = {"signal": sig, "score": score}

    # Strategy C: Full-text scan for "Asset: Signal" patterns
    if not result:
        full = soup.get_text(separator="\n")
        for line in full.split("\n"):
            line_lower = line.lower().strip()
            asset_id   = _map(line_lower)
            if not asset_id:
                continue
            # Look for signal in this line and surrounding lines
            sig, score = _parse_signal(line_lower)
            if sig and asset_id not in result:
                result[asset_id] = {"signal": sig, "score": score}

    return result


def _parse_signal(text):
    """Extract signal label and score from a text string."""
    text = text.lower()
    for phrase, score in sorted(BII_SIGNAL.items(),
                                key=lambda x: len(x[0]), reverse=True):
        if phrase in text:
            # Capitalise nicely
            label = phrase.replace("high conviction ", "High Conviction ").title()
            return label, score
    return None, None


def _map(label):
    label = label.lower().strip()
    for phrase, aid in BII_MAP.items():
        if phrase in label:
            return aid
    return None


def _fail(msg):
    log.error("BII: %s", msg)
    return {"status": "failed", "message": msg, "retrieved": "none", "assets": {}}
