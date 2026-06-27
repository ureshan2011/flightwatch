# FlightWatch ✈

An open, free, self-hosting fare tracker for **New Zealand ↔ Sri Lanka & India**
— the **Christchurch ↔ Colombo** and **Auckland ↔ Colombo / Delhi / Mumbai**
corridors out of the box (and any route you add). It scrapes the **full** fare
list **several times a day**,
builds an **intraday** price history, forecasts where the fare is heading with a
calibrated model, and shows a **buy / wait** decision with a **confidence rate** —
plus deal scores, a predicted booking curve, a cheapest-day heatmap and free push
alerts — all running for **$0** on GitHub's free tier.

- **Data:** **Google Flights**, scraped with real headless browsers (Playwright) — no API key, no token, no paid proxy. Shared **Chromium *and* Firefox** browsers run every itinerary concurrently (a lightweight context + rotating **fingerprint/engine** each — a genuine Chrome/Firefox mix, not just spoofed UA strings), harvesting **every** offer per scan to build a large open dataset
- **Coverage:** a rolling **~3-month** grid — **every day** a departure × **every trip length 20–40 days** (~1,900 itineraries, auto-generated relative to *today*) — **sharded across the day's scan slots** so the whole grid is covered daily without any single job blowing the CI limit, on top of a dense fixed set for deep intraday curves
- **Cadence:** multiple scans/day; rows carry an hourly `scan_slot` so intraday moves accumulate instead of overwriting
- **Intelligence (100% offline):** quantile gradient-boosting + **split-conformal** bands, seasonality features, a walk-forward **backtest**, deal scores, anomaly detection, best-time-to-book and templated natural-language summaries — no LLM, no API cost
- **Alerts:** optional free **Telegram / email** pushes for new lows, price drops and BUY calls
- **Automation:** GitHub Actions (free & unlimited for public repos) · **Hosting:** GitHub Pages (free)
- **Dataset:** every observation is committed as plain CSV in `data/` — open for anyone

> Fares are informational. Always confirm the live price before booking.

> **Experimental — Singapore Airlines' own site.** Besides Google Flights, an
> opt-in source (`flightwatch/provider_sq.py`) scrapes Singapore Airlines' public
> **Best Fare Finder** for SQ's *own* end-to-end fares — the genuine "SQ flying
> the whole trip" price. It is **off by default** (the scheduled scan only uses
> Google Flights, which stays rock-solid) because singaporeair.com sits behind
> heavy bot protection and needs selector tuning against a live page. Try it with
> `python -m flightwatch sq-diag CHC CMB 2026-09-05 2026-09-26`, which prints what
> it found and saves a screenshot + HTML to `debug/`.

> **Singapore Airlines on the dashboard.** The command-center spotlights a
> configured carrier (`featured_airline`, default Singapore Airlines). Because SQ
> often flies these corridors as a connection (from Christchurch it's CHC→SIN→CMB
> with Air New Zealand), the dashboard distinguishes **SQ operating one leg** from
> **SQ flying the whole trip on its own metal** (e.g. AKL→SIN→CMB all on SQ) and
> shows the cheapest whole-trip SQ fare separately. The trip finder lets you
> filter for any single carrier — even one that only flies a leg of a connection.

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

A GitHub Actions cron runs the collector several times a day — **and again on every
merge to `main`**, so the published dashboard refreshes the moment a change lands.
Shared Chromium + Firefox browsers scrape every itinerary concurrently (each context
using a different fingerprint/engine to dodge soft-blocks), appends **every** fare
found as its own CSV row stamped with an
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
  publish.py            #   OPTIONAL scan -> Firestore writer (no-op without creds)
  __main__.py           #   CLI:  python -m flightwatch [collect|build|all|alert|publish|backtest|diag]
config.yaml             # what to track + concurrency + monetization
data/                   # the open dataset (monthly CSVs)
docs/                   # the published three-view app (GitHub Pages)
functions/              # OPTIONAL Cloud Function: per-user alert fan-out
firestore.rules         # OPTIONAL Firestore security rules
firebase.json           # OPTIONAL Firebase deploy config
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
python -m playwright install --with-deps chromium firefox   # one-time browser download (both engines)

