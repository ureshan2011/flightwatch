"""
Daily collector. Reads config.yaml, scrapes each itinerary, and appends EVERY
fare found (one row per offer) to the monthly CSV -- so the dataset grows fast.

Scrapes run concurrently: several headless browsers, each with its own rotating
fingerprint, work different itineraries at the same time. That is what makes
volume scraping practical on the free tier without tripping Google's blocks.

Run locally:
    python -m flightwatch collect

In CI this is invoked by .github/workflows/daily-scan.yml.
"""

import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date

from . import CONFIG_PATH, provider, storage

import yaml


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _scan_one(it, cfg, now, scan_date, market):
    """Scrape a single itinerary and return its list of CSV rows (one per offer)."""
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
        return []

    def empty(status):
        return [{**base, "offer_index": "", "price": "", "airline": "",
                 "stops": "", "duration_minutes": "", "status": status}]

    try:
        offers = provider.search_flight_offers(
            origin, dest, dep, ret,
            adults=cfg.get("adults", 1),
            currency=cfg.get("currency", "NZD"),
            max_offers=cfg.get("max_offers_per_search", 50),
            market=market,
        )
        if offers:
            rows = [{**base, "offer_index": i, **o, "status": "ok"}
                    for i, o in enumerate(offers)]
            best = min(offers, key=lambda o: o["price"])
            print(f"  OK   {origin}->{dest} {dep}->{ret}  "
                  f"{len(offers)} offers, low {best['currency']}{best['price']:.0f}")
            return rows
        print(f"  --   {origin}->{dest} {dep}->{ret}  no offers")
        return empty("no_results")
    except Exception as e:
        print(f"  ERR  {origin}->{dest} {dep}->{ret}  {e}")
        traceback.print_exc()
        return empty("error")


def collect():
    cfg = load_config()
    now = datetime.utcnow()
    scan_date = now.strftime("%Y-%m-%d")
    market = cfg.get("market", "nz")
    itineraries = cfg["itineraries"]
    # How many browsers to run at once. Modest by default to stay polite and fit
    # the free-tier runner's memory; bump `concurrency` in config.yaml to go wider.
    workers = max(int(cfg.get("concurrency", 3) or 1), 1)

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scan_one, it, cfg, now, scan_date, market): it
            for it in itineraries
        }
        for fut in as_completed(futures):
            try:
                rows.extend(fut.result())
            except Exception:
                traceback.print_exc()

    storage.append_rows(rows)
    ok_offers = sum(1 for r in rows if r.get("status") == "ok")
    priced_itins = len({(r["origin"], r["destination"], r["depart_date"],
                         r["return_date"]) for r in rows if r.get("status") == "ok"})
    print(f"\nCollected {ok_offers} fare rows across {priced_itins} itineraries "
          f"on {scan_date}.")
    if ok_offers == 0:
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
    for o in offers[:20]:
        print(f"  {currency}{o.get('price')}  {o.get('airline','')}  "
              f"stops={o.get('stops')}  dur={o.get('duration_minutes')}m")
    print(f"\nDebug screenshot + HTML saved under: {provider.DEBUG_DIR}")
