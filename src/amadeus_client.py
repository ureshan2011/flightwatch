"""
Minimal Amadeus Self-Service API client.

Only needs two things from Amadeus:
  1. An OAuth2 access token (client-credentials grant).
  2. The Flight Offers Search endpoint.

Credentials come from environment variables so they are NEVER committed:
    AMADEUS_CLIENT_ID
    AMADEUS_CLIENT_SECRET
    AMADEUS_ENV         -> "test" (default) or "production"

Free tier notes:
  - The TEST environment is free but returns a limited/cached data set, so some
    routes return few or no offers. Good for wiring everything up.
  - Moving to PRODUCTION is free up to a monthly quota and returns live data.
    Switch by setting AMADEUS_ENV=production once your app is approved.
"""

import os
import time
import requests

_TOKEN_CACHE = {"token": None, "expires_at": 0.0}


def _base_url() -> str:
    env = os.environ.get("AMADEUS_ENV", "test").lower()
    return "https://api.amadeus.com" if env == "production" else "https://test.api.amadeus.com"


def get_access_token() -> str:
    """Fetch (and cache) an OAuth2 access token."""
    now = time.time()
    if _TOKEN_CACHE["token"] and now < _TOKEN_CACHE["expires_at"] - 30:
        return _TOKEN_CACHE["token"]

    client_id = os.environ.get("AMADEUS_CLIENT_ID")
    client_secret = os.environ.get("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET environment variables. "
            "Create a free app at https://developers.amadeus.com to get them."
        )

    resp = requests.post(
        f"{_base_url()}/v1/security/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _TOKEN_CACHE["token"] = data["access_token"]
    _TOKEN_CACHE["expires_at"] = now + float(data.get("expires_in", 1799))
    return _TOKEN_CACHE["token"]


def search_flight_offers(origin, destination, depart_date, return_date,
                         adults=1, currency="NZD", max_offers=5):
    """
    Call Flight Offers Search for one round-trip itinerary.
    Returns the parsed JSON 'data' list (may be empty).
    """
    token = get_access_token()
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": depart_date,
        "returnDate": return_date,
        "adults": adults,
        "currencyCode": currency,
        "max": max_offers,
    }
    resp = requests.get(
        f"{_base_url()}/v2/shopping/flight-offers",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    # 400s usually mean "no offers / bad date", which we treat as empty, not fatal.
    if resp.status_code == 400:
        return []
    resp.raise_for_status()
    return resp.json().get("data", [])


def cheapest_offer(offers):
    """Pick the lowest-price offer and flatten the fields we store."""
    if not offers:
        return None
    best = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
    out_itin = best["itineraries"][0]
    segments = out_itin["segments"]
    carriers = sorted({s["carrierCode"] for s in segments})
    # ISO8601 duration like "PT19H15M" -> minutes
    dur = out_itin.get("duration", "")
    minutes = _iso8601_to_minutes(dur)
    return {
        "price": float(best["price"]["grandTotal"]),
        "currency": best["price"]["currency"],
        "airline": ",".join(carriers),
        "stops": max(len(segments) - 1, 0),
        "duration_minutes": minutes,
    }


def _iso8601_to_minutes(s: str) -> int:
    import re
    if not s:
        return 0
    h = re.search(r"(\d+)H", s)
    m = re.search(r"(\d+)M", s)
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
