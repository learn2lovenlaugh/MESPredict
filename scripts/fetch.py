#!/usr/bin/env python3
"""
MES risk board - data fetcher.

Runs server-side in GitHub Actions (no CORS limits, no keys in client code).
Rebuilds data/history.csv from full-history sources each run, appends the
point-in-time GEX reading to data/gex_history.csv, then writes the scored
snapshot to data/latest.json for the static page to read.

Horizon assumption: 1-5 day swing. Low VIX reads green, not yellow.
Edit SCORING below to retune - no other file needs to change.
"""

import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HISTORY_CSV = os.path.join(DATA, "history.csv")
GEX_CSV = os.path.join(DATA, "gex_history.csv")
LATEST_JSON = os.path.join(DATA, "latest.json")
EVENTS_JSON = os.path.join(DATA, "events.json")

YEARS_HISTORY = 4
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# requests.Session is not thread-safe; give each worker its own.
_LOCAL = threading.local()


def session():
    s = getattr(_LOCAL, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "*/*",
                          "Accept-Encoding": "gzip, deflate"})
        _LOCAL.s = s
    return s


WARNINGS = []
TIMINGS = {}


def warn(msg):
    WARNINGS.append(msg)
    print("WARN: " + msg, file=sys.stderr)


def get(url, tries=2, timeout=20):
    """One retry, tight timeout. A dead source must not cost minutes."""
    last = None
    for i in range(tries):
        try:
            r = session().get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i + 1 < tries:
                time.sleep(1.5)
    raise RuntimeError("GET failed %s: %s" % (url, last))


# ---------------------------------------------------------------- sources

def fred_series(series_id, start):
    """FRED public CSV. No API key needed for the fredgraph endpoint."""
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
           "?id=%s&cosd=%s" % (series_id, start.isoformat()))
    rows = list(csv.DictReader(io.StringIO(get(url).text)))
    out = {}
    if not rows:
        return out
    cols = list(rows[0].keys())
    date_col = cols[0]
    val_col = cols[1] if len(cols) > 1 else None
    for row in rows:
        raw = (row.get(val_col) or "").strip()
        if raw in ("", ".", "NA"):
            continue
        try:
            out[row[date_col].strip()] = float(raw)
        except ValueError:
            continue
    return out


def cboe_index(name, start):
    """CBOE daily index history CSV (VIX, VIX3M, VIX9D)."""
    url = ("https://cdn.cboe.com/api/global/us_indices/daily_prices/"
           "%s_History.csv" % name)
    rows = list(csv.DictReader(io.StringIO(get(url).text)))
    out = {}
    for row in rows:
        keys = {k.strip().upper(): v for k, v in row.items() if k}
        raw_date = keys.get("DATE")
        raw_close = keys.get("CLOSE")
        if not raw_date or not raw_close:
            continue
        iso = normalize_date(raw_date)
        if not iso or iso < start.isoformat():
            continue
        try:
            out[iso] = float(raw_close)
        except ValueError:
            continue
    return out


def stooq_series(symbol, start):
    """Stooq daily OHLC. Free, no key, generous with history."""
    url = "https://stooq.com/q/d/l/?s=%s&i=d" % symbol
    text = get(url).text
    if "Date" not in text.split("\n")[0]:
        raise RuntimeError("stooq returned no header for %s" % symbol)
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        iso = normalize_date(row.get("Date", ""))
        raw = (row.get("Close") or "").strip()
        if not iso or iso < start.isoformat() or raw in ("", "N/D"):
            continue
        try:
            out[iso] = float(raw)
        except ValueError:
            continue
    return out


def cnn_fear_greed():
    """CNN Fear & Greed. Undocumented endpoint - treat as best effort."""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    payload = get(url).json()
    hist = {}
    block = (payload.get("fear_and_greed_historical") or {}).get("data") or []
    for point in block:
        try:
            ts = float(point["x"]) / 1000.0
            iso = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            hist[iso] = float(point["y"])
        except (KeyError, TypeError, ValueError):
            continue
    current = (payload.get("fear_and_greed") or {}).get("score")
    if current is not None:
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            hist.setdefault(today, float(current))
        except (TypeError, ValueError):
            pass
    return hist


