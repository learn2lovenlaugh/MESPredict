# MES risk board

Pre-open risk gauge for a 1 to 5 day MES swing. GitHub Actions fetches the
data server-side on a weekday cron and commits it; GitHub Pages serves a
static page that reads its own JSON. No API keys in client code, no CORS
workarounds, no server to run.

## Setup

1. Push this repo to GitHub (public, so Pages and Actions are free).
2. Settings > Pages > Source: **Deploy from a branch**, branch `main`, folder `/`.
3. Settings > Actions > General > Workflow permissions: **Read and write**.
4. Actions tab > Refresh data > **Run workflow**. First run backfills four
   years of history and takes about a minute.
5. Open `https://<you>.github.io/<repo>/`.

## What it does

`scripts/fetch.py` rebuilds `data/history.csv` from full-history sources on
every run, so the file is idempotent and self-healing. Dealer gamma is
point-in-time only and appends to `data/gex_history.csv`.

| Factor | Weight | Source |
|---|---|---|
| VIX term structure (VIX/VIX3M) | 22 | CBOE index history CSV |
| Net dealer gamma | 18 | CBOE delayed SPX chains, computed here |
| Credit spreads (HY OAS, 20d change) | 15 | FRED `BAMLH0A0HYM2` |
| VIX level (4y percentile) | 12 | CBOE |
| Fear & Greed | 10 | CNN dataviz endpoint |
| Breadth (RSP/SPY, 60d) | 8 | Yahoo chart API, stooq fallback |
| Dollar momentum (20d) | 8 | FRED `DTWEXBGS` |
| Oil momentum (20d) | 7 | FRED `DCOILWTICO` |

Two views. **Vetoes and size** treats four conditions as hard stops -- any
one red is stand down regardless of everything else -- and weights the rest
into a size multiplier. **Combined score** folds everything into one 0-100
number with no hard stops, for comparison.

## Things that will break

**`data/events.json` is hand-maintained.** It has dates through October 2026.
When it runs dry the event veto silently stops firing, which is the worst
kind of failure because the page still looks healthy. Refill it quarterly
from the Fed and BLS release calendars.

**The CNN Fear & Greed endpoint is undocumented.** If CNN changes it, that
factor drops out, the score renormalises over the rest, and the page shows a
warning. Nothing else breaks.

**Fetches are grouped by host on purpose.** FRED throttles concurrent
connections from a single IP, and firing all ten requests at once made every
FRED call time out on CI while CBOE answered in under a second. Requests are
now sequential within a host and parallel across hosts. If you add a source,
put it in the right group in `build_history()` rather than making a fifth
one-off thread.

**Stooq blocks datacenter IPs**, which includes GitHub Actions runners. It is
kept only as a fallback behind Yahoo. Do not promote it back to primary.

**FRED is read from the static `/data/<id>.txt` dump**, not `fredgraph.csv`.
The CSV endpoint renders on demand and times out under load; the txt file is
pre-generated. The CSV remains as a fallback.

**If a run is slow, read the timings.** `fetch.py` prints elapsed seconds per
source, slowest first, and stores them in `latest.json` under `timings_sec`.
All ten fetches run concurrently, so total wall time is the slowest source,
not the sum. The usual offender is the CBOE SPX chain (tens of MB) or Stooq
throttling. A healthy run is well under two minutes; the job is capped at 12.

**Scheduled workflows get disabled after ~60 days of repo inactivity**, and
commits made by the workflow's own token may not reset that clock. If the
board goes stale, re-enable it in the Actions tab.

**Dealer gamma assumes dealers are long calls and short puts.** That is the
common convention, not a measured fact. Read the sign and the trend; treat
the absolute size as indicative. It also uses delayed CBOE data, so it is a
prior-close reading, not live.

## Retuning

Weights live in `SCORING` in `scripts/fetch.py`. Band edges live in the
`zones` arrays in `score_all()`. Nothing in `index.html` needs to change --
the page renders whatever factors the JSON contains.

The weights are hand-set from theory. They are not fitted, because eight
weights cannot be fitted honestly on the sample available. Before trusting
the number, log readings against a hard outcome label -- something like
*max adverse excursion over the next five sessions exceeding 1.5x ATR* --
and check whether the buckets actually separate. Until then this is a
disciplined way to look at the same eight things every morning, which is
worth something on its own, but it is not a validated signal.

`python3 scripts/selftest.py` checks the scoring math offline, with no
network. It runs in CI before every fetch.

## Not advice

Personal monitoring tool. Not financial advice, not validated against
outcomes, and no part of it predicts direction.
