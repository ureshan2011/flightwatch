"""
Collector. Reads config.yaml, scrapes each itinerary, and appends EVERY fare
found (one row per offer) to the monthly CSV -- so the dataset grows fast.

Scrapes run concurrently and efficiently: a SINGLE headless Chromium is shared
across the whole run, and each itinerary gets its own lightweight browser
*context* (with its own rotating fingerprint). An asyncio semaphore caps how
many scrape at once, and a little random jitter staggers their starts. Reusing
one browser instead of launching one per itinerary is what makes high
concurrency -- and running several times a day -- practical on the free tier.

Intraday: every run stamps rows with an hourly `scan_slot`, so scans at
different times of day accumulate instead of overwriting each other.

Run locally:
    python -m flightwatch collect

In CI this is invoked by .github/workflows/daily-scan.yml.
"""

import asyncio
import random
import traceback
from datetime import datetime, date

from . import CONFIG_PATH, provider, storage

import yaml


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _base_row(it, cfg, now, scan_date, slot):
    """Build the shared row fields for one itinerary and its days-to-departure."""
    origin, dest = it["origin"], it["destination"]
    dep, ret = str(it["depart_date"]), str(it["return_date"])
    dep_d = datetime.strptime(dep, "%Y-%m-%d").date()
    ret_d = datetime.strptime(ret, "%Y-%m-%d").date()
    base = {
        "scan_datetime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scan_date": scan_date,
        "scan_slot": slot,
        "origin": origin, "destination": dest,
        "depart_date": dep, "return_date": ret,
        "trip_length": (ret_d - dep_d).days,
        "days_to_departure": (dep_d - date.today()).days,
        "currency": cfg.get("currency", "NZD"),
        "source": "googleflights",
    }
    return base, base["days_to_departure"]


def _empty(base, status):
    return [{**base, "offer_index": "", "price": "", "airline": "",
             "stops": "", "duration_minutes": "", "status": status}]


async def _scan_one_async(browser, sem, it, cfg, now, scan_date, slot, market):
    """Scrape one itinerary on the shared browser; return its list of CSV rows."""
    base, dtd = _base_row(it, cfg, now, scan_date, slot)
    origin, dest = base["origin"], base["destination"]
    dep, ret = base["depart_date"], base["return_date"]

    if dtd < 0:                       # departure already passed -- skip
        return []

    async with sem:
        # Stagger starts so concurrent scrapes don't all hit Google at once.
        await asyncio.sleep(random.uniform(0, float(cfg.get("jitter_seconds", 6) or 0)))
        try:
            offers = await provider.search_flight_offers_async(
                browser, origin, dest, dep, ret,
                adults=cfg.get("adults", 1),
                currency=cfg.get("currency", "NZD"),
                max_offers=cfg.get("max_offers_per_search", 50),
                market=market,
            )
        except Exception as e:
            print(f"  ERR  {origin}->{dest} {dep}->{ret}  {e}")
            traceback.print_exc()
            return _empty(base, "error")

    if offers:
        rows = [{**base, "offer_index": i, **o, "status": "ok"}
                for i, o in enumerate(offers)]
        best = min(offers, key=lambda o: o["price"])
        print(f"  OK   {origin}->{dest} {dep}->{ret}  "
              f"{len(offers)} offers, low {best['currency']}{best['price']:.0f}")
        return rows
    print(f"  --   {origin}->{dest} {dep}->{ret}  no offers")
    return _empty(base, "no_results")


async def _collect_async(cfg, now, scan_date, slot, market):
    from playwright.async_api import async_playwright

    workers = max(int(cfg.get("concurrency", 3) or 1), 1)
    sem = asyncio.Semaphore(workers)
    itineraries = cfg["itineraries"]
    rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=provider.headless_mode(), args=provider._LAUNCH_ARGS)
        try:
            tasks = [_scan_one_async(browser, sem, it, cfg, now, scan_date, slot, market)
                     for it in itineraries]
            for res in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(res, Exception):
                    traceback.print_exception(type(res), res, res.__traceback__)
                else:
                    rows.extend(res)
        finally:
            await browser.close()
    return rows


def collect():
    cfg = load_config()
    now = datetime.utcnow()
    scan_date = now.strftime("%Y-%m-%d")
    slot = storage.slot_from_datetime(now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    market = cfg.get("market", "nz")

    rows = asyncio.run(_collect_async(cfg, now, scan_date, slot, market))

    storage.append_rows(rows)
    ok_offers = sum(1 for r in rows if r.get("status") == "ok")
    priced_itins = len({(r["origin"], r["destination"], r["depart_date"],
                         r["return_date"]) for r in rows if r.get("status") == "ok"})
    print(f"\nCollected {ok_offers} fare rows across {priced_itins} itineraries "
          f"on {scan_date} (slot {slot}).")
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
