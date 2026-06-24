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
import hashlib
import random
import traceback
from datetime import datetime, date, timedelta

from . import CONFIG_PATH, provider, storage

import yaml


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _itin_key(it):
    return (str(it["origin"]), str(it["destination"]),
            str(it["depart_date"]), str(it["return_date"]))


def generate_grid(cfg, existing_keys=None):
    """Build ONLY the auto-generated itinerary grid (not the fixed ones).

    The `auto_generate` block sweeps a rolling window of departures across the
    next few months for each route, pairing every departure with every trip
    length in a configured range. It's computed relative to TODAY on every run,
    so the window always rolls forward -- the grid keeps covering "the next ~3
    months" instead of a frozen set of dates. Itineraries whose key is already in
    `existing_keys` (e.g. a fixed one) are skipped.
    """
    gen = cfg.get("auto_generate") or {}
    if not gen.get("enabled"):
        return []
    routes = gen.get("routes") or []

    seen = set(existing_keys or set())
    today = date.today()
    out = []
    for route in routes:
        origin, dest = route["origin"], route["destination"]
        # Every knob can be overridden PER ROUTE so a dense corridor (CHC<->CMB,
        # every day) and a lighter one (a new India route, every few days) can
        # share one grid without the lighter route blowing up the CI budget.
        horizon = int(_route_opt(route, gen, "horizon_days", 90))
        min_out = int(_route_opt(route, gen, "min_days_out", 0))
        step = max(int(_route_opt(route, gen, "depart_step_days", 3)), 1)
        lengths = _trip_lengths(route, gen)
        for offset in range(min_out, horizon + 1, step):
            dep = today + timedelta(days=offset)
            for length in lengths:
                ret = dep + timedelta(days=length)
                it = {"origin": origin, "destination": dest,
                      "depart_date": dep.isoformat(),
                      "return_date": ret.isoformat()}
                key = _itin_key(it)
                if key in seen:
                    continue
                seen.add(key)
                out.append(it)
    return out


def _route_opt(route, gen, key, default):
    """A grid setting, with a per-route override falling back to the block default."""
    v = route.get(key)
    if v is None:
        v = gen.get(key, default)
    return default if v is None else v


def _trip_lengths(route, gen):
    """Trip lengths for a route: an explicit list, or a min..max range (inclusive)
    swept at `trip_length_step`. The range form is what covers e.g. 20 to 40 days.
    Both forms accept a per-route override."""
    explicit = route.get("trip_lengths") or gen.get("trip_lengths")
    if explicit:
        return [int(x) for x in explicit if int(x) > 0]
    lmin = int(_route_opt(route, gen, "trip_length_min", 21))
    lmax = int(_route_opt(route, gen, "trip_length_max", lmin) or lmin)
    lstep = max(int(_route_opt(route, gen, "trip_length_step", 1)), 1)
    return [n for n in range(lmin, lmax + 1, lstep) if n > 0]


def expand_itineraries(cfg):
    """Full itinerary list: the fixed ones plus the ENTIRE generated grid."""
    fixed = list(cfg.get("itineraries") or [])
    keys = {_itin_key(i) for i in fixed}
    return fixed + generate_grid(cfg, keys)


def _shard_index(now, shards):
    """Which shard this run owns, derived from the scan hour (0..shards-1).

    With a 6-hourly cron there are 4 daily slots, so shards=4 maps each run to a
    distinct quarter of the day. Manual/push-triggered runs at any hour still map
    deterministically into one of the shards.
    """
    return (now.hour * shards) // 24 if shards > 1 else 0


def _itin_shard(it, shards):
    """Stable shard for an itinerary, so the SAME date/length lands in the same
    daily slot every day -- which keeps its per-slot history a clean curve."""
    h = int(hashlib.md5("|".join(_itin_key(it)).encode()).hexdigest(), 16)
    return h % shards


def select_itineraries(cfg, now):
    """Itineraries to scrape THIS run.

    The dense fixed itineraries are always scanned (tight intraday curves). The
    big auto-generated grid is sharded across the day's scan slots, so each run
    scrapes only ~1/shards of it and the whole grid is covered once per day --
    keeping any single job well inside CI limits.
    """
    fixed = list(cfg.get("itineraries") or [])
    keys = {_itin_key(i) for i in fixed}
    grid = generate_grid(cfg, keys)

    gen = cfg.get("auto_generate") or {}
    shards = max(int(gen.get("shards", 4) or 1), 1)
    if gen.get("shard_across_slots", True) and shards > 1 and grid:
        idx = _shard_index(now, shards)
        grid = [it for it in grid if _itin_shard(it, shards) == idx]
    return fixed + grid


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
             "stops": "", "duration_minutes": "", "layover": "", "status": status}]


async def _scan_one_async(browsers, sem, it, cfg, now, scan_date, slot, market):
    """Scrape one itinerary on the shared browsers; return its list of CSV rows."""
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
                browsers, origin, dest, dep, ret,
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
    itineraries = select_itineraries(cfg, now)
    rows = []

    gen = cfg.get("auto_generate") or {}
    shards = max(int(gen.get("shards", 4) or 1), 1)
    if gen.get("enabled") and gen.get("shard_across_slots", True) and shards > 1:
        print(f"Scanning {len(itineraries)} itineraries this run "
              f"(shard {_shard_index(now, shards) + 1}/{shards} of the rolling grid, "
              f"plus fixed itineraries), {workers} concurrent bots.")
    else:
        print(f"Scanning {len(itineraries)} itineraries, {workers} concurrent bots.")

    headless = provider.headless_mode()
    async with async_playwright() as p:
        # Launch ONE shared browser per engine in the fingerprint pool (Chromium
        # + Firefox). Each itinerary's chosen fingerprint routes to the matching
        # engine, so we get a real Chrome/Firefox mix without paying browser
        # start-up cost per scrape.
        browsers = {}
        for engine in provider.engines_in_use():
            launcher = getattr(p, engine)
            browsers[engine] = await launcher.launch(
                **provider._launch_kwargs(engine, headless))
        try:
            tasks = [_scan_one_async(browsers, sem, it, cfg, now, scan_date, slot, market)
                     for it in itineraries]
            for res in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(res, Exception):
                    traceback.print_exception(type(res), res, res.__traceback__)
                else:
                    rows.extend(res)
        finally:
            for browser in browsers.values():
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
