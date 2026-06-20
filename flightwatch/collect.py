"""
Daily collector. Reads config.yaml, queries each itinerary once, and appends
one observation per itinerary to the monthly CSV.

Run locally:
    TRAVELPAYOUTS_TOKEN=xxx python -m flightwatch collect

In CI this is invoked by .github/workflows/daily-scan.yml.
"""

import os
import time
import traceback
from datetime import datetime, date

import yaml

from . import CONFIG_PATH, provider, storage


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def collect():
    cfg = load_config()
    now = datetime.utcnow()
    scan_date = now.strftime("%Y-%m-%d")
    market = cfg.get("market", "us")
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
            "source": "travelpayouts",
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
                tier = best.get("tier", "dates")
                row = {**base, **best, "status": "ok", "source": f"travelpayouts:{tier}"}
                rows.append(row)
                note = "" if tier == "dates" else f"  (month fallback -> {best.get('found_depart','')})"
                print(f"  OK   {origin}->{dest} {dep}->{ret}  {best['currency']}{best['price']:.0f}{note}")
            else:
                rows.append({**base, "price": "", "airline": "", "stops": "",
                             "duration_minutes": "", "status": "no_results"})
                print(f"  --   {origin}->{dest} {dep}->{ret}  no offers")
        except Exception as e:
            rows.append({**base, "price": "", "airline": "", "stops": "",
                         "duration_minutes": "", "status": "error"})
            print(f"  ERR  {origin}->{dest} {dep}->{ret}  {e}")
            traceback.print_exc()

        time.sleep(cfg.get("delay_seconds", 1.0))  # be polite to the API

    storage.append_rows(rows)
    ok = sum(1 for r in rows if r.get("status") == "ok")
    print(f"\nCollected {ok}/{len(rows)} priced itineraries on {scan_date}.")
    if ok == 0:
        print("No fares found. Run `python -m flightwatch diag` to see what the "
              "Travelpayouts cache holds for these routes.")


def diagnose():
    """
    Print the raw Travelpayouts response for each configured route, per tier.
    Helps answer "is the token working?" and "does the cache have this route?".
    Run with: python -m flightwatch diag
    """
    cfg = load_config()
    currency = cfg.get("currency", "NZD")
    market = cfg.get("market", "nz")
    print(f"Diagnostics -- currency={currency} market={market}\n")

    for it in cfg["itineraries"]:
        origin, dest = it["origin"], it["destination"]
        dep, ret = str(it["depart_date"]), str(it["return_date"])
        print(f"{origin}->{dest}  {dep} -> {ret}")
        try:
            raw = provider.raw_search(origin, dest, dep, ret,
                                      currency=currency, market=market,
                                      max_offers=cfg.get("max_offers_per_search", 5))
        except Exception as e:
            print(f"  ERROR calling API: {e}\n")
            continue
        for tier, body in raw.items():
            ok = body.get("success", False)
            data = body.get("data", []) or []
            sample = ""
            if data:
                d0 = data[0]
                sample = f"  e.g. {d0.get('departure_at','')[:10]} {d0.get('price','')}{currency} {d0.get('airline','')}"
            print(f"  [{tier:5}] success={ok}  offers={len(data)}{sample}")
        print()
