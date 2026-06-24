"""
scrapers/ndr_scraper.py
========================
Logs into NDR with NDR_USER / NDR_PASS (GitHub Secrets).
Reads the House Views page which shows:
  Asset | Position % | Benchmark % | Conviction (Over/Under/Market)

Active weight = Position % - Benchmark %
Signal is derived from active weight using the standard formula.
"""

import os, re, logging
log = logging.getLogger(__name__)

NDR_USER = os.environ.get("NDR_USER", "")
NDR_PASS = os.environ.get("NDR_PASS", "")
NDR_LOGIN = "https://www.ndr.com/group/ndr"

# Map NDR's asset labels to our canonical IDs
# Update these if NDR changes their terminology
NDR_MAP = {
    # Primary
    "stocks":              "equities",
    "equities":            "equities",
    "global equities":     "equities",
    "bonds":               "bonds",
    "fixed income":        "bonds",
    "cash":                "cash",
    "money market":        "cash",
    "gold":                "gold",
    # Sub-asset
    "large-cap":           "us-lc",
    "large cap":           "us-lc",
    "u.s. large":          "us-lc",
    "small-cap":           "us-sc",
    "small cap":           "us-sc",
    "u.s. small":          "us-sc",
    "growth":              "us-gr",
    "u.s. growth":         "us-gr",
    "u.s. tech":           "us-gr",
    "value":               "us-val",
    "u.s. value":          "us-val",
    "emerging market":     "em-eq",
    "emerging markets eq": "em-eq",
    "eafe":                "dev-intl",
    "europe":              "dev-intl",
    "international equity":"dev-intl",
    "developed int":       "dev-intl",
    "u.s. treasur":        "govt-us",
    "government bond":     "govt-us",
    "long-term u.s.":      "govt-us",
    "international bond":  "intl-debt",
    "international debt":  "intl-debt",
    "investment grade":    "ig-credit",
    "ig credit":           "ig-credit",
    "high yield":          "hy-bonds",
    "emerging markets bond":"em-bonds",
    "em debt":             "em-bonds",
    "oil":                 "oil",
    "energy":              "oil",
    "commodities":         "oil",
}


def fetch_ndr():
    """
    Returns:
    {
      "status": "live" | "failed",
      "message": str,
      "retrieved": str,     # page title or URL
      "assets": {
        "equities": {"position": 60.0, "benchmark": 55.0, "activeWt": 5.0},
        ...
      }
    }
    """
    if not NDR_USER or not NDR_PASS:
        return _fail("NDR_USER or NDR_PASS not set in GitHub Secrets.")

    # Try Playwright first (handles JS-rendered pages)
    try:
        return _via_playwright()
    except ImportError:
        log.warning("NDR: Playwright not installed, trying requests")
    except Exception as e:
        log.warning("NDR: Playwright failed (%s), trying requests", e)

    # Fallback to requests
    try:
        return _via_requests()
    except Exception as e:
        return _fail(f"Both methods failed. Last error: {e}")