python -m flightwatch collect    # one scrape -> appends to data/
python -m flightwatch build      # regenerate docs/index.html
python -m flightwatch all        # both at once
python -m flightwatch alert      # push fresh signals (dry-prints if no secrets set)
python -m flightwatch backtest   # how the engine's past calls actually fared
python -m flightwatch diag       # scrape one route verbosely + save debug screenshot
python -m flightwatch sq-diag    # EXPERIMENTAL: scrape Singapore Airlines' own site
open docs/index.html
```

Run the tests with `pytest` (they use synthetic data — no network or browser needed).

Set `FLIGHTWATCH_HEADFUL=1` (the CI default, via `xvfb-run`) to drive non-headless
browsers, which Google is less likely to soft-block than pure headless.

---

## Configure what gets tracked

Edit `config.yaml`. The fixed `itineraries:` list is the **dense** core — daily
depth on a few date-pairs that builds tight intraday curves:

```yaml
itineraries:
  - {origin: CHC, destination: CMB, depart_date: 2026-09-01, return_date: 2026-09-22}
  - {origin: AKL, destination: CMB, depart_date: 2026-09-01, return_date: 2026-09-22}
```

For **breadth**, the `auto_generate:` block sweeps a rolling window of departures
across the next ~3 months for each route — **every day** a departure, paired with
**every trip length in a range** (e.g. 20–40 days). It's recomputed from *today*
every run, so it always covers the next 3 months. Each route can **override any
grid setting** (`depart_step_days`, `horizon_days`, `min_days_out`,
`trip_length_*`), so a dense core corridor and a lighter new route can share one
grid without the additions blowing the CI budget:

```yaml
auto_generate:
  enabled: true
  horizon_days: 90          # ~3 months out (block default)
  min_days_out: 1
  depart_step_days: 1       # every day is a departure date (block default)
  trip_length_min: 20       # trip duration 20..40 days inclusive
  trip_length_max: 40
  trip_length_step: 1
  routes:
    - {origin: CHC, destination: CMB}                      # dense: every day
    - {origin: AKL, destination: CMB, depart_step_days: 4} # lighter sweep
    - {origin: AKL, destination: DEL, depart_step_days: 14}
    - {origin: AKL, destination: BOM, depart_step_days: 14}
  shard_across_slots: true  # see below
  shards: 4
