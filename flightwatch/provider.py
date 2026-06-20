"""
Flight fare provider: Travelpayouts (Aviasales) Data API.

Why Travelpayouts instead of Amadeus?
  - It is genuinely free. Amadeus dropped its free Self-Service tier, which broke
    FlightWatch's $0 promise. Travelpayouts needs only a single API token -- no
    OAuth handshake, no per-call billing, no "move to production" approval step.
  - It returns the cheapest *cached* fares per route + date, which is exactly the
    signal FlightWatch needs to build booking curves over time.
  - It is a plain HTTPS GET, so it runs comfortably inside a free GitHub Actions job.

Get a free token by signing up at https://www.travelpayouts.com/ (free, no credit
card) and copying the token from your dashboard's *API token* section -- see
https://support.travelpayouts.com/hc/en-us/articles/13024069738386-Where-to-find-API-token
Store it as the TRAVELPAYOUTS_TOKEN environment variable -- it is never committed.

Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API

Important: this is a CACHE. The API only knows fares that people recently searched
on Aviasales, so an *exact-date round trip on a thin route* (e.g. CHC <-> CMB) is
the least likely thing to be in cache and often comes back empty. To still get a
usable daily signal we widen the query in tiers (see `search_flight_offers`):

  1. exact dates           -- best fidelity, rarest cache hit
  2. whole departure month -- "cheapest for a Sept 2026 departure", far likelier

The tier that produced each price is recorded (via `offer["tier"]`) so the dataset
stays honest about how the number was obtained.

The rest of the project only depends on the two functions below
(`search_flight_offers` and `cheapest_offer`) returning normalised dicts, so
swapping in a different provider later means editing only this file.
"""

import os
import requests

# Aviasales Data API v3 -- cheapest prices for specific dates (or whole months).
BASE_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def _token() -> str:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing TRAVELPAYOUTS_TOKEN environment variable. "
            "Sign up free at https://www.travelpayouts.com/ and copy your token "
            "from the dashboard's 'API token' section."
        )
    return token


def _request(origin, destination, departure_at, return_at,
             currency, max_offers, market):
    """Raw Data API call. Returns the parsed JSON body (dict)."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": departure_at,
        "return_at": return_at,
        "currency": currency.lower(),
        "one_way": "false",
        "sorting": "price",
        "limit": max_offers,
        "market": market,
        "token": _token(),
    }
    resp = requests.get(BASE_URL, params=params, timeout=60)
    # 400s usually mean "bad/unsupported date", which we treat as empty, not fatal.
    if resp.status_code == 400:
        return {"success": False, "data": [], "_http_status": 400}
    resp.raise_for_status()
    return resp.json()


def _normalise(body, fallback_currency, tier):
    """Turn a raw Data API body into a list of normalised offer dicts."""
    if not body.get("success", False):
        return []
    resp_currency = (body.get("currency") or fallback_currency).upper()
    offers = []
    for o in body.get("data", []):
        try:
            price = float(o["price"])
        except (KeyError, TypeError, ValueError):
            continue
        stops = int(o.get("transfers", 0) or 0) + int(o.get("return_transfers", 0) or 0)
        offers.append({
            "price": price,
            "currency": resp_currency,
            "airline": o.get("airline", "") or "",
            "stops": stops,
            "duration_minutes": int(o.get("duration", 0) or 0),
            # provenance -- not stored as a CSV column, but used to set `source`
            "tier": tier,
            "found_depart": (o.get("departure_at", "") or "")[:10],
            "found_return": (o.get("return_at", "") or "")[:10],
        })
    return offers


def search_flight_offers(origin, destination, depart_date, return_date,
                         adults=1, currency="NZD", max_offers=5, market="nz"):
    """
    Look up cheapest round-trip fares for one itinerary, widening the query until
    the cache returns something.

    Returns a list of normalised offer dicts (possibly empty), each shaped like:
        {"price": float, "currency": str, "airline": str, "stops": int,
         "duration_minutes": int, "tier": str, "found_depart": str, "found_return": str}

    `adults` is accepted for interface compatibility; the Data API returns
    per-passenger cached fares, so the figure is informational either way.
    """
    # YYYY-MM-DD -> YYYY-MM for the month-level fallback.
    dep_month, ret_month = depart_date[:7], return_date[:7]

    tiers = [
        ("dates", depart_date, return_date),
        ("month", dep_month, ret_month),
    ]
    for tier, dep, ret in tiers:
        body = _request(origin, destination, dep, ret, currency, max_offers, market)
        offers = _normalise(body, currency, tier)
        if offers:
            return offers
    return []


def cheapest_offer(offers):
    """Pick the lowest-price normalised offer, or None if there are none."""
    if not offers:
        return None
    return min(offers, key=lambda o: o["price"])


def raw_search(origin, destination, depart_date, return_date,
               currency="NZD", max_offers=5, market="nz"):
    """
    Diagnostic helper: return the raw API body for each tier so you can see exactly
    what the cache holds for a route. Used by `python -m flightwatch diag`.
    """
    dep_month, ret_month = depart_date[:7], return_date[:7]
    return {
        "dates": _request(origin, destination, depart_date, return_date,
                          currency, max_offers, market),
        "month": _request(origin, destination, dep_month, ret_month,
                          currency, max_offers, market),
    }
