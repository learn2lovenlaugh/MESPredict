#!/usr/bin/env python3
"""
Offline FRED fetcher for the MES Weekly Market Regime dashboard.

Use this when the dashboard's "Get live data" button fails. A browser may
refuse to call the FRED API cross-origin; this script runs outside the
browser, so that restriction does not apply.

Usage:
    python3 fetch_fred.py YOUR_FRED_API_KEY

or set FRED_API_KEY in the environment and run with no arguments. The
GitHub Actions workflow uses the environment form so the key stays in
repository secrets.

Writes fred-data.json next to this script. In the dashboard, press
"Load data file" and select it.

Standard library only. No pip install.
"""

import json
import sys
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import date, timedelta

BASE = "https://api.stlouisfed.org/fred/series/observations"

# series id -> number of recent observations to pull
SERIES = {
    # daily market series - long history so the dashboard can backtest
    "VIXCLS": 2600, "VXVCLS": 2600,
    "DGS2": 2600, "DGS10": 2600, "DFII10": 2600,
    "DFEDTARU": 2600,
    "BAMLH0A0HYM2": 2600, "BAMLC0A0CM": 2600,
    "SP500": 2600,
    "NASDAQNQUS500LCE": 2600,   # equal-weight large cap, breadth proxy vs SP500
    "DTWEXBGS": 2600, "DCOILWTICO": 2600,
    # weekly / monthly
    "NFCI": 540,
    "ICSA": 540,
    "CPIAUCSL": 140, "CPILFESL": 140,
    "PAYEMS": 130, "UNRATE": 130, "CES0500000003": 140,
    "GACDISA066MSFRBPHI": 130,  # Philadelphia Fed manufacturing
    "GACDISA066MSFRBNY": 130,   # Empire State manufacturing
    "GDPNOW": 60,
}

# FRED caps S&P and Dow daily series at 10 years of history.
LOOKBACK_DAYS = 3700


def fetch(series_id, limit, key, start):
    q = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
        "observation_start": start,
    })
    req = urllib.request.Request(BASE + "?" + q, headers={"User-Agent": "mes-regime-dashboard/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))["observations"]


def main():
    key = (sys.argv[1].strip() if len(sys.argv) > 1 else "") or os.environ.get("FRED_API_KEY", "")
    key = key.strip()
    if not key:
        print("No API key. Pass it as an argument or set FRED_API_KEY.")
        return 2

    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fred-data.json")

    start = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    out = {}
    failed = []

    for sid, limit in SERIES.items():
        try:
            obs = fetch(sid, limit, key, start)
            out[sid] = [{"date": o["date"], "value": o["value"]} for o in obs]
            print("  ok    %-14s %d obs" % (sid, len(obs)))
        except urllib.error.HTTPError as e:
            msg = "HTTP %s" % e.code
            if e.code == 400:
                msg += " - FRED rejected the request, usually a bad API key"
            elif e.code == 429:
                msg += " - rate limited"
            failed.append((sid, msg))
            print("  FAIL  %-14s %s" % (sid, msg))
        except Exception as e:
            failed.append((sid, str(e)))
            print("  FAIL  %-14s %s" % (sid, e))

    if not out:
        print("\nNothing fetched. Check the API key and your connection.")
        return 1

    path = out_path
    payload = {
        "fetched": __import__("datetime").datetime.now().astimezone().isoformat(),
        "series": out,
    }
    with open(path, "w") as f:
        json.dump(payload, f)

    print("\nWrote %s (%d series, %d failed)" % (path, len(out), len(failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