def _via_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise

    log.info("NDR: launching Playwright")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        # Load login page
        page.goto(NDR_LOGIN, wait_until="networkidle", timeout=60000)

        # Fill credentials — NDR uses standard username/password form
        # Try common field selectors
        username_filled = False
        for sel in ["input[name='login']", "input[name='email']",
                    "input[type='email']", "input[name='username']",
                    "input#username", "input#email", "input[name='user']"]:
            el = page.query_selector(sel)
            if el:
                try:
                    el.fill(NDR_USER)
                    username_filled = True
                except Exception:
                    pass
                break

        password_selector = None
        for sel in ["input[name='password']", "input[type='password']", "input#password"]:
            el = page.query_selector(sel)
            if el:
                try:
                    el.fill(NDR_PASS)
                    password_selector = sel
                except Exception:
                    pass
                break

        # Try submitting. Some sites don't have a submit button or require pressing Enter.
        submitted = False
        try:
            # Try common submit buttons
            if page.query_selector("button[type='submit'], input[type='submit'], button:has-text('Sign in'), button:has-text('Log in')"):
                page.click("button[type='submit'], input[type='submit'], button:has-text('Sign in'), button:has-text('Log in')")
                submitted = True
        except Exception:
            submitted = False

        # If not submitted, try pressing Enter in the password field
        if not submitted and password_selector:
            try:
                el = page.query_selector(password_selector)
                if el:
                    el.press("Enter")
                    submitted = True
            except Exception:
                submitted = False

        # As a last resort, submit the first form on the page
        if not submitted:
            try:
                page.eval_on_selector("form", "f => f.submit()")
                submitted = True
            except Exception:
                pass

        page.wait_for_load_state("networkidle", timeout=30000)

        # Detect SSO/third-party login flows which cannot be automated here
        current_url = page.url or ""
        if "login" in current_url.lower() and "ndr" in current_url.lower():
            browser.close()
            return _fail(
                f"NDR login rejected — check NDR_USER and NDR_PASS in GitHub Secrets. Final URL: {current_url}"
            )

        # If we've been redirected off ndr.com (e.g., Okta/Azure/SSO), surface that
        if current_url and "ndr.com" not in current_url.lower():
            browser.close()
            return _fail(
                f"NDR appears to use SSO or a third-party identity provider (redirected to {current_url}). "
                "Automated username/password login is likely not possible."
            )

        log.info("NDR: logged in. Looking for House Views page…")

        # Navigate to House Views
        # Try clicking a nav link first
        for selector in [
            "a:has-text('House Views')",
            "a:has-text('Model Portfolios')",
            "a[href*='house-views']",
            "a[href*='model-portfolio']",
        ]:
            el = page.query_selector(selector)
            if el:
                try:
                    el.click()
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                break

        title = page.title()
        html = page.content()
        # Save a copy for debugging when parsing fails
        try:
            with open("/tmp/ndr_last_page.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            # ignore filesystem errors; helpful when running locally
            pass

        browser.close()

    assets = _parse_html(html)
    if not assets:
        return _fail(
            f"NDR: logged in and navigated (page: '{title}') but could not parse "
            "position/benchmark table. The page layout may have changed. "
            "Please check ndr.com/group/ndr and update NDR_MAP or _parse_html()."
        )

    log.info("NDR: extracted %d assets via Playwright", len(assets))
    return {
        "status":    "live",
        "message":   f"Retrieved via Playwright. Page: {title}",
        "retrieved": title,
        "assets":    assets
    }


def _via_requests():
    import requests
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    log.info("NDR: attempting requests-based login")
    s = requests.Session()
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36")

    # Load login page to capture hidden fields and form
    r = s.get(NDR_LOGIN, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    # Find a login form and discover the input names and action
    form = soup.find("form")
    login_action = NDR_LOGIN
    hidden = {i["name"]: i.get("value", "")
              for i in soup.find_all("input", type="hidden") if i.get("name")}

    username_field = None
    password_field = None

    if form:
        action = form.get("action")
        if action:
            login_action = urljoin(NDR_LOGIN, action)

        # Inspect inputs inside the form for likely username/password fields
        for inp in form.find_all("input"):
            n = inp.get("name") or ""
            t = (inp.get("type") or "").lower()
            nl = n.lower()
            if (t in ("text", "email") or 'user' in nl or 'login' in nl or 'email' in nl) and not username_field:
                username_field = n
            if t == "password" or 'pass' in nl:
                password_field = n

    # Fallback common names
    if not username_field:
        username_field = "login"
    if not password_field:
        password_field = "password"

    payload = {**hidden, username_field: NDR_USER, password_field: NDR_PASS}
    # also add common aliases so the form receives at least one expected field
    payload.setdefault('email', NDR_USER)
    payload.setdefault('login', NDR_USER)
    payload.setdefault('password', NDR_PASS)

    r2 = s.post(login_action, data=payload, timeout=30, allow_redirects=True)

    # If the login endpoint redirected to a non-NDR domain, it's likely SSO
    if r2.history and r2.url and 'ndr.com' not in r2.url.lower():
        return _fail(f"NDR appears to use SSO or third-party auth (redirect to {r2.url}). "
                     "Automated username/password login is likely not possible.")

    # Try fetching House Views directly
    for url in [
        "https://www.ndr.com/group/ndr/-/media/house-views",
        "https://www.ndr.com/hs/model-portfolios",
        "https://www.ndr.com/group/ndr",
    ]:
        r = s.get(url, timeout=30)
        assets = _parse_html(r.text)
        if assets:
            log.info("NDR: parsed %d assets from %s", len(assets), url)
            return {
                "status":    "live",
                "message":   f"Retrieved via HTTP session from {url}",
                "retrieved": url,
                "assets":    assets
            }

    # Save last response for debugging
    try:
        with open('/tmp/ndr_last_requests.html', 'w', encoding='utf-8') as f:
            f.write(r2.text if 'r2' in locals() and r2 is not None else r.text)
    except Exception:
        pass

    return _fail("Requests login worked but could not find or parse House Views table.")


def _parse_html(html):
    """
    Parse NDR's position/benchmark table.
    NDR format (from House Views page):
      Asset | Position % | Benchmark % | Signal (Over/Under/Market)

    Returns dict: { asset_id: {"position": float, "benchmark": float, "activeWt": float} }
    Update this function if NDR changes their page layout.
    """
    from bs4 import BeautifulSoup
    soup   = BeautifulSoup(html, "html.parser")
    result = {}

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower()
                   for th in table.find_all("th")]

        # Look for a table that has position and benchmark columns
        has_pos = any("position" in h or "weight" in h for h in headers)
        has_bm  = any("benchmark" in h for h in headers)
        if not (has_pos and has_bm):
            continue

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            label = cells[0].lower().strip()
            asset_id = _map(label)
            if not asset_id:
                continue

            # Extract numeric percentages from cells
            nums = []
            for cell in cells[1:]:
                pct = _parse_pct(cell)
                if pct is not None:
                    nums.append(pct)

            if len(nums) >= 2:
                pos = nums[0]
                bm  = nums[1]
                result[asset_id] = {
                    "position":  pos,
                    "benchmark": bm,
                    "activeWt":  round(pos - bm, 2)
                }

    # Also try scanning for key-value pairs in free text
    if not result:
        text = soup.get_text(separator="\n")
        for line in text.split("\n"):
            line_lower = line.lower()
            asset_id = _map(line_lower)
            if not asset_id:
                continue
            nums = re.findall(r"(\d+\.?\d*)\s*%", line)
            if len(nums) >= 2:
                pos = float(nums[0])
                bm  = float(nums[1])
                result[asset_id] = {
                    "position":  pos,
                    "benchmark": bm,
                    "activeWt":  round(pos - bm, 2)
                }

    return result


def _map(label):
    label = label.lower().strip()
    for phrase, aid in NDR_MAP.items():
        if phrase in label:
            return aid
    return None


def _parse_pct(s):
    m = re.search(r"(-?\d+\.?\d*)\s*%?", s.replace(",", ""))
    try:
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _fail(msg):
    log.error("NDR: %s", msg)
    return {"status": "failed", "message": msg, "retrieved": "none", "assets": {}}
