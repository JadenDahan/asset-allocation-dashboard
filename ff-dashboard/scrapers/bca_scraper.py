"""
scrapers/bca_scraper.py
========================
Logs into BCA Research with BCA_USER / BCA_PASS (GitHub Secrets).
Reads the GIS FullView Portfolio page which shows:
  Asset | FullView Portfolio % | Benchmark % | Difference (%pts)

Active weight is directly provided as Difference (%pts).
Signal is derived from that active weight using the standard formula.
"""

import os, re, logging
log = logging.getLogger(__name__)

BCA_USER  = os.environ.get("BCA_USER", "")
BCA_PASS  = os.environ.get("BCA_PASS", "")
BCA_LOGIN = "https://www.bcaresearch.com/user/login"
BCA_GIS   = "https://www.bcaresearch.com/site/gis/home"

# Map BCA's asset labels → canonical IDs
BCA_MAP = {
    # Primary
    "equities":            "equities",
    "global equities":     "equities",
    "equity":              "equities",
    "bonds":               "bonds",
    "fixed income":        "bonds",
    "cash":                "cash",
    "gold":                "gold",
    # Sub-asset
    "u.s. large":          "us-lc",
    "us large":            "us-lc",
    "large cap":           "us-lc",
    "large-cap":           "us-lc",
    "u.s. small":          "us-sc",
    "us small":            "us-sc",
    "small cap":           "us-sc",
    "small-cap":           "us-sc",
    "u.s. growth":         "us-gr",
    "growth":              "us-gr",
    "u.s. value":          "us-val",
    "value":               "us-val",
    "emerging market":     "em-eq",
    "em equit":            "em-eq",
    "eafe":                "dev-intl",
    "europe":              "dev-intl",
    "developed market":    "dev-intl",
    "international equit": "dev-intl",
    "u.s. treasur":        "govt-us",
    "us treasur":          "govt-us",
    "government bond":     "govt-us",
    "international bond":  "intl-debt",
    "intl bond":           "intl-debt",
    "investment grade":    "ig-credit",
    "ig credit":           "ig-credit",
    "high yield":          "hy-bonds",
    "emerging market bond":"em-bonds",
    "em bond":             "em-bonds",
    "oil":                 "oil",
    "energy":              "oil",
}


def fetch_bca():
    """
    Returns:
    {
      "status": "live" | "failed",
      "message": str,
      "retrieved": str,
      "assets": {
        "equities": {"position": 54.9, "benchmark": 60.0, "activeWt": -5.1},
        ...
      }
    }
    """
    if not BCA_USER or not BCA_PASS:
        return _fail("BCA_USER or BCA_PASS not set in GitHub Secrets.")

    try:
        return _via_playwright()
    except ImportError:
        log.warning("BCA: Playwright not installed, trying requests")
    except Exception as e:
        log.warning("BCA: Playwright failed (%s), trying requests", e)

    try:
        return _via_requests()
    except Exception as e:
        return _fail(f"Both methods failed. Last error: {e}")


def _via_playwright():
    from playwright.sync_api import sync_playwright
    log.info("BCA: launching Playwright")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()

        # Load login page
        page.goto(BCA_LOGIN, wait_until="networkidle", timeout=60000)

        # Fill credentials
        for sel in ["input[name='name']", "input[name='email']",
                    "input[type='email']", "input[name='username']"]:
            if page.query_selector(sel):
                page.fill(sel, BCA_USER)
                break

        for sel in ["input[name='pass']", "input[name='password']",
                    "input[type='password']"]:
            if page.query_selector(sel):
                page.fill(sel, BCA_PASS)
                break

        page.click("input[type='submit'], button[type='submit'], "
                   "button:has-text('Log in')")
        page.wait_for_load_state("networkidle", timeout=30000)

        if "login" in page.url.lower():
            browser.close()
            return _fail(
                f"BCA login rejected — check BCA_USER and BCA_PASS in GitHub Secrets. "
                f"Final URL: {page.url}"
            )

        log.info("BCA: logged in. Navigating to GIS FullView Portfolio…")

        # Navigate to GIS FullView Portfolio
        page.goto(BCA_GIS, wait_until="networkidle", timeout=30000)

        # Look for FullView Portfolio link
        for selector in [
            "a:has-text('FullView')",
            "a:has-text('Full View')",
            "a:has-text('GIS FullView')",
            "a[href*='fullview']",
            "a[href*='full-view']",
        ]:
            el = page.query_selector(selector)
            if el:
                el.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                break

        title = page.title()
        html  = page.content()
        browser.close()

    assets = _parse_html(html)
    if not assets:
        return _fail(
            f"BCA: logged in (page: '{title}') but could not parse FullView table. "
            "Page layout may have changed. Update BCA_MAP or _parse_html()."
        )

    log.info("BCA: extracted %d assets via Playwright", len(assets))
    return {
        "status":    "live",
        "message":   f"Retrieved via Playwright. Page: {title}",
        "retrieved": title,
        "assets":    assets
    }


