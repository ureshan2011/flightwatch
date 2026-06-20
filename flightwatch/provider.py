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

The rest of the project only depends on the two functions below
(`search_flight_offers` and `cheapest_offer`) returning normalised dicts, so
swapping in a different provider later means editing only this file.
"""

import os
import requests

# Aviasales Data API v3 -- cheapest prices for specific dates.
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


def search_flight_offers(origin, destination, depart_date, return_date,
                         adults=1, currency="NZD", max_offers=5, market="us"):
    """
    Look up cheapest round-trip fares for one itinerary.

    Returns a list of normalised offer dicts (possibly empty), each shaped like:
        {"price": float, "currency": str, "airline": str,
         "stops": int, "duration_minutes": int}

    `adults` is accepted for interface compatibility; the Data API returns
    per-passenger cached fares, so the figure is informational either way.
    """
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart_date,
        "return_at": return_date,
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
        return []
    resp.raise_for_status()

    body = resp.json()
    if not body.get("success", False):
        return []

    resp_currency = (body.get("currency") or currency).upper()
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
        })
    return offers


def cheapest_offer(offers):
    """Pick the lowest-price normalised offer, or None if there are none."""
    if not offers:
        return None
    return min(offers, key=lambda o: o["price"])
