# FlightWatch ✈

An open, free, self-hosting fare tracker for the **Christchurch ↔ Colombo** corridor
(and any route you add). It scrapes the **full** fare list once a day, builds a price
history, forecasts where the fare is heading, and shows a **buy / wait** decision with
a **confidence rate** on a public dashboard — all running for **$0** on GitHub's free tier.

- **Data:** **Google Flights**, scraped with real headless browsers (Playwright) — no API key, no token, no paid proxy. It rotates several **browser fingerprints** and runs **multiple browsers in parallel**, harvesting **every** offer per scan (not just the cheapest) to build a large open dataset
- **Automation:** GitHub Actions (free & unlimited for public repos)
- **Hosting:** GitHub Pages (free)
- **Dataset:** every observation is committed as plain CSV in `data/` — open for anyone

> Fares are informational. Always confirm the live price before booking.

> **Why scrape Google Flights?** FlightWatch first used the Amadeus Self-Service API
> (dropped its free tier), then the Travelpayouts Data API (free, but a *cache* of
> Aviasales searches that simply has no data for a thin route like CHC↔CMB — it
> returned zero fares). Google Flights actually has the fares, and driving a real
> browser needs no key and stays free — so the tracker can be fully autonomous.

> **Heads up:** scraping is inherently more fragile than an official API. Google can
> change its markup or rate-limit a runner. FlightWatch is built to degrade
> gracefully (it records `no_results` instead of crashing and saves debug
> screenshots), and the scraper is isolated in one file so it's easy to repair.

---

## How it works

```
config.yaml ──► flightwatch/collect.py ──► data/flights_YYYY_MM.csv (append-only, time-stamped)
                                                  │
                     flightwatch/dashboard.py ──► docs/index.html  (GitHub Pages)
                     flightwatch/predict.py ──────┘  (buy/wait signal)
```

A GitHub Actions cron runs the collector daily. It scrapes each itinerary in parallel
(each browser using a different fingerprint to dodge soft-blocks), appends **every**
fare found as its own CSV row, retrains the forecast model, regenerates the dashboard,
and commits everything back to the repo. Over weeks, the per-itinerary history becomes
a *booking curve* — which is what makes a real "should I book now?" prediction possible.

---

## Project layout

```
flightwatch/            # the Python package (importable, no path hacks)
  __init__.py           #   shared paths (repo root, data/, docs/, config.yaml)
  provider.py           #   fare source — Google Flights scraper (swap providers here)
  collect.py            #   daily scan -> appends to data/
  storage.py            #   append-only monthly CSVs with de-duplication
  predict.py            #   forecast + buy/wait/watch decision with confidence (heuristic + quantile ML)
  dashboard.py          #   renders docs/index.html
  __main__.py           #   CLI:  python -m flightwatch [collect|build|all|diag]
config.yaml             # what to track
data/                   # the open dataset (monthly CSVs)
docs/                   # the published dashboard (GitHub Pages)
.github/workflows/      # the daily cron
```

Everything runs through the package, so the provider lives in exactly one file
(`flightwatch/provider.py`). To use a different fare source later, keep its two
functions (`search_flight_offers` and `cheapest_offer`) and the rest is unchanged.

---

## Setup (about 10 minutes, all free — no API keys at all)

The scraper needs no token or secret, so setup is just GitHub settings.

### 1. Create the repo
1. Make a **public** GitHub repository (public = free Actions + Pages).
2. Upload these files (or `git push` them).

### 2. Allow the workflow to commit
Repo → **Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** → Save. (The workflow commits the daily data back.)

### 3. Turn on Pages
Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch: `main`,
Folder: `/docs` → Save. Your dashboard will be at
`https://<your-username>.github.io/<repo-name>/`.

### 4. Run it once
Repo → **Actions** tab → **daily-scan** → **Run workflow**. The workflow installs a
headless Chromium, scrapes each route, and commits the first data point to `data/`.
From then on it runs automatically every day. If a run scrapes 0 fares, download the
**scrape-debug** artifact from that run to see exactly what Google returned.

