"""
python/update_data.py
======================
Master orchestrator. Runs all three scrapers, applies the universal
scoring formula, appends to monthly history, and writes:
  data/allocations.json  — the dashboard data file

Run manually:  python python/update_data.py
Demo mode:     python python/update_data.py --demo

GitHub Actions runs this on the 1st of every month automatically.
"""

import sys, os, json, logging, argparse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scrapers"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

ROOT    = Path(__file__).parent.parent
OUT     = ROOT / "data" / "allocations.json"
HISTORY = 12  # months to keep

# ── Universal scoring formula ──
# NDR and BCA: score derived from active weight (position - benchmark)
# BII: score derived from directional signal text
def score_from_aw(aw):
    """Active weight → conviction score."""
    if aw is None: return None
    if aw >  5:  return  2   # Heavy Overweight
    if aw >  2:  return  1   # Overweight
    if aw >= -2: return  0   # Market Weight
    if aw >= -5: return -1   # Underweight
    return -2               # Heavy Underweight

def score_from_bii(signal_text):
    """BII signal text → conviction score."""
    if not signal_text: return None
    s = signal_text.lower()
    if "high conviction" in s and "over"  in s: return  2
    if "high conviction" in s and "under" in s: return -2
    if "overweight"  in s: return  1
    if "neutral"     in s: return  0
    if "underweight" in s: return -1
    return None

# ── All asset IDs we track ──
# Primary assets (Table 1)
PRIMARY = ["equities", "bonds", "cash", "gold"]

# Sub-asset classes (Table 2)
SUB = [
    "us-lc", "us-sc", "us-gr", "us-val",
    "em-eq", "dev-intl",
    "govt-us", "intl-debt", "ig-credit", "hy-bonds", "em-bonds",
    "oil",
]

ALL_ASSETS = PRIMARY + SUB


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="Use demo data — no live scraping")
    args = parser.parse_args()

    if args.demo:
        log.info("DEMO MODE — generating representative data, no live logins")
        data = _demo_data()
        _save(data)
        return

    # ── Import scrapers ──
    from ndr_scraper import fetch_ndr
    from bca_scraper import fetch_bca
    from bii_scraper import fetch_bii

    # ── Load previous data for history ──
    prev = _load_prev()

    # ── Run scrapers ──
    log.info("━━━ Fetching NDR House Views…")
    ndr = _run(fetch_ndr, "NDR")

    log.info("━━━ Fetching BCA GIS FullView Portfolio…")
    bca = _run(fetch_bca, "BCA")

    log.info("━━━ Fetching BlackRock BII Asset Class Views…")
    bii = _run(fetch_bii, "BII")

    # ── Build asset records ──
    today    = str(date.today())
    month    = datetime.today().strftime("%b %Y")
    assets   = []
    snap_assets = []  # for history snapshot

    for aid in ALL_ASSETS:
        # NDR — provides position/benchmark weights
        ndr_raw = ndr["assets"].get(aid, {})
        ndr_aw  = ndr_raw.get("activeWt")
        ndr_pos = ndr_raw.get("position")
        ndr_bm  = ndr_raw.get("benchmark")
        ndr_score = score_from_aw(ndr_aw)

        # BCA — provides position/benchmark/difference weights
        bca_raw  = bca["assets"].get(aid, {})
        bca_aw   = bca_raw.get("activeWt")
        bca_pos  = bca_raw.get("position")
        bca_bm   = bca_raw.get("benchmark")
        bca_score = score_from_aw(bca_aw)

        # BII — provides directional signal text
        bii_raw   = bii["assets"].get(aid, {})
        bii_sig   = bii_raw.get("signal")
        bii_score = bii_raw.get("score") if "score" in bii_raw \
                    else score_from_bii(bii_sig)

        # Skip asset if no firm covers it at all
        if ndr_score is None and bca_score is None and bii_score is None:
            continue

        record = {
            "id":  aid,
            # NDR data
            "ndrActiveWt":  ndr_aw,
            "ndrPosition":  ndr_pos,
            "ndrBenchmark": ndr_bm,
            "ndrScore":     ndr_score,
            # BCA data
            "bcaActiveWt":  bca_aw,
            "bcaPosition":  bca_pos,
            "bcaBenchmark": bca_bm,
            "bcaScore":     bca_score,
            # BII data
            "biiSignal": bii_sig,
            "biiScore":  bii_score,
        }
        assets.append(record)

        # Compact snapshot for history
        snap_assets.append({
            "id":       aid,
            "ndrScore": ndr_score,
            "bcaScore": bca_score,
            "biiScore": bii_score,
        })

    # ── Append to history ──
    history = list(prev.get("history", []))
    history = [h for h in history if h.get("month") != month]
    history.append({"month": month, "assets": snap_assets})
    history = history[-HISTORY:]

    # ── Write output ──
    output = {
        "as_of":  today,
        "sources": {
            "ndr": {
                "status":    ndr["status"],
                "message":   ndr["message"],
                "retrieved": ndr["retrieved"],
                "page":      "NDR House Views — ndr.com/group/ndr"
            },
            "bca": {
                "status":    bca["status"],
                "message":   bca["message"],
                "retrieved": bca["retrieved"],
                "page":      "BCA GIS FullView Portfolio — bcaresearch.com/site/gis/home"
            },
            "bii": {
                "status":    bii["status"],
                "message":   bii["message"],
                "retrieved": bii["retrieved"],
                "page":      "BlackRock BII — blackrock.com/corporate/insights/blackrock-investment-institute/publications/outlook"
            },
        },
        "assets":  assets,
        "history": history,
    }
    _save(output)

    # ── Summary ──
    live = sum(1 for s in [ndr, bca, bii] if s["status"] == "live")
    log.info("━━━ DONE: %d/3 sources live · %d assets recorded · history: %d months",
             live, len(assets), len(history))

    for name, result in [("NDR", ndr), ("BCA", bca), ("BII", bii)]:
        icon = "✓" if result["status"] == "live" else "✗"
        log.info("  %s %s: %s", icon, name, result["message"][:100])