```

That full grid is ~**1,900 itineraries** — far too many for one run. So the
generated grid is **sharded across the day's scan slots**: each 6-hourly run
scrapes one shard (~1/4, a few hundred itineraries, ~1–1.5 h), and across the day
**every date/length is covered exactly once**. Sharding is stable per itinerary, so
the same date/length lands in the same daily slot each day (a clean per-slot
history). The fixed `itineraries:` are **never** sharded — they run every scan for
tight intraday curves. Set `shards` to the number of daily cron slots. Prefer a
denser-but-smaller grid? Just raise `depart_step_days` / `trip_length_step` or drop
`shard_across_slots`.

Tune `concurrency` (how many bots scrape at once) / `jitter_seconds` here too. Change the scan frequency in
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

## The app: Answer · Watch · Lab

The published dashboard (`docs/index.html`, regenerated by `python -m flightwatch
build`) is a single self-contained three-view app driven entirely by the scraped
data — see `redesign/UX_REVAMP.md`:

- **Answer** — tell it your trip (`?o=&d=&dep=&len=` is deep-linkable) and get one
  honest **BUY / WAIT / WATCH** verdict: the best-buy window, a confidence rate
  *with provenance* (observations + this route's backtested hit-rate), a 7-day
  **fare-weather** strip built from the model's conformal forecast curve, and the
  fare story from real daily history. The headline verdict is pre-rendered as
  static HTML so it reads before any JavaScript runs. Below it:
  - **Flights on this trip** — the *distinct* flights tracked on that exact trip
    in the latest scan: operating carrier(s), stops, the real connection
    airport(s) and total duration, with the cheapest tagged. The same airline can
    appear more than once (different routing/timing) — we show each, never
    collapsing it to one line. **Exact flight numbers and departure times are
    confirmed on the booking page** (the data source doesn't expose them, so we
    don't invent them — the Book CTA deep-links there).
  - **Save by being flexible** — the *Flexibility Engine*: for your trip it finds
    the cheapest nearby alternative across three levers — shift the departure date,
    change the trip length, or fly from another NZ origin — each with the saving
    and a real Book link, plus a price-by-departure-date bar. Every suggested
    saving must clear a gate of `max(4%, the model's conformal band)`, so it only
    surfaces real money, never sampling noise.
- **Lab** — pick a route, then dig into *that corridor*: a real, auditable
  walk-forward **backtest scorecard** (accuracy + money saved vs booking on the
  day you first searched), a **price surface** (a departure-date × trip-length
  heatmap — flexibility at a glance, cheapest cell starred), the forecast fan with
  its conformal band, a cheapest-day heatmap, and a trip finder over the route's
  slice of the ~1,900-itinerary grid (lazy-loaded from `explore.json`, kept off
  the Answer's critical path).
- **Watch** — pin trips and get told when to buy. With **no Firebase config it
  works out of the box** in demo mode (trips in `localStorage`); add a config and
  the same code paths become real cross-device sync + per-user alerts (below).

Every "Book" button deep-links into a flight search for the exact route+dates via
the `monetization:` providers in `config.yaml` — add your Travelpayouts marker and
the links start earning, with zero code changes. Fares stay **informational** and
the honest empty/collecting states remain; accuracy and savings are only ever
shown when there's enough history to back them (otherwise "still learning").

---

## Optional: real accounts, sync & per-user alerts (Firebase)

Everything above runs for **$0 with no accounts**. Firebase is a fully optional,
**additive edge** that turns the Watch into a real product — accounts that sync
across devices, live-updating verdicts, and a Cloud Function that pushes *each
user* a personal alert when *their* trip hits BUY / a price target / a closing
window. The scraper, model, CSV pipeline and `data/` are untouched; with no config
the site builds and runs exactly as before. Full design: `redesign/FIREBASE.md`.

Files in this repo for it: `firestore.rules` (security rules), `firebase.json` /
`firestore.indexes.json` (deploy config), and `functions/` (the scheduled
`fanOutAlerts` Cloud Function, which reuses `alerts.py`'s Telegram logic).

**Setup (one-time):**

1. Create a Firebase project (free **Spark** tier is enough for everything except
   the scheduled function, which needs **Blaze** — at this volume it's cents/month).
2. **Auth** → enable **Anonymous** and **Google** sign-in.
3. **Firestore** → create a database, then deploy the rules:
   `firebase deploy --only firestore:rules`.
4. **Functions** (for personal alerts) → `cd functions && npm install`, then
   `firebase deploy --only functions`. Set the shared Telegram token with
   `firebase functions:config:set` / an env var `TELEGRAM_BOT_TOKEN`; for email,
   install the **Trigger Email** extension (the function writes to the `mail`
   collection).
5. Add the project's **public web config** as a GitHub repo **Variable** (not a
   secret — it's not sensitive) named **`FARO_FIREBASE_CONFIG`**, holding the JSON
   Firebase gives you (`{apiKey, authDomain, projectId, messagingSenderId, appId,
   …}`). The build injects it as `window.FARO_FIREBASE` and the Watch goes live.
   Optionally add **`FARO_FCM_VAPID_KEY`** (Cloud Messaging → Web Push
   certificates) to enable browser push.
6. For the scan→Firestore writer, add the Admin **service-account JSON** as a
   GitHub **Secret** named **`FARO_FIREBASE_SERVICE_ACCOUNT`**. The workflow then
   runs `python -m flightwatch publish` after each scan to upsert
   `routes/{route}` verdicts. **Keep this secret out of the repo** — it's the only
   credential that can write `routes/*`.

| GitHub setting | Name | What it is |
|---|---|---|
| Variable | `FARO_FIREBASE_CONFIG` | Public web config JSON (injected inline) |
| Variable | `FARO_FCM_VAPID_KEY` | Optional Web Push VAPID key (enables push) |
| Secret | `FARO_FIREBASE_SERVICE_ACCOUNT` | Admin service-account JSON (Firestore writes) |

`python -m flightwatch publish` is a **clean no-op** when no service account is
configured, and never touches `data/` or the commit step — so it can't affect the
open dataset or the always-free build.

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
| Fare data | Google Flights scrape (headless Chromium + Firefox) | Free, no key |
| Alerts | Telegram Bot API / SMTP | Free |
| Forecasts & AI features | scikit-learn, offline (no LLM) | Free |
| Accounts, sync & live data *(optional)* | Firebase Auth + Firestore (Spark tier) | Free |
| Per-user push alerts *(optional)* | Cloud Functions (Blaze) + FCM | ~cents/month |

Each run takes a few Actions minutes (browser install + scraping the rolling
~3-month grid across Chromium + Firefox); several runs a day plus per-merge refreshes
stay comfortably inside the free **unlimited** allowance for public repos.

---

## License

Code: MIT. Data in `data/`: open — attribution appreciated (CC-BY).
