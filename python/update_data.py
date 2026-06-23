"""
python/update_data.py
======================
Master orchestrator. Runs all three scrapers, merges results,
computes month-over-month changes, appends to history, and writes
data/allocations.json + data/source_status.json.

GitHub Actions runs this on the 1st of every month.
Run locally with:  python python/update_data.py
Run in demo mode:  python python/update_data.py --demo
"""

import sys
import os
import json
import logging
import argparse
from datetime import date, datetime
from pathlib import Path

# Make scrapers importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scrapers"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

DATA_DIR   = Path(__file__).parent.parent / "data"
OUTPUT     = DATA_DIR / "allocations.json"
STATUS_OUT = DATA_DIR / "source_status.json"
FALLBACK   = DATA_DIR / "allocations_fallback.json"

MAX_HISTORY = 12  # months of history to keep

# ── Signal score mapping (for MoM change calculation) ──
SCORE = {"HOW": 2, "OW": 1, "N": 0, "UW": -1, "HUW": -2}

# ── Canonical asset metadata (name, group, benchmark descriptions) ──
ASSET_META = [
    {"id": "us-eq",     "group": "Equities",     "cat": "equity",
     "name": "U.S. Equities",              "sub": "S&P 500 / Russell 3000",
     "ndrBm": "NDR 60/40 model — US weight within 60% MSCI ACWI equity sleeve",
     "blBm":  "BII — broad global equity market-cap weight (MSCI ACWI)",
     "bcaBm": "BCA GIS — MSCI ACWI neutral weight for US equities"},

    {"id": "intl-eq",   "group": "Equities",     "cat": "equity",
     "name": "International Equities",     "sub": "Developed ex-US (EAFE)",
     "ndrBm": "NDR 60/40 model — MSCI ACWI ex-US developed weight within equity sleeve",
     "blBm":  "BII — global market-cap weight for EAFE (MSCI EAFE)",
     "bcaBm": "BCA GIS — MSCI ACWI ex-US developed markets neutral weight"},

    {"id": "us-lc",     "group": "Equities",     "cat": "equity",
     "name": "U.S. Large-Cap",             "sub": "S&P 500 / Russell 1000",
     "ndrBm": "NDR US equity model — Russell 1000 vs equal-weight / mid-small blend",
     "blBm":  "BII — MSCI USA (broad US equity market-cap, large-cap dominated)",
     "bcaBm": "BCA GIS — S&P 500 market-cap weight within US equity allocation"},

    {"id": "us-sc",     "group": "Equities",     "cat": "equity",
     "name": "U.S. Small-Cap",             "sub": "Russell 2000",
     "ndrBm": "NDR US equity model — Russell 2000 weight within US equity allocation",
     "blBm":  "BII — MSCI USA Small Cap vs broad MSCI USA benchmark",
     "bcaBm": "BCA GIS — Russell 2000 weight within US equity model portfolio"},

    {"id": "us-gr",     "group": "Equities",     "cat": "equity",
     "name": "U.S. Growth",                "sub": "Russell 1000 Growth / Nasdaq-100",
     "ndrBm": "NDR US equity model — Russell 1000 Growth vs style-neutral Russell 1000 blend",
     "blBm":  "BII — MSCI USA Growth vs MSCI USA broad (style tilt)",
     "bcaBm": "BCA GIS — Russell 1000 Growth vs style-neutral Russell 1000 benchmark"},

    {"id": "us-val",    "group": "Equities",     "cat": "equity",
     "name": "U.S. Value",                 "sub": "Russell 1000 Value",
     "ndrBm": "NDR US equity model — Russell 1000 Value vs style-neutral Russell 1000 blend",
     "blBm":  "BII — MSCI USA Value vs MSCI USA broad (style tilt)",
     "bcaBm": "BCA GIS — Russell 1000 Value vs style-neutral Russell 1000 benchmark"},

    {"id": "em-eq",     "group": "Equities",     "cat": "equity",
     "name": "Emerging Markets Equities",  "sub": "MSCI EM",
     "ndrBm": "NDR 60/40 model — MSCI ACWI EM weight within equity sleeve",
     "blBm":  "BII — MSCI Emerging Markets vs broad global equity market-cap weight",
     "bcaBm": "BCA GIS — MSCI EM neutral weight within global equity allocation"},

    {"id": "dev-intl",  "group": "Equities",     "cat": "equity",
     "name": "Developed Int'l Equities",   "sub": "EAFE — Europe, Japan, Australia",
     "ndrBm": "NDR 60/40 model — MSCI EAFE weight within 60% MSCI ACWI equity sleeve",
     "blBm":  "BII — MSCI EAFE vs global equity market-cap",
     "bcaBm": "BCA GIS — MSCI EAFE neutral weight within global equity allocation"},

    {"id": "govt-us",   "group": "Fixed Income", "cat": "fixed",
     "name": "Govt Bonds — U.S. Treasuries","sub": "All maturities",
     "ndrBm": "NDR 60/40 model — US Treasury weight within 40% Bloomberg Barclays Global Agg",
     "blBm":  "BII — Bloomberg US Treasury Index vs global bond market-cap weight",
     "bcaBm": "BCA GIS — US Treasury weight within Bloomberg Global Aggregate benchmark"},

    {"id": "intl-debt", "group": "Fixed Income", "cat": "fixed",
     "name": "International Govt Bonds",   "sub": "DM sovereign ex-US",
     "ndrBm": "NDR 60/40 model — ex-US sovereign weight within Bloomberg Barclays Global Agg",
     "blBm":  "BII — per-country sovereign benchmarks",
     "bcaBm": "BCA GIS — ex-US sovereign weight within Bloomberg Global Aggregate"},

    {"id": "ig-credit", "group": "Fixed Income", "cat": "fixed",
     "name": "Investment Grade Credit",    "sub": "Corporate bonds BBB–AAA",
     "ndrBm": "NDR 60/40 model — IG credit weight within Bloomberg Barclays Global Agg",
     "blBm":  "BII — Bloomberg Global Corporate IG Index vs global bond market weight",
     "bcaBm": "BCA GIS — IG credit weight within Bloomberg Global Aggregate"},

    {"id": "hy-bonds",  "group": "Fixed Income", "cat": "fixed",
     "name": "High Yield Bonds",           "sub": "BB–B rated corporate",
     "ndrBm": "NDR fixed income model — HY weight within tactical fixed income allocation",
     "blBm":  "BII — Bloomberg Global High Yield Index vs global bond market weight",
     "bcaBm": "BCA GIS — HY weight within Bloomberg Global Aggregate / tactical FI model"},

    {"id": "em-bonds",  "group": "Fixed Income", "cat": "fixed",
     "name": "EM Bonds — Hard Currency",   "sub": "EM sovereign USD-denominated",
     "ndrBm": "NDR fixed income model — EM hard currency weight within Bloomberg Barclays Global Agg",
     "blBm":  "BII — J.P. Morgan EMBI vs global bond market-cap weight",
     "bcaBm": "BCA GIS — J.P. Morgan EMBI weight within global fixed income allocation"},

    {"id": "gold",      "group": "Alternatives", "cat": "alts",
     "name": "Gold",                       "sub": "Spot / GLD / central bank proxy",
     "ndrBm": "NDR alternatives model — gold vs near-zero strategic baseline",
     "blBm":  "BII — gold vs its role as a portfolio diversifier",
     "bcaBm": "BCA GIS — gold vs zero or minimal strategic baseline"},

    {"id": "oil",       "group": "Alternatives", "cat": "alts",
     "name": "Oil / Energy Commodities",   "sub": "Brent / WTI crude",
     "ndrBm": "NDR alternatives model — energy vs near-zero neutral commodity allocation",
     "blBm":  "BII — Brent crude / energy vs portfolio neutral commodity allocation",
     "bcaBm": "BCA GIS — MacroQuant energy vs neutral commodity allocation"},
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="Use fallback data — no live API calls")
    args = parser.parse_args()

    if args.demo:
        logger.info("DEMO MODE — using fallback data, no API calls")
        data = load_fallback()
        data["source"] = "demo"
        save(data)
        save_status([
            {"firm": "NDR",        "status": "demo", "message": "Demo mode — no live retrieval"},
            {"firm": "BlackRock",  "status": "demo", "message": "Demo mode — no live retrieval"},
            {"firm": "BCA",        "status": "demo", "message": "Demo mode — no live retrieval"},
        ])
        return

    # ── Import scrapers ──
    from ndr_scraper        import fetch_ndr
    from blackrock_scraper  import fetch_blackrock
    from bca_scraper        import fetch_bca

    # ── Load previous data for MoM comparison ──
    prev = load_previous()

    # ── Run all three scrapers ──
    scraper_results = {}
    status_records  = []

    for firm_key, fetcher, firm_label in [
        ("NDR",       fetch_ndr,        "NDR House Views"),
        ("BlackRock", fetch_blackrock,  "BlackRock BII Weekly"),
        ("BCA",       fetch_bca,        "BCA GIS FullView"),
    ]:
        logger.info("━━━ Running %s scraper…", firm_label)
        try:
            result = fetcher()
        except Exception as e:
            result = {
                "status":    "failed",
                "message":   f"Unhandled exception: {e}",
                "retrieved": "none",
                "broad":     {},
                "signals":   {}
            }

        scraper_results[firm_key] = result
        status_records.append({
            "firm":        firm_label,
            "firm_key":    firm_key,
            "status":      result["status"],
            "message":     result["message"],
            "retrieved":   result["retrieved"],
            "retrieved_on": datetime.now().strftime("%B %d, %Y at %H:%M UTC"),
            "retrieved_by": "GitHub Actions — automated scraper"
        })

        if result["status"] == "live":
            logger.info("✓ %s: SUCCESS — %s", firm_label, result["message"])
        else:
            logger.warning("✗ %s: %s — using previous data", firm_label, result["status"].upper())
            # Fill in previous month's signals as fallback
            prev_sigs = {a["id"]: a for a in prev.get("assets", [])}
            for asset_id, asset in prev_sigs.items():
                if asset_id not in result["signals"]:
                    prev_sig = asset.get(firm_key)
                    if prev_sig:
                        result["signals"][asset_id] = prev_sig

    # ── Build merged asset list ──
    today      = str(date.today())
    month_label = datetime.today().strftime("%b %Y")
    prev_sigs   = {a["id"]: a for a in prev.get("assets", [])}

    assets = []
    for meta in ASSET_META:
        aid = meta["id"]

        ndr_sig = scraper_results["NDR"]["signals"].get(aid, "N")
        bl_sig  = scraper_results["BlackRock"]["signals"].get(aid, "N")
        bca_sig = scraper_results["BCA"]["signals"].get(aid, "N")

        prev_row = prev_sigs.get(aid, {})

        # Build notes from retrieval results
        ndr_note = (
            f"NDR {scraper_results['NDR']['status'].upper()}: "
            f"{scraper_results['NDR']['message'][:200]}"
        )
        bl_note = (
            f"BlackRock BII {scraper_results['BlackRock']['status'].upper()}: "
            f"{scraper_results['BlackRock']['message'][:200]}"
        )
        bca_note = (
            f"BCA GIS {scraper_results['BCA']['status'].upper()}: "
            f"{scraper_results['BCA']['message'][:200]}"
        )

        assets.append({
            **meta,
            "NDR":       ndr_sig,
            "BlackRock": bl_sig,
            "BCA":       bca_sig,
            "ndrNote":   ndr_note,
            "blNote":    bl_note,
            "bcaNote":   bca_note,
            "momN": mom_change(prev_row.get("NDR"),       ndr_sig),
            "momB": mom_change(prev_row.get("BlackRock"), bl_sig),
            "momC": mom_change(prev_row.get("BCA"),       bca_sig),
        })

    # ── Build history snapshot ──
    history_snap = {
        "month":  month_label,
        "assets": [{"id": a["id"], "NDR": a["NDR"],
                    "BlackRock": a["BlackRock"], "BCA": a["BCA"]}
                   for a in assets]
    }
    history = prev.get("history", [])
    history = [h for h in history if h.get("month") != month_label]
    history.append(history_snap)
    history = history[-MAX_HISTORY:]

    # ── Broad allocation ──
    broad = {}
    for firm_key, default_broad in [
        ("NDR",       {"equity": 57, "debt": 31, "cash": 12}),
        ("BlackRock", {"equity": 64, "debt": 28, "cash":  8}),
        ("BCA",       {"equity": 55, "debt": 34, "cash": 11}),
    ]:
        result_broad = scraper_results[firm_key].get("broad", {})
        broad[firm_key] = {
            "equity": result_broad.get("equity", default_broad["equity"]),
            "debt":   result_broad.get("debt",   default_broad["debt"]),
            "cash":   result_broad.get("cash",   default_broad["cash"]),
            "note":   result_broad.get("note",   f"{firm_key} — retrieved {today}")
        }

    # ── Write outputs ──
    output = {
        "as_of":   today,
        "source":  "live",
        "broad":   broad,
        "assets":  assets,
        "history": history,
    }
    save(output)
    save_status(status_records)

    # ── Summary ──
    live_count   = sum(1 for s in status_records if s["status"] == "live")
    failed_count = sum(1 for s in status_records if s["status"] == "failed")
    logger.info("━━━ DONE: %d/%d providers live, %d failed",
                live_count, len(status_records), failed_count)

    if failed_count > 0:
        logger.warning(
            "Failed providers used previous month's data as fallback. "
            "Check the logs above for details. "
            "Manual update may be required for failed providers."
        )


def mom_change(prev_sig, curr_sig):
    """Compute month-over-month direction change."""
    if not prev_sig or not curr_sig:
        return "nc"
    delta = SCORE.get(curr_sig, 0) - SCORE.get(prev_sig, 0)
    return "up" if delta > 0 else "dn" if delta < 0 else "nc"


def load_previous():
    """Load last month's data for MoM comparison."""
    for path in [OUTPUT, FALLBACK]:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    return {}


def load_fallback():
    if FALLBACK.exists():
        with open(FALLBACK) as f:
            return json.load(f)
    return {}


def save(data):
    DATA_DIR.mkdir(exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Wrote %s", OUTPUT)


def save_status(records):
    DATA_DIR.mkdir(exist_ok=True)
    with open(STATUS_OUT, "w") as f:
        json.dump({"updated": datetime.now().isoformat(), "providers": records}, f, indent=2)
    logger.info("Wrote %s", STATUS_OUT)


if __name__ == "__main__":
    main()
