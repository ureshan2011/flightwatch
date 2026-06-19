# FlightWatch ✈

An open, free, self-hosting fare tracker for the **Christchurch ↔ Colombo** corridor
(and any route you add). It scans fares once a day, builds a price history, and shows
a simple **buy / wait** signal on a public dashboard — all running for **$0** on
GitHub's free tier.

- **Data:** [Amadeus Self-Service API](https://developers.amadeus.com) (free tier)
- **Automation:** GitHub Actions (free & unlimited for public repos)
- **Hosting:** GitHub Pages (free)
- **Dataset:** every observation is committed as plain CSV in `data/` — open for anyone

> Fares are informational. Always confirm the live price before booking.

---

## How it works

```
config.yaml ──► src/collect.py ──► data/flights_YYYY_MM.csv (append-only, time-stamped)
                                          │
                     src/build_dashboard.py ──► docs/index.html  (GitHub Pages)
                     src/predict.py ───────────┘  (buy/wait signal)
```

A GitHub Actions cron runs `collect.py` daily, appends one priced observation per
itinerary, regenerates the dashboard, and commits both back to the repo. Over weeks,
the per-itinerary history becomes a *booking curve* — which is what makes a real
"should I book now?" prediction possible.

---

## Setup (about 15 minutes, all free)

### 1. Get free Amadeus API keys
1. Create an account at <https://developers.amadeus.com>.
2. In **My Self-Service Workspace**, create an app. Copy its **API Key** and **API Secret**.
3. You start in the free **test** environment. Later, click **Move to Production**
   (still free up to quota) for live data — then set the repo variable `AMADEUS_ENV` to `production`.

### 2. Create the repo
1. Make a **public** GitHub repository (public = free Actions + Pages).
2. Upload these files (or `git push` them).

### 3. Add your keys as repo secrets
Repo → **Settings → Secrets and variables → Actions**:
- New **secret** `AMADEUS_CLIENT_ID` = your API Key
- New **secret** `AMADEUS_CLIENT_SECRET` = your API Secret
- (Optional) New **variable** `AMADEUS_ENV` = `production` once you've upgraded

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
export AMADEUS_CLIENT_ID=your_key
export AMADEUS_CLIENT_SECRET=your_secret
# export AMADEUS_ENV=production   # once upgraded

python src/collect.py            # one scan -> appends to data/
python src/build_dashboard.py    # regenerate docs/index.html
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

`src/predict.py` starts with a transparent **heuristic**: it compares today's price
to that route's own observed minimum/median and the days left to departure.

- **BUY** — near its lowest observed price with departure approaching
- **WAIT** — priced at/above typical and still far out (history suggests room to fall)
- **WATCH** — not enough history yet, or no clear edge

Once you've accumulated enough observations (~400), it also trains a gradient-boosting
model and reports its cross-validated error. Expect predictions to only become
meaningful inside ~6–8 weeks of departure, when fares actually start moving.

---

## Cost reality check

| Piece | Service | Cost |
|---|---|---|
| Daily automation | GitHub Actions (public repo, standard runner) | Free, unlimited |
| Dashboard hosting | GitHub Pages | Free |
| Data storage | CSV in the repo | Free |
| Fare data | Amadeus Self-Service | Free (test + free production quota) |

A daily run uses ~1–2 Actions minutes and a handful of API calls — comfortably inside
every free limit.

---

## License

Code: MIT. Data in `data/`: open — attribution appreciated (CC-BY).