def _via_requests():
    import requests
    from bs4 import BeautifulSoup

    log.info("BCA: attempting requests-based login")
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36")

    # Get CSRF token from login page
    r    = s.get(BCA_LOGIN, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    hidden = {i["name"]: i.get("value", "")
              for i in soup.find_all("input", type="hidden") if i.get("name")}

    s.post(BCA_LOGIN, data={"name": BCA_USER, "pass": BCA_PASS,
                             "op": "Log in", **hidden},
           timeout=30, allow_redirects=True)

    # Try GIS FullView page
    for url in [BCA_GIS, "https://www.bcaresearch.com/site/gis/fullview",
                "https://www.bcaresearch.com/topic?name=Asset+Allocation"]:
        r = s.get(url, timeout=30)
        assets = _parse_html(r.text)
        if assets:
            log.info("BCA: parsed %d assets from %s", len(assets), url)
            return {
                "status":    "live",
                "message":   f"Retrieved via HTTP session from {url}",
                "retrieved": url,
                "assets":    assets
            }

    return _fail("Requests login worked but could not find or parse FullView table.")


def _parse_html(html):
    """
    Parse BCA's FullView Portfolio table.
    BCA format (from GIS FullView Portfolio):
      Asset | GIS FullView Portfolio (%) | Benchmark (%) | Difference (%pts)

    The Difference column is the active weight — use directly.
    Returns: { asset_id: {"position": float, "benchmark": float, "activeWt": float} }
    """
    from bs4 import BeautifulSoup
    soup   = BeautifulSoup(html, "html.parser")
    result = {}

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower()
                   for th in table.find_all("th")]

        # Identify columns
        has_portfolio  = any("portfolio" in h or "fullview" in h
                             or "full view" in h or "gis" in h for h in headers)
        has_benchmark  = any("benchmark" in h for h in headers)
        has_difference = any("diff" in h or "active" in h or "%pt" in h
                             or "pts" in h for h in headers)

        if not (has_portfolio and has_benchmark):
            continue

        # Find column indices
        port_col = next((i for i, h in enumerate(headers)
                         if "portfolio" in h or "fullview" in h
                         or "full view" in h or "gis" in h), 1)
        bm_col   = next((i for i, h in enumerate(headers)
                         if "benchmark" in h), 2)
        diff_col = next((i for i, h in enumerate(headers)
                         if "diff" in h or "active" in h
                         or "%pt" in h or "pts" in h), None)

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            label    = cells[0].lower().strip()
            asset_id = _map(label)
            if not asset_id:
                continue

            port = _parse_pct(cells[port_col]) if port_col < len(cells) else None
            bm   = _parse_pct(cells[bm_col])   if bm_col   < len(cells) else None

            if port is None or bm is None:
                continue

            # Use the Difference column if available (BCA provides it directly)
            if diff_col is not None and diff_col < len(cells):
                diff = _parse_pct(cells[diff_col])
                aw   = diff if diff is not None else round(port - bm, 2)
            else:
                aw = round(port - bm, 2)

            result[asset_id] = {
                "position":  port,
                "benchmark": bm,
                "activeWt":  round(aw, 2)
            }

    # Text-scan fallback
    if not result:
        text = soup.get_text(separator="\n")
        for line in text.split("\n"):
            label    = line.lower().strip()
            asset_id = _map(label)
            if not asset_id:
                continue
            nums = re.findall(r"-?\d+\.?\d*", line)
            if len(nums) >= 3:
                try:
                    port = float(nums[0])
                    bm   = float(nums[1])
                    diff = float(nums[2])
                    result[asset_id] = {
                        "position":  port,
                        "benchmark": bm,
                        "activeWt":  round(diff, 2)
                    }
                except Exception:
                    pass

    return result


def _map(label):
    for phrase, aid in BCA_MAP.items():
        if phrase in label:
            return aid
    return None


def _parse_pct(s):
    m = re.search(r"(-?\d+\.?\d*)", str(s).replace(",", ""))
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _fail(msg):
    log.error("BCA: %s", msg)
    return {"status": "failed", "message": msg, "retrieved": "none", "assets": {}}
