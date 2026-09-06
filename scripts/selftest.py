#!/usr/bin/env python3
"""Offline check of the scoring path. No network. Run: python3 scripts/selftest.py"""
import json, math, os, random, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch  # noqa: E402


def synth(days=600, regime="calm"):
    rows, d = [], date.today() - timedelta(days=days)
    vix, oas, dol, wti, spx, rsp, spy, fng = 15.0, 3.2, 99.0, 70.0, 6800.0, 180.0, 620.0, 50.0
    random.seed(7)
    for i in range(days):
        drift = 1.0 if regime == "calm" else 1.0 + i / days
        vix = max(9.0, vix + random.gauss(0, 0.6) * drift)
        oas = max(2.5, oas + random.gauss(0, 0.03) * drift)
        dol += random.gauss(0, 0.25)
        wti += random.gauss(0, 0.8)
        spx *= 1 + random.gauss(0.0004, 0.008)
        spy *= 1 + random.gauss(0.0004, 0.008)
        rsp *= 1 + random.gauss(0.0001, 0.008)
        fng = min(95, max(5, fng + random.gauss(0, 3)))
        vix3m = vix * (1.06 if regime == "calm" else 0.97)
        rows.append({"date": (d + timedelta(days=i)).isoformat(), "vix": round(vix, 2),
                     "vix3m": round(vix3m, 2), "hy_oas": round(oas, 2),
                     "dollar": round(dol, 2), "wti": round(wti, 2), "spx": round(spx, 2),
                     "rsp": round(rsp, 2), "spy": round(spy, 2), "fng": round(fng)})
    return rows


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


def main():
    ok = True

    # Black-Scholes gamma sanity: peaks near the money, positive, decays in the wings.
    atm = fetch.bs_gamma(6800, 6800, 30 / 365, 0.15)
    otm = fetch.bs_gamma(6800, 7500, 30 / 365, 0.15)
    ok &= check("gamma positive and peaks ATM", atm > otm > 0)
    ok &= check("gamma rises as expiry nears",
                fetch.bs_gamma(6800, 6800, 5 / 365, 0.15) > atm)

    # Option symbol parsing
    p = fetch.parse_option_symbol("SPX260918C05000000")
    ok &= check("symbol parse", p and p[1] == "C" and abs(p[2] - 5000.0) < 1e-9)
    ok &= check("bad symbol rejected", fetch.parse_option_symbol("garbage") is None)

    # Percentile + change helpers
    ok &= check("pct_rank midpoint", fetch.pct_rank([1, 2, 3, 4], 3) == 50.0)
    ok &= check("unit clamps", fetch.unit(99, 0, 10) == 1.0 and fetch.unit(-5, 0, 10) == 0.0)

    events = json.load(open(os.path.join(fetch.DATA, "events.json")))["events"]

    # Calm regime, healthy gamma -> should not stand down.
    calm = synth(regime="calm")
    gex_ok = {"spot": 6800, "net_gex_bn": 4.5, "flip": 6600, "pct_to_flip": 3.0}
    f, s, live = fetch.score_all(calm, gex_ok, events)
    v, nxt = fetch.build_vetoes(f, events, gex_ok)
    ok &= check("calm regime scores low", s < 55)
    ok &= check("calm regime trips no market veto",
                not any(x["tripped"] for x in v if x["id"] in ("term", "credit", "gamma")))
    print("   calm score %.1f, factors live %d/%d" % (s, len(live), len(fetch.SCORING)))

    # Stressed regime, short gamma -> vetoes should fire.
    stress = synth(regime="stress")
    gex_bad = {"spot": 6800, "net_gex_bn": -2.2, "flip": 6950, "pct_to_flip": -2.2}
    f2, s2, _ = fetch.score_all(stress, gex_bad, events)
    v2, _ = fetch.build_vetoes(f2, events, gex_bad)
    ok &= check("stress regime scores higher than calm", s2 > s)
    ok &= check("backwardation trips term veto",
                any(x["tripped"] for x in v2 if x["id"] == "term"))
    ok &= check("short gamma trips gamma veto",
                any(x["tripped"] for x in v2 if x["id"] == "gamma"))
    print("   stress score %.1f" % s2)

    # Missing data must degrade, not crash.
    holed = [dict(r) for r in calm]
    for r in holed:
        r["fng"] = None
        r["wti"] = None
    f3, s3, live3 = fetch.score_all(holed, None, events)
    ok &= check("missing factors degrade gracefully", 0 <= s3 <= 100)
    ok &= check("missing factors excluded from live set",
                "fng" not in live3 and "oil" not in live3 and "gex" not in live3)
    print("   holed score %.1f, live %s" % (s3, live3))

    # Every factor must expose the keys the page reads.
    need = {"value", "display", "note", "band", "risk", "zones", "green_side", "weight", "label", "points"}
    missing = {k: sorted(need - set(f[k])) for k in f if need - set(f[k])}
    ok &= check("all factors expose page contract", not missing)
    if missing:
        print("   missing:", missing)

    ok &= check("bands are valid",
                all(f[k]["band"] in ("green", "amber", "red", "unknown") for k in f))

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
