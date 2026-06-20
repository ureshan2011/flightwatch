"""
Daily collector. Reads config.yaml, queries each itinerary once, and appends
one observation per itinerary to the monthly CSV.

Run locally:
    python -m flightwatch collect

In CI this is invoked by .github/workflows/daily-scan.yml.
"""

import time
import traceback
from datetime import datetime, date

from . import CONFIG_PATH, provider, storage

import yaml


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def collect():
    cfg = load_config()
    now = datetime.utcnow()
    scan_date = now.strftime("%Y-%m-%d")
    market = cfg.get("market", "nz")
    rows = []

    for it in cfg["itineraries"]:
        origin, dest = it["origin"], it["destination"]
        dep, ret = str(it["depart_date"]), str(it["return_date"])
        dep_d = datetime.strptime(dep, "%Y-%m-%d").date()
        ret_d = datetime.strptime(ret, "%Y-%m-%d").date()
        trip_len = (ret_d - dep_d).days
        dtd = (dep_d - date.today()).days

        base = {
            "scan_datetime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scan_date": scan_date,
            "origin": origin, "destination": dest,
            "depart_date": dep, "return_date": ret,
            "trip_length": trip_len, "days_to_departure": dtd,
            "currency": cfg.get("currency", "NZD"),
            "source": "googleflights",
        }

        # Skip itineraries whose departure has already passed.
        if dtd < 0:
            continue

        try:
            offers = provider.search_flight_offers(
                origin, dest, dep, ret,
                adults=cfg.get("adults", 1),
                currency=cfg.get("currency", "NZD"),
                max_offers=cfg.get("max_offers_per_search", 5),
                market=market,
            )
            best = provider.cheapest_offer(offers)
            if best:
                rows.append({**base, **best, "status": "ok"})
                print(f"  OK   {origin}->{dest} {dep}->{ret}  {best['currency']}{best['price']:.0f}")
            else:
                rows.append({**base, "price": "", "airline": "", "stops": "",
                             "duration_minutes": "", "status": "no_results"})
                print(f"  --   {origin}->{dest} {dep}->{ret}  no offers")
        except Exception as e:
            rows.append({**base, "price": "", "airline": "", "stops": "",
                         "duration_minutes": "", "status": "error"})
            print(f"  ERR  {origin}->{dest} {dep}->{ret}  {e}")
            traceback.print_exc()

        time.sleep(cfg.get("delay_seconds", 1.0))  # be polite between scrapes

    storage.append_rows(rows)
    ok = sum(1 for r in rows if r.get("status") == "ok")
    print(f"\nCollected {ok}/{len(rows)} priced itineraries on {scan_date}.")
    if ok == 0:
        print("No fares scraped. Run `python -m flightwatch diag` and inspect the "
              "screenshot/HTML written to debug/ to see what Google Flights returned.")


def diagnose():
    """
    Scrape the first configured route with verbose output and always save a
    screenshot + HTML to debug/, so you can confirm the scraper works and tune
    selectors if Google changes its markup. Run with: python -m flightwatch diag
    """
    cfg = load_config()
    currency = cfg.get("currency", "NZD")
    market = cfg.get("market", "nz")
    it = cfg["itineraries"][0]
    origin, dest = it["origin"], it["destination"]
    dep, ret = str(it["depart_date"]), str(it["return_date"])

    url = provider._build_url(origin, dest, dep, ret, cfg.get("adults", 1), currency, market)
    print(f"Diagnostics -- currency={currency} market={market}")
    print(f"Route: {origin}->{dest}  {dep} -> {ret}")
    print(f"URL:   {url}\n")

    offers = provider._scrape(url, debug_tag=f"diag-{origin}-{dest}")
    print(f"Scraped {len(offers)} raw offers.")
    for o in offers[:8]:
        print(f"  {currency}{o.get('price')}  {o.get('airline','')}  "
              f"stops={o.get('stops')}  dur={o.get('duration_minutes')}m")
    print(f"\nDebug screenshot + HTML saved under: {provider.DEBUG_DIR}")
