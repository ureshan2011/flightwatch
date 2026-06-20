# FlightWatch ✈

An open, free, self-hosting fare tracker for the **Christchurch ↔ Colombo** corridor
(and any route you add). It scans fares once a day, builds a price history, and shows
a simple **buy / wait** signal on a public dashboard — all running for **$0** on
GitHub's free tier.

- **Data:** [Travelpayouts (Aviasales) Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API) (free, token only)
- **Automation:** GitHub Actions (free & unlimited for public repos)
- **Hosting:** GitHub Pages (free)
- **Dataset:** every observation is committed as plain CSV in `data/` — open for anyone

> Fares are informational. Always confirm the live price before booking.

> **Why not Amadeus?** FlightWatch originally used the Amadeus Self-Service API,
> but that no longer has a usable free tier. Travelpayouts' Data API is genuinely
> free (a single token, no OAuth, no per-call billing) and returns the cheapest
> cached fares per route + date — exactly what a booking-curve tracker needs.

---

## How it works

```
config.yaml ──► flightwatch/collect.py ──► data/flights_YYYY_MM.csv (append-only, time-stamped)
                                                  │
                     flightwatch/dashboard.py ──► docs/index.html  (GitHub Pages)
                     flightwatch/predict.py ──────┘  (buy/wait signal)
```

A GitHub Actions cron runs the collector daily, appends one priced observation per
itinerary, regenerates the dashboard, and commits both back to the repo. Over weeks,
the per-itinerary history becomes a *booking curve* — which is what makes a real
"should I book now?" prediction possible.

---

## Project layout

```
flightwatch/            # the Python package (importable, no path hacks)
  __init__.py           #   shared paths (repo root, data/, docs/, config.yaml)
  provider.py           #   fare source — Travelpayouts client (swap providers here)
  collect.py            #   daily scan -> appends to data/
  storage.py            #   append-only monthly CSVs with de-duplication
  predict.py            #   buy / wait / watch signal (heuristic + optional ML)
  dashboard.py          #   renders docs/index.html
  __main__.py           #   CLI:  python -m flightwatch [collect|build|all]
config.yaml             # what to track
data/                   # the open dataset (monthly CSVs)
docs/                   # the published dashboard (GitHub Pages)
.github/workflows/      # the daily cron
```

Everything runs through the package, so the provider lives in exactly one file
(`flightwatch/provider.py`). To use a different fare source later, keep its two
functions (`search_flight_offers` and `cheapest_offer`) and the rest is unchanged.

---

## Setup (about 15 minutes, all free)

### 1. Get a free Travelpayouts API token
1. Sign up at <https://www.travelpayouts.com/> (free, no credit card — it's their
   travel affiliate network, which is what gates API access).
2. In your dashboard, open your profile and copy your **API token** from the
   *API token* section. If you can't find it, follow their short guide:
   [Where to find API token](https://support.travelpayouts.com/hc/en-us/articles/13024069738386-Where-to-find-API-token).
   That single token is all FlightWatch needs — there's no OAuth step or paid upgrade.

> The token authorises the **Data API** (`aviasales/v3/prices_for_dates`), which
> returns cheapest cached fares per route + date. It's available to every account;
> only the separate real-time *Flights Search API* has a high traffic requirement,
> and FlightWatch doesn't use it.

### 2. Create the repo
1. Make a **public** GitHub repository (public = free Actions + Pages).
2. Upload these files (or `git push` them).

### 3. Add your token as a repo secret
Repo → **Settings → Secrets and variables → Actions**:
- New **secret** `TRAVELPAYOUTS_TOKEN` = your API token

Secrets are encrypted and never appear in logs or the code.

### 4. Allow the workflow to commit
Repo → **Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** → Save. (The workflow commits the daily data back.)

### 5. Turn on Pages
Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch: `main`,
Folder: `/docs` → Save. Your dashboard will be at
`https://<your-username>.github.io/<repo-name>/`.

### 6. Run it once
Repo → **Actions** tab → **daily-scan** → **Run workflow**. After it finishes, the
first data point lands in `data/` and the dashboard updates. From then on it runs
automatically every day.

---

## Run locally (optional)

```bash
pip install -r requirements.txt
export TRAVELPAYOUTS_TOKEN=your_token

python -m flightwatch collect    # one scan -> appends to data/
python -m flightwatch build      # regenerate docs/index.html
# or do both at once:
python -m flightwatch all
open docs/index.html
```

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

## The buy / wait signal

`flightwatch/predict.py` starts with a transparent **heuristic**: it compares
today's price to that route's own observed minimum/median and the days left to
departure.

- **BUY** — near its lowest observed price with departure approaching
- **WAIT** — priced at/above typical and still far out (history suggests room to fall)
- **WATCH** — not enough history yet, or no clear edge

Once you've accumulated enough observations (~400), it also trains a gradient-boosting
model and reports its cross-validated error. Expect predictions to only become
meaningful inside ~6–8 weeks of departure, when fares actually start moving.

---

## Troubleshooting: "it ran but collected 0 fares"

The Travelpayouts Data API is a **cache** — it only knows fares that people recently
searched on Aviasales. An exact-date round trip on a thin route (like CHC↔CMB) is
the least likely thing to be cached, so an exact-date query often comes back
`success: true` with an empty list.

FlightWatch handles this in two ways:

1. **Market locale** (`market:` in `config.yaml`, default `nz`) — fares are cached
   per storefront, so set this to the country your route is searched from.
2. **Month fallback** — if an exact-date query is empty, the collector retries the
   whole departure month and records the cheapest fare found, tagging the row's
   `source` as `travelpayouts:month` (vs `travelpayouts:dates`). That's still a
   solid daily "cheapest for a September departure" signal for a thin route.

To see exactly what the cache holds for your routes, run:

```bash
export TRAVELPAYOUTS_TOKEN=your_token
python -m flightwatch diag
```

It prints `success` and the number of offers for each route at both the exact-date
and month tiers. If even the month tier is consistently empty for your route, the
Aviasales cache simply doesn't cover it — at that point the keyless **web-scraping**
collector (a drop-in alternative for `provider.py`) is the way to go; open an issue
or ask and it can be wired in.

---

## Cost reality check

| Piece | Service | Cost |
|---|---|---|
| Daily automation | GitHub Actions (public repo, standard runner) | Free, unlimited |
| Dashboard hosting | GitHub Pages | Free |
| Data storage | CSV in the repo | Free |
| Fare data | Travelpayouts (Aviasales) Data API | Free (token only) |

A daily run uses ~1–2 Actions minutes and a handful of API calls — comfortably inside
every free limit.

---

## License

Code: MIT. Data in `data/`: open — attribution appreciated (CC-BY).
