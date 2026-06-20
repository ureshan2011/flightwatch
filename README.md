# FlightWatch ✈

An open, free, self-hosting fare tracker for the **Christchurch ↔ Colombo** corridor
(and any route you add). It scrapes the **full** fare list **several times a day**,
builds an **intraday** price history, forecasts where the fare is heading with a
calibrated model, and shows a **buy / wait** decision with a **confidence rate** —
plus deal scores, a predicted booking curve, a cheapest-day heatmap and free push
alerts — all running for **$0** on GitHub's free tier.

- **Data:** **Google Flights**, scraped with real headless browsers (Playwright) — no API key, no token, no paid proxy. One **shared** Chromium runs every itinerary concurrently (a lightweight context + rotating **fingerprint** each), harvesting **every** offer per scan to build a large open dataset
- **Cadence:** multiple scans/day; rows carry an hourly `scan_slot` so intraday moves accumulate instead of overwriting
- **Intelligence (100% offline):** quantile gradient-boosting + **split-conformal** bands, seasonality features, a walk-forward **backtest**, deal scores, anomaly detection, best-time-to-book and templated natural-language summaries — no LLM, no API cost
- **Alerts:** optional free **Telegram / email** pushes for new lows, price drops and BUY calls
- **Automation:** GitHub Actions (free & unlimited for public repos) · **Hosting:** GitHub Pages (free)
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
config.yaml ──► flightwatch/collect.py ──► data/flights_YYYY_MM.csv (append-only, intraday slots)
                     (async, shared browser)        │
                     flightwatch/predict.py ───►  model + backtest ┐
                     flightwatch/analytics.py ─►  offline AI layer ┤
                     flightwatch/dashboard.py ─►  docs/index.html  │ (GitHub Pages)
                     flightwatch/alerts.py ────►  Telegram / email ┘ (optional)
```

A GitHub Actions cron runs the collector several times a day. A single shared Chromium
scrapes every itinerary concurrently (each context using a different fingerprint to
dodge soft-blocks), appends **every** fare found as its own CSV row stamped with an
hourly `scan_slot`, retrains the forecast model, regenerates the dashboard, sends any
fresh alerts, and commits everything back to the repo. Over weeks, the per-itinerary
history becomes an intraday *booking curve* — which is what makes a real "should I book
now?" prediction (and an honest backtest of it) possible.

---

## Project layout

```
flightwatch/            # the Python package (importable, no path hacks)
  __init__.py           #   shared paths (repo root, data/, docs/, config.yaml)
  provider.py           #   fare source — Google Flights scraper, sync + async (swap providers here)
  collect.py            #   async shared-browser scan -> appends to data/
  storage.py            #   append-only monthly CSVs, intraday scan_slot de-duplication
  predict.py            #   forecast + buy/wait/watch with conformal bands, seasonality, backtest
  analytics.py          #   offline AI: deal scores, anomalies, heatmap, digest, narratives
  dashboard.py          #   renders docs/index.html
  alerts.py             #   optional free Telegram / email pushes
  __main__.py           #   CLI:  python -m flightwatch [collect|build|all|alert|backtest|diag]
config.yaml             # what to track + concurrency
data/                   # the open dataset (monthly CSVs)
docs/                   # the published dashboard (GitHub Pages)
.github/workflows/      # the scan cron
tests/                  # pytest suite (storage, model, analytics, alerts)
```

Everything runs through the package, so the provider lives in exactly one file
(`flightwatch/provider.py`). To use a different fare source later, keep its
`search_flight_offers` / `search_flight_offers_async` shape and the rest is unchanged.

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
Repo → **Actions** tab → **scan** → **Run workflow**. The workflow installs a
headless Chromium, scrapes each route, and commits the first data point to `data/`.
From then on it runs automatically several times a day. If a run scrapes 0 fares,
download the **scrape-debug** artifact from that run to see exactly what Google returned.

---

## Run locally (optional)

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # one-time browser download

python -m flightwatch collect    # one scrape -> appends to data/
python -m flightwatch build      # regenerate docs/index.html
python -m flightwatch all        # both at once
python -m flightwatch alert      # push fresh signals (dry-prints if no secrets set)
python -m flightwatch backtest   # how the engine's past calls actually fared
python -m flightwatch diag       # scrape one route verbosely + save debug screenshot
open docs/index.html
```

Run the tests with `pytest` (they use synthetic data — no network or browser needed).

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

Tune `concurrency` / `jitter_seconds` here too. Change the scan frequency in
`.github/workflows/daily-scan.yml` (the `cron`, in UTC — currently every 6 hours).
Scheduled runs only fire on the **default branch**, so a new cadence takes effect once
it's merged there.

---

## The buy / wait decision (with confidence)

`flightwatch/predict.py` is the decision engine. While history is thin it uses a
transparent **heuristic** (today's price vs the route's own min/median and days left)
and honestly reports **low confidence**. Once there are enough cheapest-per-day
observations (~120) it trains gradient-boosting models over the booking curve with:

- **Honest validation** — error is **time-series cross-validated** (`TimeSeriesSplit`,
  forward-chaining), never a shuffled split that would leak the future and overstate skill.
- **Calibrated bands** — a **split-conformal** interval whose width is learned from real
  held-out residuals, so an "80% band" actually covers ~80% of outcomes.
- **Seasonality features** — cyclical day-of-week / month / week-of-year encodings,
  peak-season flags, lead-time buckets and a per-route price level.

Each recommendation exposes its full reasoning — **signal** (BUY / WAIT / WATCH),
**confidence** (0–100%), the **whole predicted forward curve** with bands, the expected
**forecast low**, **expected savings** from waiting and the **drop probability**.

**Offline AI layer** (`flightwatch/analytics.py`, no LLM or API) adds, on the dashboard:

- **Deal score** (0–100) for each route's current cheapest fare
- **Predicted booking curve** fan chart + **best time to book**
- **Cheapest day to fly** heatmap (which departure/return weekday is cheapest)
- **What changed** since the last scan, and **anomaly** drop/spike detection
- **Model accuracy** — a walk-forward **backtest** of whether past BUY/WAIT calls paid off
- A per-route **natural-language summary** generated purely from the numbers

…alongside the existing market insights and latest-offers tables.

### Free push alerts (optional)

`flightwatch/alerts.py` turns the freshest signals into Telegram / email notifications,
de-duplicated via `data/alert_state.json`. It is a **no-op until you add secrets**, so
the project stays free and runnable by anyone:

- **Telegram** — message `@BotFather` to create a bot, then set repo **Settings → Secrets
  and variables → Actions** secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- **Email** (optional) — set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL_TO`
  (e.g. a Gmail address + app password).

Alerts fire on a fresh all-time low, an unusual price drop, a new BUY call, or a
"book now — window closing" nudge.

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
| Automation (several runs/day) | GitHub Actions (public repo, standard runner) | Free, unlimited |
| Dashboard hosting | GitHub Pages | Free |
| Data storage | CSV in the repo | Free |
| Fare data | Google Flights scrape (headless Chromium) | Free, no key |
| Alerts | Telegram Bot API / SMTP | Free |
| Forecasts & AI features | scikit-learn, offline (no LLM) | Free |

Each run uses ~2–3 Actions minutes (most of it installing Chromium); a few runs a day
stays comfortably inside the free **unlimited** allowance for public repos.

---

## License

Code: MIT. Data in `data/`: open — attribution appreciated (CC-BY).