---

## Run locally (optional)

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # one-time browser download

python -m flightwatch collect    # one scrape -> appends to data/
python -m flightwatch build      # regenerate docs/index.html
python -m flightwatch all        # both at once
python -m flightwatch diag       # scrape one route verbosely + save debug screenshot
open docs/index.html
```

Set `FLIGHTWATCH_HEADFUL=1` (the CI default, via `xvfb-run`) to drive a non-headless
Chromium, which Google is less likely to soft-block than pure headless.

---

## Configure what gets tracked

Edit `config.yaml`. Keep the list **small and dense** — daily depth on a few
itineraries beats sparse coverage of a huge grid:

```yaml
itineraries:
  - {origin: CHC, destination: CMB, depart_date: 2026-09-01, return_date: 2026-09-22}
  - {origin: AKL, destination: CMB, depart_date: 2026-09-01, return_date: 2026-09-22}
```

Change the scan time in `.github/workflows/daily-scan.yml` (`cron`, in UTC).

---

## The buy / wait decision (with confidence)

`flightwatch/predict.py` is a small decision engine. While history is thin it uses a
transparent **heuristic** (today's price vs the route's own min/median and days left)
and honestly reports **low confidence**. Once there are enough cheapest-per-day
observations (~120) it trains a **quantile gradient-boosting model** — 10th / 50th /
90th percentile of the booking curve — so every call carries an uncertainty band, not
just a point estimate.

Each recommendation exposes its full reasoning:

- **Signal** — **BUY** / **WAIT** / **WATCH**
- **Confidence** — 0–100%, combining how much data exists with how decisive the signal is
- **Forecast low** — the model's expected cheapest fare over the remaining window
- **Expected savings** — how much waiting is predicted to save (for WAIT)
- **Drop probability** — the chance the fare falls below today's price

The dashboard also renders **market insights** (cheapest / typical / range, airlines,
nonstop availability per route) and the **latest cheapest offers** from each scan.
The model's cross-validated error (±MAE) is shown so you can judge how much to trust it;
expect it to sharpen as history accumulates and inside ~6–8 weeks of departure, when
fares actually start moving.

---

## Troubleshooting: "it ran but scraped 0 fares"

Scraping Google Flights is the trade-off for being keyless and free. If a run
records `no_results`, it's almost always one of:

1. **Google soft-blocked the runner** — datacenter IPs are fingerprinted. The
   workflow already runs a *non-headless* Chromium under `xvfb` (via
   `FLIGHTWATCH_HEADFUL=1`) to look more like a real browser, which is usually
   enough for a once-a-day, few-route scrape. A later daily run typically succeeds.
2. **Google changed its markup** — the extractor keys off the stable
   `"… round trip total"` ARIA label rather than CSS class names, but if Google
   reshapes the page the selectors in `provider.py` may need a tweak.

To see exactly what Google returned, scrape one route locally and inspect the saved
screenshot + HTML:

```bash
python -m flightwatch diag
ls debug/        # diag-CHC-CMB.png / .html
```

Every failed CI scrape also uploads those files as the **scrape-debug** artifact on
the run, so you can debug a remote run without reproducing it locally.

> If Google blocking ever becomes persistent on GitHub's shared IPs, the most
> reliable free fix is to run the same `python -m flightwatch all` on any small
> always-on machine (a home server / Raspberry Pi) on a cron and push the commits —
> the code is identical; only the runner's IP changes.

---

## Cost reality check

| Piece | Service | Cost |
|---|---|---|
| Daily automation | GitHub Actions (public repo, standard runner) | Free, unlimited |
| Dashboard hosting | GitHub Pages | Free |
| Data storage | CSV in the repo | Free |
| Fare data | Google Flights scrape (headless Chromium) | Free, no key |

A daily run uses ~2–3 Actions minutes (most of it installing Chromium) — comfortably
inside every free limit.

---

## License

Code: MIT. Data in `data/`: open — attribution appreciated (CC-BY).