def _run(fetcher, name):
    """Run a scraper, catch all exceptions."""
    try:
        return fetcher()
    except Exception as e:
        log.error("%s: unhandled exception: %s", name, e)
        return {
            "status":    "failed",
            "message":   f"Unhandled exception: {e}",
            "retrieved": "none",
            "assets":    {}
        }


def _load_prev():
    if OUT.exists():
        with open(OUT) as f:
            return json.load(f)
    return {}


def _save(data):
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
    log.info("Wrote %s", OUT)


def _demo_data():
    """
    Representative demo data. Shows what the dashboard looks like
    with realistic signals. Replace with live data after first run.

    Signal formula reminder:
      Active weight > +5pp  → Heavy OW  (+2)
      +2 to +5pp            → OW        (+1)
      -2 to +2pp            → MW        (0)
      -2 to -5pp            → UW        (-1)
      < -5pp                → Heavy UW  (-2)
    """
    today = str(date.today())
    month = datetime.today().strftime("%b %Y")

    # Representative signals based on June 2026 published views
    DEMO = {
        # id:          (ndr_aw, bca_aw, bii_signal)
        "equities":   ( 5.0,  -5.1,  "Overweight"),
        "bonds":      (-9.0,  -2.0,  "Underweight"),
        "cash":       ( 12.0,  5.0,  None),
        "gold":       ( 3.0,   4.5,  "Overweight"),
        "us-lc":      ( 3.0,   2.5,  "Overweight"),
        "us-sc":      ( 0.0,  -6.0,  None),
        "us-gr":      ( 3.0,   2.0,  "Overweight"),
        "us-val":     ( 0.0,   0.0,  None),
        "em-eq":      ( 0.0,   0.0,  "Overweight"),
        "dev-intl":   ( 0.0,  -3.0,  "Neutral"),
        "govt-us":    ( 0.0,   0.0,  "Underweight"),
        "intl-debt":  ( 0.0,   0.0,  "Underweight"),
        "ig-credit":  ( 0.0,   0.0,  "Neutral"),
        "hy-bonds":   (-2.5,  -7.0,  "Neutral"),
        "em-bonds":   ( 0.0,   2.0,  "Overweight"),
        "oil":        ( 2.5,   2.5,  None),
    }

    assets = []
    snap   = []

    for aid, (ndr_aw, bca_aw, bii_sig) in DEMO.items():
        ndr_score = score_from_aw(ndr_aw)
        bca_score = score_from_aw(bca_aw)
        bii_score = score_from_bii(bii_sig) if bii_sig else None

        assets.append({
            "id":          aid,
            "ndrActiveWt": ndr_aw,
            "ndrPosition":  None,
            "ndrBenchmark": None,
            "ndrScore":     ndr_score,
            "bcaActiveWt":  bca_aw,
            "bcaPosition":  None,
            "bcaBenchmark": None,
            "bcaScore":     bca_score,
            "biiSignal":    bii_sig,
            "biiScore":     bii_score,
        })
        snap.append({
            "id": aid,
            "ndrScore": ndr_score,
            "bcaScore": bca_score,
            "biiScore": bii_score,
        })

    # Generate 3 months of fake history so trend arrows show
    history = []
    for i, m_label in enumerate(["Apr 2026", "May 2026", month]):
        h_snap = []
        for aid, (ndr_aw, bca_aw, bii_sig) in DEMO.items():
            # Slight variation per month for trend illustration
            offset = (i - 2) * 0.3
            h_snap.append({
                "id":       aid,
                "ndrScore": score_from_aw(ndr_aw + offset),
                "bcaScore": score_from_aw(bca_aw + offset),
                "biiScore": score_from_bii(bii_sig) if bii_sig else None,
            })
        history.append({"month": m_label, "assets": h_snap})

    return {
        "as_of": today,
        "sources": {
            "ndr": {"status": "demo", "retrieved": today,
                    "message": "Demo mode — representative data",
                    "page": "NDR House Views — ndr.com/group/ndr"},
            "bca": {"status": "demo", "retrieved": today,
                    "message": "Demo mode — representative data",
                    "page": "BCA GIS FullView Portfolio — bcaresearch.com"},
            "bii": {"status": "demo", "retrieved": today,
                    "message": "Demo mode — representative data",
                    "page": "BlackRock BII — blackrock.com/corporate/insights/blackrock-investment-institute/publications/outlook"},
        },
        "assets":  assets,
        "history": history,
    }


if __name__ == "__main__":
    main()
