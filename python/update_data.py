"""
python/update_data.py
======================
Master orchestrator — runs all three scrapers, merges results,
computes month-over-month changes, appends to 12-month history,
and writes data/allocations.json.

Run locally:
  cd dashboard/
  pip install -r requirements.txt
  python python/update_data.py

Run with dummy/fallback data (no API credentials needed):
  python python/update_data.py --demo

GitHub Actions runs this automatically on the 1st of each month.
"""

import sys
import os
import json
import copy
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

# Make scrapers importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scrapers"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT   = DATA_DIR / "allocations.json"
FALLBACK = DATA_DIR / "allocations_fallback.json"

# Maximum months of history to retain
MAX_HISTORY_MONTHS = 12

# Canonical asset definitions (metadata never changes, only signals change)
ASSET_META = [
    {"id":"us-eq",     "group":"Equities",     "cat":"equity",       "name":"U.S. Equities",              "sub":"Broad US market"},
    {"id":"intl-eq",   "group":"Equities",     "cat":"equity",       "name":"International Equities",     "sub":"Developed ex-US"},
    {"id":"us-lc",     "group":"Equities",     "cat":"equity",       "name":"U.S. Large-Cap",             "sub":"S&P 500 / Russell 1000"},
    {"id":"us-sc",     "group":"Equities",     "cat":"equity",       "name":"U.S. Small-Cap",             "sub":"Russell 2000"},
    {"id":"us-gr",     "group":"Equities",     "cat":"equity",       "name":"U.S. Growth",                "sub":"Russell 1000 Growth / QQQ"},
    {"id":"us-val",    "group":"Equities",     "cat":"equity",       "name":"U.S. Value",                 "sub":"Russell 1000 Value"},
    {"id":"em-eq",     "group":"Equities",     "cat":"equity",       "name":"Emerging Markets Equities",  "sub":"MSCI EM"},
    {"id":"dev-intl",  "group":"Equities",     "cat":"equity",       "name":"Developed (Int'l) Equities", "sub":"EAFE — Europe, Japan, Pacific"},
    {"id":"govt-us",   "group":"Fixed Income", "cat":"fixed",        "name":"Govt Bonds (U.S.)",          "sub":"Treasuries — all maturities"},
    {"id":"intl-debt", "group":"Fixed Income", "cat":"fixed",        "name":"International Debt",         "sub":"DM sovereign ex-US"},
    {"id":"ig-credit", "group":"Fixed Income", "cat":"fixed",        "name":"Investment Grade (IG) Credit","sub":"Corp bonds — BBB to AAA"},
    {"id":"hy-bonds",  "group":"Fixed Income", "cat":"fixed",        "name":"High Yield Bonds",           "sub":"BB-B rated corporate HY"},
    {"id":"em-bonds",  "group":"Fixed Income", "cat":"fixed",        "name":"Emerging Markets Bonds",     "sub":"EM Sovereign & Corporate USD"},
    {"id":"gold",      "group":"Alternatives", "cat":"alternatives", "name":"Gold",                       "sub":"Spot + futures exposure"},
    {"id":"oil",       "group":"Alternatives", "cat":"alternatives", "name":"Oil / Energy Commodities",   "sub":"WTI / Brent crude"},
]