def normalize_date(raw):
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------- GEX

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(spot, strike, t_years, iv, rate=0.04):
    """Black-Scholes gamma. Same for calls and puts."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return 0.0
    vol_t = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / vol_t
    return norm_pdf(d1) / (spot * vol_t)


OPT_RE = re.compile(r"^([A-Z^_]+)(\d{6})([CP])(\d{8})$")


def parse_option_symbol(sym):
    m = OPT_RE.match(sym.strip())
    if not m:
        return None
    _root, yymmdd, cp, strike_raw = m.groups()
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return expiry, cp, int(strike_raw) / 1000.0


def fetch_gex():
    """
    Crude dealer gamma exposure from CBOE delayed SPX chains.

    Convention: dealers assumed long calls / short puts against customer flow.
    This is the common naive assumption. It is an assumption, not a fact -
    treat sign and trend as signal, absolute magnitude as indicative only.
    """
    url = "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"
    payload = get(url, timeout=90).json()
    data = payload.get("data") or {}
    spot = data.get("current_price") or data.get("close")
    options = data.get("options") or []
    if not spot or not options:
        raise RuntimeError("CBOE returned no SPX chain")
    spot = float(spot)

    today = date.today()
    contracts = []
    for opt in options:
        parsed = parse_option_symbol(opt.get("option", ""))
        if not parsed:
            continue
        expiry, cp, strike = parsed
        dte = (expiry - today).days
        if dte < 0 or dte > 90:
            continue
        if abs(strike / spot - 1.0) > 0.15:
            continue
        oi = opt.get("open_interest") or 0
        iv = opt.get("iv") or 0
        if oi <= 0 or iv <= 0:
            continue
        contracts.append({
            "cp": cp,
            "k": strike,
            "t": max(dte, 1) / 365.0,
            "iv": float(iv),
            "oi": float(oi),
        })

    if not contracts:
        raise RuntimeError("no usable SPX contracts after filtering")

    def net_gex_at(s):
        total = 0.0
        for c in contracts:
            g = bs_gamma(s, c["k"], c["t"], c["iv"])
            signed = g * c["oi"] * (1.0 if c["cp"] == "C" else -1.0)
            total += signed
        # notional gamma per 1% move, in USD
        return total * 100.0 * s * s * 0.01

    net = net_gex_at(spot)

    # Walk spot to find the zero crossing (gamma flip level).
    flip = None
    lo, hi = spot * 0.90, spot * 1.10
    steps = 60
    prev_s, prev_v = None, None
    for i in range(steps + 1):
        s = lo + (hi - lo) * i / steps
        v = net_gex_at(s)
        if prev_v is not None and (prev_v < 0) != (v < 0):
            frac = abs(prev_v) / (abs(prev_v) + abs(v)) if (prev_v or v) else 0.5
            flip = prev_s + (s - prev_s) * frac
            if flip <= spot:  # take the crossing nearest below/at spot
                break
        prev_s, prev_v = s, v

    return {
        "spot": round(spot, 2),
        "net_gex_bn": round(net / 1e9, 3),
        "flip": round(flip, 2) if flip else None,
        "pct_to_flip": round((spot / flip - 1.0) * 100.0, 2) if flip else None,
        "contracts_used": len(contracts),
    }


# --------------------------------------------------------------- history

def build_history():
    start = date.today() - timedelta(days=int(365.25 * YEARS_HISTORY))
    jobs = {
        "vix":    lambda: cboe_index("VIX", start),
        "vix3m":  lambda: cboe_index("VIX3M", start),
        "hy_oas": lambda: fred_series("BAMLH0A0HYM2", start),
        "dollar": lambda: fred_series("DTWEXBGS", start),
        "wti":    lambda: fred_series("DCOILWTICO", start),
        "spx":    lambda: stooq_series("^spx", start),
        "rsp":    lambda: stooq_series("rsp.us", start),
        "spy":    lambda: stooq_series("spy.us", start),
        "fng":    cnn_fear_greed,
        "_gex":   fetch_gex,
    }

    def run(item):
        key, fn = item
        t0 = time.time()
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001
            warn("%s failed: %s" % (key, exc))
            out = {}
        TIMINGS[key] = round(time.time() - t0, 1)
        return key, out

    # Independent HTTP fetches: total is the slowest source, not the sum.
    with ThreadPoolExecutor(max_workers=10) as pool:
        series = dict(pool.map(run, jobs.items()))

    gex = series.pop("_gex") or None
    for key in sorted(TIMINGS, key=lambda k: -TIMINGS[key]):
        n = len(series[key]) if key in series else "-"
        print("%6.1fs  %-8s %s rows" % (TIMINGS[key], key, n))

    all_dates = sorted(set().union(*[set(s.keys()) for s in series.values()]) or [])
    all_dates = [d for d in all_dates if d >= start.isoformat()]

    cols = ["date", "vix", "vix3m", "hy_oas", "dollar", "wti", "spx",
            "rsp", "spy", "fng"]
    rows = []
    carry = {}
    for d in all_dates:
        row = {"date": d}
        for key in cols[1:]:
            val = series.get(key, {}).get(d)
            if val is None:
                val = carry.get(key)  # forward fill; holidays differ by source
            else:
                carry[key] = val
            row[key] = val
        rows.append(row)

    # Drop the warm-up window where forward fill has nothing to carry.
    rows = [r for r in rows if r.get("vix") is not None and r.get("spx") is not None]

    with open(HISTORY_CSV, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row[k] is None else row[k]) for k in cols})
    print("wrote %s (%d rows)" % (HISTORY_CSV, len(rows)))
    return rows, gex


def append_gex(gex):
    exists = os.path.exists(GEX_CSV)
    cols = ["date", "spot", "net_gex_bn", "flip", "pct_to_flip"]
    today = date.today().isoformat()
    existing = []
    if exists:
        with open(GEX_CSV) as fh:
            existing = [r for r in csv.DictReader(fh) if r.get("date") != today]
    with open(GEX_CSV, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for row in existing:
            writer.writerow({k: row.get(k, "") for k in cols})
        if gex:
            writer.writerow({
                "date": today,
                "spot": gex["spot"],
                "net_gex_bn": gex["net_gex_bn"],
                "flip": gex["flip"] or "",
                "pct_to_flip": gex["pct_to_flip"] if gex["pct_to_flip"] is not None else "",
            })
    return existing


# --------------------------------------------------------------- scoring

def pct_rank(series, value):
    """Percentile rank of value within series, 0-100."""
    vals = [v for v in series if v is not None]
    if not vals or value is None:
        return None
    below = sum(1 for v in vals if v < value)
    return round(100.0 * below / len(vals), 1)


def change_over(rows, col, lookback):
    vals = [r[col] for r in rows if r[col] is not None]
    if len(vals) <= lookback:
        return None
    return vals[-1] - vals[-1 - lookback]


def pct_change_over(rows, col, lookback):
    vals = [r[col] for r in rows if r[col] is not None]
    if len(vals) <= lookback or not vals[-1 - lookback]:
        return None
    return 100.0 * (vals[-1] / vals[-1 - lookback] - 1.0)


def band(value, green_max, amber_max, invert=False):
    """Map a value to green/amber/red. invert=True means lower is worse."""
    if value is None:
        return "unknown"
    if invert:
        if value >= green_max:
            return "green"
        if value >= amber_max:
            return "amber"
        return "red"
    if value <= green_max:
        return "green"
    if value <= amber_max:
        return "amber"
    return "red"


# Weights sum to 100. Tuned for a 1-5 day swing horizon, hand-set from
# theory rather than fitted - there is not enough independent sample to
# fit eight weights without overfitting. Retune here after you have logged
# a few hundred readings against outcomes.
SCORING = {
    "vix_term":   {"w": 22, "label": "VIX term structure"},
    "gex":        {"w": 18, "label": "Net dealer gamma"},
    "hy_oas":     {"w": 15, "label": "Credit spreads"},
    "vix_level":  {"w": 12, "label": "VIX level"},
    "fng":        {"w": 10, "label": "Fear & Greed"},
    "breadth":    {"w": 8,  "label": "Breadth (RSP/SPY)"},
    "dollar":     {"w": 8,  "label": "Dollar momentum"},
    "oil":        {"w": 7,  "label": "Oil momentum"},
}


def unit(x, lo, hi):
    """Clamp x into 0..1 across [lo, hi]."""
    if x is None:
        return None
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score_all(rows, gex, events):
    last = rows[-1]
    hist = {c: [r[c] for r in rows] for c in rows[0]}
    out = {}

    # --- VIX term structure: VIX / VIX3M. Backwardation is the stress flag.
    term = None
    if last["vix"] and last["vix3m"]:
        term = last["vix"] / last["vix3m"]
    out["vix_term"] = {
        "value": round(term, 4) if term else None,
        "display": ("%.3f" % term) if term else "n/a",
        "note": "backwardated" if term and term > 1.0 else "contango",
        "band": band(term, 0.92, 1.00),
        "risk": unit(term, 0.85, 1.08),
        "zones": [0.80, 0.92, 1.00, 1.15], "green_side": "low",
    }

    # --- Net GEX. Negative = dealers short gamma = moves get amplified.
    g = gex or {}
    net = g.get("net_gex_bn")
    pct_flip = g.get("pct_to_flip")
    gex_risk = None
    if net is not None:
        # -3bn to +8bn covers the usual SPX range
        gex_risk = 1.0 - unit(net, -3.0, 8.0)
    out["gex"] = {
        "value": net,
        "display": ("%+.2f bn" % net) if net is not None else "n/a",
        "note": (("%.1f%% above flip %s" % (pct_flip, g.get("flip")))
                 if pct_flip is not None and pct_flip >= 0 else
                 (("%.1f%% below flip %s" % (pct_flip, g.get("flip")))
                  if pct_flip is not None else "flip not found")),
        "band": band(net, 2.0, 0.0, invert=True) if net is not None else "unknown",
        "risk": gex_risk,
        "zones": [-4.0, 0.0, 2.0, 8.0], "green_side": "high",
    }

    # --- Credit spreads: 20-day change in HY OAS, in bp.
    oas_chg = change_over(rows, "hy_oas", 20)
    oas_bp = round(oas_chg * 100, 1) if oas_chg is not None else None
    out["hy_oas"] = {
        "value": oas_bp,
        "display": ("%+.0f bp / 20d" % oas_bp) if oas_bp is not None else "n/a",
        "note": ("level %.2f%%" % last["hy_oas"]) if last["hy_oas"] else "",
        "band": band(oas_bp, 15, 50),
        "risk": unit(oas_bp, -30, 80),
        "zones": [-40, 15, 50, 100], "green_side": "low",
    }

    # --- VIX level, percentile ranked. For a 1-5 day swing, low is fine.
    vix_pctile = pct_rank(hist["vix"], last["vix"])
    out["vix_level"] = {
        "value": last["vix"],
        "display": ("%.2f" % last["vix"]) if last["vix"] else "n/a",
        "note": ("%.0fth pct, 4y" % vix_pctile) if vix_pctile is not None else "",
        "band": band(vix_pctile, 60, 85),
        "risk": unit(vix_pctile, 10, 95),
        "zones": [0, 60, 85, 100], "green_side": "low",
    }

    # --- Fear & Greed, nonlinear. Only the tails carry information, and
    #     extreme fear is a contrarian long flag, not a risk flag.
    fng = last["fng"]
    fng_risk, fng_note = None, ""
    if fng is not None:
        if fng >= 80:
            fng_risk, fng_note = 0.9, "crowded greed"
        elif fng >= 65:
            fng_risk, fng_note = 0.55, "greed"
        elif fng >= 20:
            fng_risk, fng_note = 0.2, "neutral zone, low information"
        else:
            fng_risk, fng_note = 0.35, "extreme fear - contrarian long flag"
    out["fng"] = {
        "value": fng,
        "display": ("%.0f" % fng) if fng is not None else "n/a",
        "note": fng_note,
        "band": ("red" if fng is not None and fng >= 80
                 else "amber" if fng is not None and (fng >= 65 or fng < 20)
                 else "green" if fng is not None else "unknown"),
        "risk": fng_risk,
        "zones": [0, 20, 65, 100], "green_side": "middle",
        "contrarian": bool(fng is not None and fng < 20),
    }

    # --- Breadth: equal-weight vs cap-weight, 60-day change.
    ratio = [(r["rsp"] / r["spy"]) if (r["rsp"] and r["spy"]) else None
             for r in rows]
    ratio_rows = [{"r": v} for v in ratio]
    br = pct_change_over(ratio_rows, "r", 60)
    out["breadth"] = {
        "value": round(br, 2) if br is not None else None,
        "display": ("%+.2f%% / 60d" % br) if br is not None else "n/a",
        "note": "equal-weight vs cap-weight",
        "band": band(br, -1.0, -4.0, invert=True),
        "risk": 1.0 - (unit(br, -8.0, 3.0) or 0.0) if br is not None else None,
        "zones": [-8, -4, -1, 3], "green_side": "high",
    }

    # --- Dollar: 20-day rate of change. Level tells you nothing.
    dxy_chg = pct_change_over(rows, "dollar", 20)
    out["dollar"] = {
        "value": round(dxy_chg, 2) if dxy_chg is not None else None,
        "display": ("%+.2f%% / 20d" % dxy_chg) if dxy_chg is not None else "n/a",
        "note": ("index %.2f" % last["dollar"]) if last["dollar"] else "",
        "band": band(abs(dxy_chg) if dxy_chg is not None else None, 1.0, 2.5),
        "risk": unit(abs(dxy_chg) if dxy_chg is not None else None, 0.0, 3.5),
        "zones": [0, 1.0, 2.5, 4.0], "green_side": "low",
        "absolute": True,
    }

    # --- Oil: 20-day rate of change, magnitude.
    oil_chg = pct_change_over(rows, "wti", 20)
    out["oil"] = {
        "value": round(oil_chg, 2) if oil_chg is not None else None,
        "display": ("%+.2f%% / 20d" % oil_chg) if oil_chg is not None else "n/a",
        "note": ("WTI %.2f" % last["wti"]) if last["wti"] else "",
        "band": band(abs(oil_chg) if oil_chg is not None else None, 8.0, 15.0),
        "risk": unit(abs(oil_chg) if oil_chg is not None else None, 0.0, 20.0),
        "zones": [0, 8, 15, 25], "green_side": "low",
        "absolute": True,
    }

    for key, meta in SCORING.items():
        out[key]["weight"] = meta["w"]
        out[key]["label"] = meta["label"]
        r = out[key].get("risk")
        out[key]["points"] = round(meta["w"] * r, 1) if r is not None else None

    # Renormalise over factors that actually returned data.
    live = [k for k in SCORING if out[k].get("risk") is not None]
    wsum = sum(SCORING[k]["w"] for k in live) or 1
    total = sum(out[k]["points"] for k in live) * (100.0 / wsum)

    return out, round(total, 1), live


def build_vetoes(factors, events, gex):
    """Hard stops. Any one red means stand down regardless of the score."""
    vetoes = []

    term = factors["vix_term"]["value"]
    vetoes.append({
        "id": "term",
        "label": "VIX term structure",
        "detail": factors["vix_term"]["display"],
        "tripped": bool(term and term > 1.00),
        "why": "Backwardation. Front-month fear above 3-month - the market is "
               "pricing near-term stress, and systematic de-risking follows.",
    })

    oas = factors["hy_oas"]["value"]
    vetoes.append({
        "id": "credit",
        "label": "Credit spreads",
        "detail": factors["hy_oas"]["display"],
        "tripped": bool(oas is not None and oas > 50),
        "why": "High yield spreads widening more than 50bp in a month. Credit "
               "leads equity at turns.",
    })

    pct_flip = (gex or {}).get("pct_to_flip")
    vetoes.append({
        "id": "gamma",
        "label": "Dealer gamma",
        "detail": factors["gex"]["display"],
        "tripped": bool(pct_flip is not None and pct_flip < -1.0),
        "why": "Spot more than 1% below the flip level. Dealers are short "
               "gamma and hedging flows amplify moves in both directions.",
    })

    nxt = next_event(events)
    hours = nxt["hours"] if nxt else None
    vetoes.append({
        "id": "event",
        "label": "Event window",
        "detail": (("%s in %s" % (nxt["name"], humanize(hours)))
                   if nxt else "nothing scheduled"),
        "tripped": bool(nxt and nxt["tier"] == 1 and hours is not None and hours <= 48),
        "why": "A tier-1 macro release inside 48 hours. Position sizing before "
               "these is a coin flip on the print, not on your edge.",
    })

    return vetoes, nxt


def humanize(hours):
    if hours is None:
        return "n/a"
    if hours < 24:
        return "%dh" % int(hours)
    return "%dd" % round(hours / 24.0)


def next_event(events):
    now = datetime.now(timezone.utc)
    upcoming = []
    for ev in events:
        try:
            when = datetime.fromisoformat(ev["utc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if when < now:
            continue
        upcoming.append({
            "name": ev.get("name", "event"),
            "tier": int(ev.get("tier", 2)),
            "utc": ev["utc"],
            "hours": (when - now).total_seconds() / 3600.0,
        })
    upcoming.sort(key=lambda e: e["hours"])
    tier1 = [e for e in upcoming if e["tier"] == 1]
    return (tier1[0] if tier1 and tier1[0]["hours"] <= 120
            else (upcoming[0] if upcoming else None))


def main():
    t_start = time.time()
    rows, gex = build_history()
    if not rows:
        raise SystemExit("no history rows - every source failed")
    gex_hist = append_gex(gex)

    events = []
    if os.path.exists(EVENTS_JSON):
        with open(EVENTS_JSON) as fh:
            events = json.load(fh).get("events", [])

    factors, score, live = score_all(rows, gex, events)
    vetoes, nxt = build_vetoes(factors, events, gex)

    tripped = [v for v in vetoes if v["tripped"]]
    if tripped:
        verdict, size = "stand down", 0
    elif score >= 60:
        verdict, size = "high risk", 25
    elif score >= 35:
        verdict, size = "reduced", 60
    else:
        verdict, size = "clear", 100

    snapshot = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": rows[-1]["date"],
        "horizon": "1-5 day swing",
        "verdict": verdict,
        "size_pct": size,
        "score": score,
        "factors": factors,
        "vetoes": vetoes,
        "next_event": nxt,
        "gex": gex,
        "gex_samples": len(gex_hist) + (1 if gex else 0),
        "factors_live": live,
        "factors_missing": [k for k in SCORING if k not in live],
        "warnings": WARNINGS,
        "timings_sec": TIMINGS,
        "spx": rows[-1]["spx"],
    }

    with open(LATEST_JSON, "w") as fh:
        json.dump(snapshot, fh, indent=2)
    print("wrote %s -> %s (%s), score %.1f" %
          (LATEST_JSON, verdict, "%d%%" % size, score))
    print("total %.1fs" % (time.time() - t_start))


if __name__ == "__main__":
    main()
