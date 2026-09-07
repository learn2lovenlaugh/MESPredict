#!/usr/bin/env python3
"""
Source probe. Run this in Actions to find out what is actually reachable
from a GitHub runner, instead of guessing.

Tests each candidate independently with a short timeout, prints status,
latency, size and a content sanity check. Never fails the job - the whole
point is to see every result in one run.

    python scripts/probe.py
"""

import json
import os
import sys
import time

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124 Safari/537.36")
TIMEOUT = 20

FRED_KEY = os.environ.get("FRED_API_KEY", "")


def probe(label, url, check=None, headers=None):
    """Fetch one URL, report what happened. Returns True if usable."""
    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         headers=dict({"User-Agent": UA}, **(headers or {})))
        el = time.time() - t0
        body = r.content
        note = ""
        good = r.status_code == 200
        if good and check:
            try:
                note = check(r)
                if note is False:
                    good, note = False, "content check failed"
            except Exception as exc:  # noqa: BLE001
                good, note = False, "parse error: %s" % exc
        print("%-22s %-6s %6.1fs %9s  %s" %
              (label, r.status_code, el, "%dB" % len(body), note))
        if not good and body:
            snippet = body[:120].decode("utf-8", "replace").replace("\n", " ")
            print("%-22s        ^ %s" % ("", snippet))
        return good
    except Exception as exc:  # noqa: BLE001
        print("%-22s %-6s %6.1fs %9s  %s" %
              (label, "ERR", time.time() - t0, "-", exc))
        return False


def lines(r):
    return "%d lines" % len(r.text.splitlines())


def json_keys(r):
    d = r.json()
    return "keys: %s" % ", ".join(list(d)[:4])


def fred_obs(r):
    d = r.json()
    n = len(d.get("observations", []))
    return ("%d observations" % n) if n else False


def yahoo_chart(r):
    d = r.json()
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        return False
    return "%d bars" % len(res[0].get("timestamp") or [])


print("=" * 78)
print("SOURCE PROBE  -  what is reachable from this runner")
print("=" * 78)
print("%-22s %-6s %6s %9s  %s" % ("source", "status", "time", "size", "note"))
print("-" * 78)

results = {}

# --- known good on the last run, included as a control
results["cboe_vix"] = probe(
    "cboe VIX csv",
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    lines)
results["cnn_fng"] = probe(
    "cnn fear&greed",
    "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
    json_keys)

# --- FRED: three different hosts/endpoints
results["fred_web_csv"] = probe(
    "fred web csv",
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2", lines)
results["fred_web_txt"] = probe(
    "fred web txt",
    "https://fred.stlouisfed.org/data/BAMLH0A0HYM2.txt", lines)
if FRED_KEY:
    results["fred_api"] = probe(
        "fred API (keyed)",
        "https://api.stlouisfed.org/fred/series/observations"
        "?series_id=BAMLH0A0HYM2&file_type=json&observation_start=2024-01-01"
        "&api_key=" + FRED_KEY, fred_obs)
else:
    print("%-22s %-6s %6s %9s  %s" %
          ("fred API (keyed)", "SKIP", "-", "-",
           "no FRED_API_KEY secret set"))
    results["fred_api"] = None

# --- ETF / index quotes
results["yahoo_chart"] = probe(
    "yahoo chart RSP",
    "https://query1.finance.yahoo.com/v8/finance/chart/RSP"
    "?range=1y&interval=1d", yahoo_chart)
results["yahoo_alt_host"] = probe(
    "yahoo query2 RSP",
    "https://query2.finance.yahoo.com/v8/finance/chart/RSP"
    "?range=1y&interval=1d", yahoo_chart)
results["stooq"] = probe(
    "stooq rsp.us",
    "https://stooq.com/q/d/l/?s=rsp.us&i=d", lines)
results["stooq_pl"] = probe(
    "stooq.pl rsp.us",
    "https://stooq.pl/q/d/l/?s=rsp.us&i=d", lines)

# --- CBOE options chain (GEX input) - large, timed separately
t0 = time.time()
try:
    r = requests.get(
        "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
        timeout=90, headers={"User-Agent": UA})
    n = len(((r.json().get("data") or {}).get("options") or []))
    print("%-22s %-6s %6.1fs %9s  %d contracts" %
          ("cboe SPX chain", r.status_code, time.time() - t0,
           "%.1fMB" % (len(r.content) / 1e6), n))
    results["cboe_chain"] = r.status_code == 200 and n > 0
except Exception as exc:  # noqa: BLE001
    print("%-22s %-6s %6.1fs %9s  %s" %
          ("cboe SPX chain", "ERR", time.time() - t0, "-", exc))
    results["cboe_chain"] = False

print("-" * 78)
working = [k for k, v in results.items() if v]
broken = [k for k, v in results.items() if v is False]
print("WORKING: %s" % (", ".join(working) or "none"))
print("BROKEN:  %s" % (", ".join(broken) or "none"))
print("=" * 78)
print("\nPaste this whole block back. Sources get chosen from these results,")
print("not from assumptions about what should work.")
sys.exit(0)