SCORE = {"HOW": 2, "OW": 1, "N": 0, "UW": -1, "HUW": -2}
NOTES = {
    "us-eq":     "U.S. equity signals driven by earnings momentum, valuation, and macro backdrop",
    "intl-eq":   "International equity relative to US — currency, growth differential, valuation",
    "us-lc":     "Large-cap driven by mega-cap tech earnings and AI capital expenditure cycle",
    "us-sc":     "Small-cap sensitive to rate environment and domestic credit conditions",
    "us-gr":     "Growth vs value driven by duration sensitivity and earnings growth premium",
    "us-val":    "Value tilts toward energy, financials, and defensive dividend payers",
    "em-eq":     "EM equity influenced by USD strength, China policy, and commodity cycles",
    "dev-intl":  "EAFE driven by ECB/BOJ policy path, EUR/JPY, and European earnings cycle",
    "govt-us":   "US Treasuries — duration view, Fed path, and recession hedge positioning",
    "intl-debt": "DM sovereign ex-US — currency hedging cost and yield differential",
    "ig-credit": "Investment grade corporate — spread tightness vs default risk outlook",
    "hy-bonds":  "High yield — credit cycle positioning, default rate forecast, spread vs HY",
    "em-bonds":  "EM USD debt — dollar exposure, EM fundamentals, spread vs Treasuries",
    "gold":      "Gold — real rates, USD direction, central bank demand, geopolitical risk",
    "oil":       "Energy/oil — supply/demand balance, OPEC+ policy, geopolitical premium",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="Use demo data instead of live API calls")
    args = parser.parse_args()

    if args.demo:
        logger.info("Running in DEMO mode — using fallback data, no API calls")
        data = load_fallback()
        data["source"] = "demo"
        save(data)
        return

    # Load previous data to compute MoM changes and append history
    prev = load_previous()

    # ── Fetch from all three providers ──────────────────────────────
    from ndr_scraper       import fetch_ndr
    from blackrock_scraper import fetch_blackrock
    from bca_scraper       import fetch_bca

    results = {}
    errors  = {}

    for name, fetcher in [("NDR", fetch_ndr), ("BlackRock", fetch_blackrock), ("BCA", fetch_bca)]:
        logger.info(f"━━━ Fetching {name}…")
        try:
            results[name] = fetcher()
            logger.info(f"✓ {name}: got {len(results[name]['signals'])} asset signals")
        except Exception as e:
            logger.error(f"✗ {name} FAILED: {e}")
            errors[name] = str(e)
            # Use previous data as fallback for this provider
            results[name] = _prev_signals_for(prev, name)

    # ── Build output ────────────────────────────────────────────────
    today     = str(date.today())
    prev_sigs = {a["id"]: a for a in prev.get("assets", [])}

    assets = []
    for meta in ASSET_META:
        aid = meta["id"]
        ndr = results["NDR"]["signals"].get(aid, "N")
        bl  = results["BlackRock"]["signals"].get(aid, "N")
        bca = results["BCA"]["signals"].get(aid, "N")

        prev_row = prev_sigs.get(aid, {})
        asset = {
            **meta,
            "NDR":       ndr,
            "BlackRock": bl,
            "BCA":       bca,
            "momNDR":    mom(prev_row.get("NDR"),       ndr),
            "momBL":     mom(prev_row.get("BlackRock"), bl),
            "momBCA":    mom(prev_row.get("BCA"),       bca),
            "note":      NOTES.get(aid, ""),
        }
        assets.append(asset)

    # ── Build history snapshot ───────────────────────────────────────
    month_label = datetime.today().strftime("%b %Y")
    history_snapshot = {
        "month":  month_label,
        "assets": [
            {"id": a["id"], "NDR": a["NDR"], "BlackRock": a["BlackRock"], "BCA": a["BCA"]}
            for a in assets
        ]
    }

    history = prev.get("history", [])
    # Remove any existing entry for this month
    history = [h for h in history if h.get("month") != month_label]
    history.append(history_snapshot)
    # Keep last N months
    history = history[-MAX_HISTORY_MONTHS:]

    output = {
        "as_of":   today,
        "source":  "live",
        "errors":  errors,
        "broad": {
            "NDR":       results["NDR"]["broad"],
            "BlackRock": results["BlackRock"]["broad"],
            "BCA":       results["BCA"]["broad"],
        },
        "assets":  assets,
        "history": history,
    }

    save(output)
    logger.info(f"✓ data/allocations.json written for {today}")
    if errors:
        logger.warning(f"Providers with errors (used previous data): {list(errors.keys())}")


# ─────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────
def mom(prev_sig, curr_sig):
    if not prev_sig or not curr_sig:
        return "nc"
    d = SCORE.get(curr_sig, 0) - SCORE.get(prev_sig, 0)
    return "up" if d > 0 else ("dn" if d < 0 else "nc")


def load_previous():
    if OUTPUT.exists():
        with open(OUTPUT) as f:
            return json.load(f)
    if FALLBACK.exists():
        with open(FALLBACK) as f:
            return json.load(f)
    return {}


def load_fallback():
    with open(FALLBACK) as f:
        return json.load(f)


def _prev_signals_for(prev, firm):
    """Extract last month's signals for a provider, used when a live fetch fails."""
    broad_fallback = {"equity": 55, "debt": 33, "cash": 12, "note": "Using previous month's data"}
    signals = {}
    for a in prev.get("assets", []):
        signals[a["id"]] = a.get(firm, "N")
    broad = prev.get("broad", {}).get(firm, broad_fallback)
    return {"broad": broad, "signals": signals}


def save(data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
