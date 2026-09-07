#!/usr/bin/env python3
"""Offline check of the scoring path. No network. Run: python3 scripts/selftest.py"""
import json, math, os, random, sys, time
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

    # End-to-end through main() with the network mocked. Catches wiring
    # bugs (unbound names, bad returns) that scoring tests miss - CI runs
    # this before the real fetch, so a broken main() fails loudly here.
    import tempfile, random as rnd
    from datetime import date as _date, timedelta as _td

    def mk(n=400, v=15.0):
        d = _date.today() - _td(days=n); rnd.seed(3)
        return {(d + _td(days=i)).isoformat(): round(v * (1 + rnd.gauss(0, .02)), 2)
                for i in range(n)}

    saved = {k: getattr(fetch, k) for k in
             ("cboe_index", "fred_series", "fred_txt", "stooq_series",
              "yahoo_series", "cnn_fear_greed", "fetch_gex",
              "HISTORY_CSV", "GEX_CSV", "LATEST_JSON")}
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated outage"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fetch.HISTORY_CSV = os.path.join(tmp, "history.csv")
            fetch.GEX_CSV = os.path.join(tmp, "gex.csv")
            fetch.LATEST_JSON = os.path.join(tmp, "latest.json")
            fetch.cboe_index = lambda n, st: mk(v=15 if n == "VIX" else 16)
            fetch.fred_txt = lambda sid, st: mk(v=6800 if sid == "SP500"
                                                 else (3.2 if "BAML" in sid else 99.0))
            fetch.fred_series = boom
            fetch.yahoo_series = lambda sym, st: mk(v=400)
            fetch.stooq_series = boom
            fetch.cnn_fear_greed = lambda: mk(v=50)
            fetch.fetch_gex = lambda: {"spot": 6800, "net_gex_bn": 1.4,
                                       "flip": 6720, "pct_to_flip": 1.2}

            fetch.main()
            snap = json.load(open(fetch.LATEST_JSON))
            ok &= check("main() writes a complete snapshot",
                        snap["verdict"] and 0 <= snap["score"] <= 100
                        and len(snap["factors"]) == len(fetch.SCORING)
                        and len(snap["vetoes"]) == 4)

            # Now kill most sources; main() must still produce a snapshot.
            fetch.WARNINGS.clear(); fetch.TIMINGS.clear()
            fetch.fred_txt = boom; fetch.fred_series = boom
            fetch.cnn_fear_greed = boom; fetch.fetch_gex = boom
            fetch.yahoo_series = boom; fetch.stooq_series = boom
            fetch.main()
            snap2 = json.load(open(fetch.LATEST_JSON))
            ok &= check("main() survives a partial outage",
                        snap2["verdict"] and snap2["warnings"]
                        and len(snap2["factors_live"]) < len(fetch.SCORING))
            print("   degraded run kept %d/%d factors" %
                  (len(snap2["factors_live"]), len(fetch.SCORING)))

            # The exact CI failure: stooq blocked AND the generated FRED CSV
            # timing out. Static .txt and yahoo must carry the run.
            fetch.WARNINGS.clear(); fetch.TIMINGS.clear()
            fetch.fred_txt = lambda sid, st: mk(v=6800 if sid == "SP500"
                                                 else (3.2 if "BAML" in sid else 99.0))
            fetch.fred_series = boom
            fetch.cnn_fear_greed = lambda: mk(v=50)
            fetch.fetch_gex = lambda: {"spot": 6800, "net_gex_bn": 1.4,
                                       "flip": 6720, "pct_to_flip": 1.2}
            fetch.yahoo_series = lambda sym, st: mk(v=400)
            fetch.stooq_series = boom
            fetch.main()
            snap3 = json.load(open(fetch.LATEST_JSON))
            ok &= check("stooq blocked + FRED csv timeout still works",
                        snap3["verdict"] and snap3["score"] is not None)
            ok &= check("SPX comes from FRED, not stooq",
                        snap3["spx"] is not None)
            print("   CI-failure replay kept %d/%d factors" %
                  (len(snap3["factors_live"]), len(fetch.SCORING)))
    finally:
        for k, v in saved.items():
            setattr(fetch, k, v)
        fetch.WARNINGS.clear(); fetch.TIMINGS.clear()

    # A total outage must cost the budget and no more. Without this cap the
    # retry layers multiply into a 20+ minute run that the CI timeout kills.
    saved_budget, saved_session = fetch.FETCH_BUDGET_S, fetch.session
    try:
        fetch.FETCH_BUDGET_S = 6

        class _Hang(object):
            def get(self, url, timeout=30):
                time.sleep(timeout)
                raise RuntimeError("timed out")

        fetch.session = lambda: _Hang()
        fetch.fetch_gex = lambda: (_ for _ in ()).throw(RuntimeError("dead"))
        with tempfile.TemporaryDirectory() as tmp:
            fetch.HISTORY_CSV = os.path.join(tmp, "h.csv")
            fetch.WARNINGS.clear(); fetch.TIMINGS.clear()
            t0 = time.time()
            try:
                fetch.build_history()
            except Exception:
                pass
            spent = time.time() - t0
        ok &= check("total outage respects the fetch budget",
                    spent < fetch.FETCH_BUDGET_S + 6)
        print("   total outage cost %.1fs on a %ds budget" %
              (spent, fetch.FETCH_BUDGET_S))
    finally:
        fetch.FETCH_BUDGET_S = saved_budget
        fetch.session = saved_session
        for k, v in saved.items():
            setattr(fetch, k, v)
        fetch.DEADLINE = None
        fetch.WARNINGS.clear(); fetch.TIMINGS.clear()

    ok &= check("bands are valid",
                all(f[k]["band"] in ("green", "amber", "red", "unknown") for k in f))

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
