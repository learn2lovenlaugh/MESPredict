#!/usr/bin/env python3
"""
Source probe. Finds out what is actually reachable from this machine.

All probes run in parallel with an 8s timeout, so this finishes in about ten
seconds even when every source is dead. It never fails the job and never
writes anything.

    python scripts/probe.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124 Safari/537.36")
TIMEOUT = 8
FRED_KEY = os.environ.get("FRED_API_KEY", "")


def lines(r):
    return "%d lines" % len(r.text.splitlines())


def jkeys(r):
    return "keys: %s" % ", ".join(list(r.json())[:3])


def fred_obs(r):
    n = len(r.json().get("observations", []))
    return ("%d obs" % n) if n else False


def ybars(r):
    res = (r.json().get("chart") or {}).get("result") or []
    if not res:
        return False
    return "%d bars" % len(res[0].get("timestamp") or [])


TARGETS = [
    ("cboe VIX csv",
     "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
     lines),
    ("cnn fear&greed",
     "https://production.dataviz.cnn.io/index/fearandgreed/graphdata", jkeys),
    ("fred web csv",
     "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2", lines),
    ("fred web txt",
     "https://fred.stlouisfed.org/data/BAMLH0A0HYM2.txt", lines),
    ("fred API keyed",
     ("https://api.stlouisfed.org/fred/series/observations"
      "?series_id=BAMLH0A0HYM2&file_type=json&observation_start=2025-01-01"
      "&api_key=" + FRED_KEY) if FRED_KEY else None, fred_obs),
    ("yahoo query1 RSP",
     "https://query1.finance.yahoo.com/v8/finance/chart/RSP"
     "?range=1y&interval=1d", ybars),
    ("yahoo query2 RSP",
     "https://query2.finance.yahoo.com/v8/finance/chart/RSP"
     "?range=1y&interval=1d", ybars),
    ("stooq .com",
     "https://stooq.com/q/d/l/?s=rsp.us&i=d", lines),
    ("stooq .pl",
     "https://stooq.pl/q/d/l/?s=rsp.us&i=d", lines),
]


def run(target):
    label, url, check = target
    if url is None:
        return (label, None, 0.0, "-", "SKIP no FRED_API_KEY secret", "")
    t0 = time.time()
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
        el = time.time() - t0
        ok = r.status_code == 200
        note, snip = "", ""
        if ok:
            try:
                note = check(r)
                if note is False:
                    ok, note = False, "content check failed"
            except Exception as exc:  # noqa: BLE001
                ok, note = False, "parse error: %s" % str(exc)[:60]
        if not ok:
            snip = r.content[:100].decode("utf-8", "replace").replace("\n", " ")
            note = note or ("HTTP %s" % r.status_code)
        return (label, ok, el, "%dB" % len(r.content), note, snip)
    except Exception as exc:  # noqa: BLE001
        return (label, False, time.time() - t0, "-", str(exc)[:90], "")


print("=" * 76)
print("SOURCE PROBE  (parallel, %ds timeout)" % TIMEOUT)
print("=" * 76)
print("%-18s %-4s %6s %9s  %s" % ("source", "ok", "time", "size", "note"))
print("-" * 76)

t_all = time.time()
with ThreadPoolExecutor(max_workers=len(TARGETS)) as pool:
    rows = list(pool.map(run, TARGETS))

for label, ok, el, size, note, snip in rows:
    flag = "--" if ok is None else ("OK" if ok else "XX")
    print("%-18s %-4s %6.1fs %9s  %s" % (label, flag, el, size, note))
    if snip:
        print("%-18s      ^ %s" % ("", snip))

good = [r[0] for r in rows if r[1]]
bad = [r[0] for r in rows if r[1] is False]
print("-" * 76)
print("WORKING: %s" % (", ".join(good) or "NONE"))
print("BROKEN:  %s" % (", ".join(bad) or "none"))
print("total %.1fs" % (time.time() - t_all))
print("=" * 76)
sys.exit(0)
