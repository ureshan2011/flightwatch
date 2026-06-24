"""
Generates docs/index.html (served free by GitHub Pages) from the accumulated data.

The page is a self-contained, modern single-file dashboard. It shows:
  1. Recommendations -- a BUY / WAIT / WATCH call per itinerary, each with a
     confidence rate, the model's predicted future low, expected savings from
     waiting, and the probability the fare drops.
  2. Market insights -- per-route aggregates from the latest scan (cheapest /
     typical / range, airline mix with each airline's cheapest fare, stop
     distribution, fastest vs typical duration) so the open dataset is legible.
  3. Latest fares -- the actual cheapest offers harvested in the most recent
     scan, with airline, stops and flight time per offer.

Self-contained: embeds the data as JSON and loads Chart.js from a CDN. The page
itself adds a light, animated UI -- scroll-reveal sections, count-up stats,
charts -- entirely client-side. Handles the empty / early-days case gracefully.
"""

import os
import re
import json
from datetime import datetime

import pandas as pd

from . import DOCS_DIR, storage, predict, analytics


# --------------------------------------------------------------------------- #
# Airline name tidy-up
# --------------------------------------------------------------------------- #
# The scraper sometimes concatenates the operating carriers of a multi-leg
# itinerary ("Singapore AirlinesAir New Zealand") or captures a route code
# ("CHC-CMB") instead of a carrier. Make airline names presentable so the
# dashboard can actually tell you *who* flies the fare.
_ROUTE_CODE = re.compile(r"^[A-Z]{3}\s*[-–—]\s*[A-Z]{3}$")

# Non-airline noise the scraper sometimes mistakes for a carrier name: Google's
# per-itinerary CO2 emissions line ("706 kg CO2e"), bare units/numbers, etc.
# Rejecting these keeps the airline mix honest (and out of the filter list).
_AIRLINE_NOISE = re.compile(r"co2e?|co₂|emission|\bkg\b", re.I)

# A scraped fragment that is really a PRICE the extractor mistook for a carrier:
# "NZ$4,005", "USD 1,234", "NZ$ 4005", "1,234". The currency symbol/code is
# optional, then a number. These leaked into the airline filter as "005", "006"
# etc. (the price tail after clean-up), so reject them outright.
_PRICE_LIKE = re.compile(r"^[A-Za-z]{0,3}\s*[$€£₹]?\s*\d[\d,. ]*$")

# Carriers that fly (or connect on) the corridors FlightWatch tracks
# (NZ <-> Sri Lanka and NZ <-> India). Used to split fares whose scraped label
# glues two operating carriers together -- "JetstarQantas" or
# "Singapore AirlinesAir New Zealand" -- without breaking single names that
# legitimately contain an internal capital (e.g. "SriLankan"). Order longest-first
# (done below) so "Air India Express" is peeled before "Air India".
_KNOWN_AIRLINES = sorted([
    "Air New Zealand", "Singapore Airlines", "Malaysia Airlines", "Cathay Pacific",
    "China Southern", "China Eastern", "Thai Airways", "Qatar Airways", "Sri Lankan",
    "SriLankan", "Qantas", "Jetstar", "Emirates", "Scoot", "Batik Air", "AirAsia",
    "Fiji Airways", "Etihad", "Korean Air", "Vietnam Airlines", "Garuda Indonesia",
    # India-corridor carriers (AKL/CHC <-> DEL/BOM/MAA/BLR...).
    "Air India Express", "Air India", "IndiGo", "Vistara", "SpiceJet",
    "Sri Lankan Airlines", "Turkish Airlines", "British Airways", "Air China",
], key=len, reverse=True)

# Carrier -> IATA code, used to pull the real airline logo on the dashboard
# (https://pics.avs.io/<w>/<h>/<IATA>.png -- keyless, CORS-enabled).
_AIRLINE_IATA = {
    "Air New Zealand": "NZ", "Singapore Airlines": "SQ", "Malaysia Airlines": "MH",
    "Cathay Pacific": "CX", "China Southern": "CZ", "China Eastern": "MU",
    "Thai Airways": "TG", "Qatar Airways": "QR", "Sri Lankan": "UL", "SriLankan": "UL",
    "Sri Lankan Airlines": "UL",
    "Qantas": "QF", "Jetstar": "JQ", "Emirates": "EK", "Scoot": "TR", "Batik Air": "OD",
    "AirAsia": "AK", "Fiji Airways": "FJ", "Etihad": "EY", "Korean Air": "KE",
    "Vietnam Airlines": "VN", "Garuda Indonesia": "GA",
    "Air India": "AI", "Air India Express": "IX", "IndiGo": "6E", "Vistara": "UK",
    "SpiceJet": "SG", "Turkish Airlines": "TK", "British Airways": "BA", "Air China": "CA",
}


def airline_iata(name):
    """IATA code for the first recognised carrier in a (possibly combined) name."""
    if not name:
        return ""
    for part in re.split(r"\s*\+\s*|,\s*", str(name)):
        code = _AIRLINE_IATA.get(part.strip())
        if code:
            return code
    return ""


def _tokenize_airlines(s):
    """Greedily peel known carrier names from a glued/comma-joined label."""
    parts, rest = [], s
    while rest:
        rest = rest.lstrip(" ,+/-&|")
        if not rest:
            break
        for a in _KNOWN_AIRLINES:
            if rest.lower().startswith(a.lower()):
                parts.append(a)
                rest = rest[len(a):]
                break
        else:
            return None          # hit an unknown chunk -- give up, caller falls back
    return parts


def clean_airline(name):
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    name = re.sub(r"\s{2,}", " ", str(name).strip())
    if not name or _ROUTE_CODE.match(name):
        return ""
    # Drop emissions/unit noise, currency/price strings ("NZ$4,005"), and anything
    # with no actual letters (stray numbers) -- these are never real carrier names.
    if (_AIRLINE_NOISE.search(name) or not re.search(r"[A-Za-z]", name)
            or "$" in name or _PRICE_LIKE.match(name)):
        return ""
    toks = _tokenize_airlines(name)
    if toks:
        # de-dupe while preserving order, then join distinct carriers
        seen = []
        for t in toks:
            if t not in seen:
                seen.append(t)
        return " + ".join(seen)
    # Fallback: an UNRECOGNISED carrier. Real carrier names on these corridors
    # carry no digits, so a leftover multi-digit run means scraper noise (a price
    # tail like "NZ$4,005" or a flight number) -- reject it rather than surface a
    # number as an airline (the bug that filled the filter with "005", "006"...).
    if re.search(r"\d{2,}", name):
        return ""
    # Treat commas/slashes as separators; never split on camelCase.
    return re.sub(r"\s*[,/]\s*", " + ", name).strip()


def _clean_layover(val):
    """Normalise a scraped layover field to a clean 'SIN' / 'SIN, KUL' string.

    Tolerates NaN (legacy rows predate the column) and keeps only plausible
    3-letter airport codes so noise never reaches the dashboard."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    codes = [c.strip().upper() for c in re.split(r"[,/+]", str(val)) if c.strip()]
    codes = [c for c in codes if re.fullmatch(r"[A-Z]{3}", c)]
    seen = []
    for c in codes:
        if c not in seen:
            seen.append(c)
    return ", ".join(seen[:3])


def _airlines_in(day):
    """Distinct, cleaned airline names present in a day's offers (order: cheapest first)."""
    seen, out = set(), []
    for r in day.sort_values("price").itertuples():
        a = clean_airline(getattr(r, "airline", ""))
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _carriers_in(day):
    """Every INDIVIDUAL operating carrier in a day's offers, split out of the
    combined "A + B" labels (cheapest first). This is what lets a visitor filter
    for, say, Singapore Airlines even when it only flies a leg of a connection
    (e.g. "Air New Zealand + Singapore Airlines")."""
    seen, out = [], []
    for combined in _airlines_in(day):
        for part in combined.split(" + "):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
                out.append(part)
    return out


# Airport code -> friendly city (and coords/timezone for the live widgets). The
# corridor is Christchurch <-> Colombo; a few neighbours are included so added
# routes still render with real city names instead of raw codes.
CITY = {
    "CHC": {"name": "Christchurch", "lat": -43.489, "lon": 172.532, "tz": "Pacific/Auckland"},
    "CMB": {"name": "Colombo", "lat": 7.181, "lon": 79.884, "tz": "Asia/Colombo"},
    "AKL": {"name": "Auckland", "lat": -37.008, "lon": 174.792, "tz": "Pacific/Auckland"},
    "WLG": {"name": "Wellington", "lat": -41.327, "lon": 174.805, "tz": "Pacific/Auckland"},
    "ZQN": {"name": "Queenstown", "lat": -45.021, "lon": 168.739, "tz": "Pacific/Auckland"},
    "SIN": {"name": "Singapore", "lat": 1.359, "lon": 103.989, "tz": "Asia/Singapore"},
    "MEL": {"name": "Melbourne", "lat": -37.669, "lon": 144.841, "tz": "Australia/Melbourne"},
    "SYD": {"name": "Sydney", "lat": -33.939, "lon": 151.175, "tz": "Australia/Sydney"},
    "KUL": {"name": "Kuala Lumpur", "lat": 2.745, "lon": 101.710, "tz": "Asia/Kuala_Lumpur"},
    # India -- popular NZ <-> India routes.
    "DEL": {"name": "Delhi", "lat": 28.556, "lon": 77.100, "tz": "Asia/Kolkata"},
    "BOM": {"name": "Mumbai", "lat": 19.090, "lon": 72.868, "tz": "Asia/Kolkata"},
    "MAA": {"name": "Chennai", "lat": 12.994, "lon": 80.171, "tz": "Asia/Kolkata"},
    "BLR": {"name": "Bengaluru", "lat": 13.199, "lon": 77.710, "tz": "Asia/Kolkata"},
    "HYD": {"name": "Hyderabad", "lat": 17.240, "lon": 78.429, "tz": "Asia/Kolkata"},
    "COK": {"name": "Kochi", "lat": 10.152, "lon": 76.401, "tz": "Asia/Kolkata"},
}


def city_name(code):
    code = str(code).strip().upper()
    return CITY.get(code, {}).get("name", code)


def _with_itin(ok):
    ok = ok.copy()
    ok["itin"] = ok["origin"] + "-" + ok["destination"] + " " + \
                 ok["depart_date"].astype(str) + " -> " + ok["return_date"].astype(str)
    return ok


def _stops_breakdown(day):
    counts = {0: 0, 1: 0, 2: 0}
    for s in day["stops"].dropna().astype(int):
        counts[min(int(s), 2)] = counts.get(min(int(s), 2), 0) + 1
    return {"nonstop": counts[0], "one": counts[1], "two_plus": counts[2]}


def _insights(ok):
    """Per-itinerary aggregates from that itinerary's most recent scan."""
    out = []
    for itin, h in ok.groupby("itin"):
        latest_day = h["scan_date"].max()
        day = h[h["scan_date"] == latest_day]
        prices = day["price"].astype(float)
        durs = day["duration_minutes"].dropna()
        durs = durs[durs > 0]
        cheapest = day.loc[prices.idxmin()] if not prices.empty else None
        # Each airline's own cheapest fare that day, so the mix is comparable.
        per_airline = {}
        for r in day.sort_values("price").itertuples():
            a = clean_airline(getattr(r, "airline", ""))
            if a and a not in per_airline:
                per_airline[a] = float(r.price)
        out.append({
            "itinerary": itin,
            "offers": int(len(day)),
            "min": float(prices.min()),
            "median": float(prices.median()),
            "max": float(prices.max()),
            "nonstop": bool((day["stops"] == 0).any()),
            "stops": _stops_breakdown(day),
            "fastest": int(durs.min()) if not durs.empty else 0,
            "typical_duration": int(durs.median()) if not durs.empty else 0,
            "airlines": _airlines_in(day)[:8],
            "airline_prices": [{"name": a, "price": p, "iata": airline_iata(a)}
                               for a, p in sorted(per_airline.items(), key=lambda kv: kv[1])][:6],
            "cheapest_airline": (clean_airline(cheapest["airline"]) if cheapest is not None else ""),
            "cheapest_iata": (airline_iata(clean_airline(cheapest["airline"])) if cheapest is not None else ""),
            "days_to_departure": (int(day["days_to_departure"].dropna().iloc[0])
                                  if day["days_to_departure"].notna().any() else None),
            "scan_date": latest_day.strftime("%Y-%m-%d"),
        })
    out.sort(key=lambda r: r["min"])
    return out


def _latest_offers(ok, per_itin=10):
    """The cheapest offers from each itinerary's most recent scan, with detail."""
    out = {}
    for itin, h in ok.groupby("itin"):
        day = h[h["scan_date"] == h["scan_date"].max()].copy()
        day = day.sort_values("price").head(per_itin)
        out[itin] = [{"price": float(r.price),
                      "airline": clean_airline(getattr(r, "airline", "")),
                      "iata": airline_iata(clean_airline(getattr(r, "airline", ""))),
                      "stops": int(r.stops) if pd.notna(r.stops) else 0,
                      "via": _clean_layover(getattr(r, "layover", "")),
                      "duration": int(r.duration_minutes) if pd.notna(r.duration_minutes) else 0}
                     for r in day.itertuples()]
    return out


def _carrier_fares(day):
    """Each INDIVIDUAL operating carrier's OWN cheapest fare for a day's offers.

    Every offer's (possibly combined) airline label is split into its operating
    carriers and each one is credited with that offer's price/stops/duration, so
    when a visitor filters for, say, Singapore Airlines the finder can show
    Singapore Airlines' *own* cheapest fare for the trip -- not the itinerary's
    overall cheapest, which is frequently a different airline (e.g. Jetstar) and
    was previously shown regardless of the filter. Keyed by carrier; each value
    carries the cheapest price, the representative (combined) label of that
    cheapest offer, the carrier's logo code, stops/duration and a flag for
    whether the carrier flies the route nonstop at all.

    It also tracks WHOLE-TRIP operation: `solo` is True when the carrier operates
    the entire itinerary on its own metal (the offer's label is just that
    carrier, no "+ other"), and `solo_min` is its cheapest such fare. This is what
    lets the dashboard answer "track Singapore Airlines flying the WHOLE trip" --
    distinct from SQ merely operating one leg of a connection.
    """
    fares = {}
    # Ascending price so the first offer seen for a carrier is already its
    # cheapest; later offers only refine the nonstop flag / fastest duration.
    for r in day.sort_values("price").itertuples():
        combined = clean_airline(getattr(r, "airline", ""))
        if not combined:
            continue
        price = float(r.price)
        st = int(r.stops) if pd.notna(r.stops) else None
        du = (int(r.duration_minutes)
              if pd.notna(r.duration_minutes) and r.duration_minutes > 0 else 0)
        lay = _clean_layover(getattr(r, "layover", ""))
        carriers = [c.strip() for c in combined.split(" + ") if c.strip()]
        solo = len(carriers) == 1            # this carrier flies the whole trip
        for carrier in carriers:
            b = fares.get(carrier)
            if b is None:
                fares[carrier] = {"min": price, "stops": st, "ns": (st == 0),
                                  "fast": du, "n": 1, "label": combined,
                                  "iata": airline_iata(carrier), "via": lay,
                                  "solo": solo,
                                  "solo_min": (price if solo else None)}
            else:
                b["n"] += 1
                if price < b["min"]:
                    b["min"], b["stops"], b["label"], b["via"] = price, st, combined, lay
                if st == 0:
                    b["ns"] = True
                if du and (not b["fast"] or du < b["fast"]):
                    b["fast"] = du
                if solo:
                    b["solo"] = True
                    if b["solo_min"] is None or price < b["solo_min"]:
                        b["solo_min"] = price
    for b in fares.values():
        b["min"], b["ns"] = round(b["min"]), bool(b["ns"])
        if b["solo_min"] is not None:
            b["solo_min"] = round(b["solo_min"])
    return fares


def _explore(ok, cap=4000):
    """Flat, filterable list of bookable trip options for the trip finder + fare
    calendar.

    Unlike `_insights` / `_latest_offers` (which only surface the dense fixed
    itineraries), this spans the WHOLE rolling grid -- every departure date x
    trip length we have ever scraped -- using each itinerary's freshest scan.
    That is what lets the client filter by date range, trip length, price,
    stops and airline, and paint a cheapest-fare-by-day calendar entirely
    client-side. Each row also embeds a per-carrier price map (`byair`) so the
    filtered view can show the *selected* airline's own fare. Kept to small
    per-row dicts (and capped) so the embedded JSON stays light even as the grid
    grows.
    """
    out = []
    for itin, h in ok.groupby("itin"):
        day = h[h["scan_date"] == h["scan_date"].max()]
        prices = day["price"].astype(float)
        if prices.empty:
            continue
        cheap = day.loc[prices.idxmin()]
        durs = day["duration_minutes"].dropna()
        durs = durs[durs > 0]
        stops_s = day["stops"].dropna().astype(int)
        byair = _carrier_fares(day)
        # Carriers cheapest-first -- drives the filter membership + ordering.
        al = sorted(byair, key=lambda c: byair[c]["min"])
        air = clean_airline(getattr(cheap, "airline", cheap["airline"]))
        iata = airline_iata(air)
        # The overall-cheapest offer's airline is sometimes unrecognised noise
        # (CO2e strings, route codes) and cleans to "" -- fall back to the
        # cheapest *named* carrier so a card never renders a blank airline.
        if not air and al:
            air, iata = byair[al[0]]["label"], byair[al[0]]["iata"]
        out.append({
            "o": str(cheap["origin"]), "d": str(cheap["destination"]),
            "dep": str(cheap["depart_date"]), "ret": str(cheap["return_date"]),
            "len": (int(cheap["trip_length"]) if pd.notna(cheap["trip_length"]) else None),
            "min": round(float(prices.min())),
            "airline": air, "iata": iata,
            "via": _clean_layover(getattr(cheap, "layover", "")),
            "al": al,
            "byair": byair,
            "stops": (int(stops_s.min()) if not stops_s.empty else None),
            "nonstop": bool((stops_s == 0).any()) if not stops_s.empty else False,
            "fastest": int(durs.min()) if not durs.empty else 0,
            "offers": int(len(day)),
            "dtd": (int(day["days_to_departure"].dropna().iloc[0])
                    if day["days_to_departure"].notna().any() else None),
        })
    out.sort(key=lambda r: (r["dep"], r["len"] or 0))
    return out[:cap]


def _explore_meta(explore):
    """Bounds + option lists that seed the trip-finder's filter controls."""
    if not explore:
        return {}
    prices = [e["min"] for e in explore]
    lens = [e["len"] for e in explore if e["len"]]
    # Each carrier's cheapest fare anywhere in the grid -- shown next to its name
    # in the airline dropdown so the floor for, say, Singapore Airlines is visible
    # before you even filter (and makes clear it differs from the overall cheapest).
    amins = {}
    for e in explore:
        for c, b in (e.get("byair") or {}).items():
            m = b.get("min")
            if m is not None and (c not in amins or m < amins[c]):
                amins[c] = m
    return {
        "count": len(explore),
        "price_min": min(prices), "price_max": max(prices),
        "dep_min": min(e["dep"] for e in explore),
        "dep_max": max(e["dep"] for e in explore),
        "len_min": min(lens) if lens else None,
        "len_max": max(lens) if lens else None,
        # Filter options are individual carriers (incl. those that only fly a leg
        # of a connection), so e.g. Singapore Airlines is selectable on its own.
        "airlines": sorted({c for e in explore for c in (e.get("al") or [])}),
        "airline_min": amins,
        "routes": sorted({e["o"] + "-" + e["d"] for e in explore}),
        "nonstop_any": any(e["nonstop"] for e in explore),
    }


def _carrier_rows(df, carrier):
    """Successful offers that `carrier` actually operates (incl. as one leg of a
    connection, e.g. "Air New Zealand + Singapore Airlines")."""
    ok = df[df["status"] == "ok"].copy()
    keep = ok["airline"].map(lambda a: carrier in clean_airline(a).split(" + "))
    return ok[keep]


def _carrier_focus(df, explore, carrier):
    """Spotlight one carrier for the dashboard's command center.

    Returns its lowest *current* fare across the whole rolling grid, a buy / wait
    call + forecast for that specific fare, and its cheapest fare for each of the
    next few departure months. The decision engine is rerun on the carrier's OWN
    fare history (cheapest carrier fare per itinerary per day) so the call and the
    forecast reflect that airline -- not the itinerary's overall-cheapest airline,
    which is usually someone else. With little history it falls back to the honest
    low-confidence heuristic, exactly like the main recommendations.

    On a thin corridor a premium carrier often only flies a leg of a connection
    (from Christchurch, Singapore Airlines is CHC->SIN->CMB with Air New Zealand),
    so "its fare" is the cheapest offer it operates any part of. Where the carrier
    DOES fly the whole trip on its own metal (e.g. SQ out of Auckland, AKL->SIN->
    CMB all on SQ), that is surfaced separately as the whole-trip fare.
    """
    items = [(f'{e["o"]}-{e["d"]} {e["dep"]} -> {e["ret"]}', e, e["byair"][carrier])
             for e in explore if carrier in (e.get("byair") or {})]
    if not items:
        return None

    # Buy/wait calls from the carrier's own fare curve (model when it has enough
    # history, heuristic otherwise -- bundle=None so any model is carrier-specific).
    recs = {r["itinerary"]: r
            for r in predict.recommendations(_carrier_rows(df, carrier))}

    def _rec_bits(key):
        r = recs.get(key, {})
        return {"signal": r.get("signal", "WATCH"), "confidence": r.get("confidence"),
                "reason": r.get("reason", ""), "method": r.get("method"),
                "points": r.get("points"), "predicted_low": r.get("predicted_low"),
                "expected_savings": r.get("expected_savings"),
                "prob_drop": r.get("prob_drop"), "percentile": r.get("percentile"),
                "trailing_min": r.get("trailing_min")}

    # Headline: the single cheapest current fare this carrier operates.
    key, e, b = min(items, key=lambda t: t[2]["min"])
    cheapest = {"itinerary": key, "o": e["o"], "d": e["d"], "dep": e["dep"],
                "ret": e["ret"], "len": e["len"], "price": b["min"],
                "label": b["label"], "iata": b["iata"], "stops": b["stops"],
                "nonstop": b["ns"], "fastest": b["fast"], "dtd": e.get("dtd"),
                "solo": bool(b.get("solo")), **_rec_bits(key)}

    # Whole-trip metal: the cheapest itinerary this carrier flies end-to-end on
    # its OWN aircraft (no codeshare/connection partner). Distinct from `cheapest`
    # above, which may be a connection where the carrier flies just one leg.
    solo_items = [(k, ee, bb) for k, ee, bb in items if bb.get("solo_min") is not None]
    whole_trip = None
    if solo_items:
        sk, se, sb = min(solo_items, key=lambda t: t[2]["solo_min"])
        whole_trip = {"itinerary": sk, "o": se["o"], "d": se["d"], "dep": se["dep"],
                      "ret": se["ret"], "len": se["len"], "price": sb["solo_min"],
                      "trips": len(solo_items)}

    # Cheapest fare for each of the next departure months (a quick 3-month read).
    bym = {}
    for k, ee, bb in items:
        m = ee["dep"][:7]
        if m not in bym or bb["min"] < bym[m]["price"]:
            bym[m] = {"month": m, "itinerary": k, "price": bb["min"],
                      "dep": ee["dep"], "ret": ee["ret"], "len": ee["len"],
                      "signal": recs.get(k, {}).get("signal", "WATCH")}
    upcoming = [bym[m] for m in sorted(bym)][:3]

    return {"name": carrier, "iata": airline_iata(carrier), "trips": len(items),
            "cheapest": cheapest, "whole_trip": whole_trip, "upcoming": upcoming}


def _configured_routes():
    """Every corridor FlightWatch is set to track (from config), as o-d pairs --
    so the dashboard can show ALL supported routes, even ones still collecting
    their first fares (which is why a freshly-added route looks 'missing')."""
    from . import collect as collect_mod
    try:
        cfg = collect_mod.load_config() or {}
    except Exception:
        return []
    pairs, seen = [], set()
    fixed = list(cfg.get("itineraries") or [])
    gen = (cfg.get("auto_generate") or {}).get("routes") or []
    for it in fixed + gen:
        o, d = str(it.get("origin", "")).upper(), str(it.get("destination", "")).upper()
        if o and d and (o, d) not in seen:
            seen.add((o, d))
            pairs.append((o, d))
    return pairs


def _routes_overview(ok):
    """One card per SUPPORTED corridor: its cheapest live fare, carrier count,
    nonstop availability and a deep-linkable cheapest itinerary -- or a
    'collecting' placeholder for a route we track but haven't scraped yet.

    This is the cross-route explorer that lets a visitor see every corridor at a
    glance (NZ <-> Sri Lanka & India), not just the one with the most history."""
    # Aggregate whatever data we DO have, per corridor, from its freshest scan.
    have = {}
    if ok is not None and not ok.empty:
        ok = ok.copy()
        ok["route"] = ok["origin"].astype(str) + "-" + ok["destination"].astype(str)
        for route, g in ok.groupby("route"):
            day = g[g["scan_date"] == g["scan_date"].max()].copy()
            prices = day["price"].astype(float)
            if prices.empty:
                continue
            cheap = day.loc[prices.idxmin()]
            stops_s = day["stops"].dropna().astype(int)
            air = clean_airline(getattr(cheap, "airline", cheap["airline"]))
            carriers = {c for a in day["airline"].map(clean_airline)
                        for c in (a.split(" + ") if a else []) if c}
            have[route] = {
                "min": round(float(prices.min())),
                "airline": air, "iata": airline_iata(air),
                "via": _clean_layover(getattr(cheap, "layover", "")),
                "dep": str(cheap["depart_date"]), "ret": str(cheap["return_date"]),
                "len": (int(cheap["trip_length"]) if pd.notna(cheap["trip_length"]) else None),
                "nonstop": bool((stops_s == 0).any()) if not stops_s.empty else False,
                "stops": (int(stops_s.min()) if not stops_s.empty else None),
                "carriers": len(carriers),
                "dtd": (int(cheap["days_to_departure"]) if pd.notna(cheap["days_to_departure"]) else None),
            }

    out = []
    for o, d in _configured_routes():
        route = f"{o}-{d}"
        card = {"o": o, "d": d, "from": city_name(o), "to": city_name(d),
                "route": route, "has_data": route in have}
        if route in have:
            card.update(have[route])
        out.append(card)
    # Routes with data first (cheapest first), then the still-collecting ones.
    out.sort(key=lambda c: (not c["has_data"], c.get("min", 1e9)))
    return out


def _highlights(df, ok, recs, ai, focus, routes):
    """A compact set of cross-route, plain-English headline insights for the hero
    -- the "why explore this site" hook. Pure templating over data we already
    computed; no LLM. Each item is {icon, kind, title, sub, itin?}."""
    H = []
    cur = (ok["currency"].iloc[0] if not ok.empty and "currency" in ok else "NZD")
    money = lambda v: f"{cur} {round(v):,}"

    # 1) Cheapest trip anywhere across all supported routes right now.
    routed = [c for c in routes if c.get("has_data")]
    if routed:
        cheapest = min(routed, key=lambda c: c["min"])
        H.append({"icon": "gem", "kind": "cheapest",
                  "title": f"{money(cheapest['min'])} · {cheapest['from']} → {cheapest['to']}",
                  "sub": f"cheapest return across all {len(routes)} tracked routes"
                         + (f", via {cheapest['via']}" if cheapest.get("via") else ""),
                  "itin": f"{cheapest['o']}-{cheapest['d']} {cheapest['dep']} -> {cheapest['ret']}"})

    # 2) Market pulse -- is it a good time to buy right now, market-wide.
    pulse = (ai or {}).get("market", {}).get("pulse") if ai else None
    if pulse:
        H.append({"icon": "pulse", "kind": "pulse", "title": pulse["label"],
                  "sub": pulse["note"], "score": pulse["score"]})

    # 3) Best moment to book, empirically, from the whole grid.
    adv = (ai or {}).get("market", {}).get("advance_curve") if ai else None
    if adv and adv.get("save_vs_worst", 0) > 0:
        H.append({"icon": "clock", "kind": "advance",
                  "title": f"Book ~{adv['best_dtd']} days out",
                  "sub": f"that lead time has been ~{money(adv['save_vs_worst'])} cheaper "
                         f"than the worst window"})

    # 4) Featured carrier whole-trip headline (Singapore Airlines).
    if focus and focus.get("whole_trip"):
        wt = focus["whole_trip"]
        H.append({"icon": "plane", "kind": "carrier",
                  "title": f"{focus['name']} whole-trip from {money(wt['price'])}",
                  "sub": f"end-to-end on {focus['name']} metal · {wt['trips']} dates tracked",
                  "itin": wt["itinerary"]})
    elif focus and focus.get("cheapest"):
        c = focus["cheapest"]
        H.append({"icon": "plane", "kind": "carrier",
                  "title": f"{focus['name']} from {money(c['price'])}",
                  "sub": f"lowest {focus['name']}-operated fare we track"
                         + (f" · via {c.get('label','')}" if not c.get("solo") else ""),
                  "itin": c["itinerary"]})

    # 5) Sweet-spot trip length across the grid.
    lc = (ai or {}).get("market", {}).get("length_curve") if ai else None
    if lc:
        H.append({"icon": "cal", "kind": "length",
                  "title": f"{lc['best_len']} nights is the value sweet spot",
                  "sub": f"cheapest trip length right now, from {money(lc['best_price'])}"})

    return H[:5]


def _airline_market(ok):
    """Across every route's latest scan: each airline's reach and best fare."""
    latest_day = ok["scan_date"].max()
    day = ok[ok["scan_date"] == latest_day]
    agg = {}
    for r in day.itertuples():
        a = clean_airline(getattr(r, "airline", ""))
        if not a:
            continue
        cur = agg.setdefault(a, {"name": a, "iata": airline_iata(a),
                                 "offers": 0, "min": float(r.price)})
        cur["offers"] += 1
        cur["min"] = min(cur["min"], float(r.price))
    return sorted(agg.values(), key=lambda d: d["min"])[:8]


def _next_tick(now, hours):
    """Next UTC time aligned to a multiple of `hours` (the cron cadence)."""
    from datetime import timedelta
    h = (now.hour // max(hours, 1) + 1) * max(hours, 1)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=h)


def _scrape_status(df):
    """Live operational picture of the scraper for the dashboard's status panel:
    when it last ran, what succeeded/failed, the dates/flights covered, and what
    the next scheduled run is expected to scrape. All times are UTC ISO so the
    page can render them live (relative, with countdowns) client-side.
    """
    from . import collect as collect_mod
    try:
        cfg = collect_mod.load_config() or {}
    except Exception:
        cfg = {}
    gen = cfg.get("auto_generate") or {}
    shards = max(int(gen.get("shards", 4) or 1), 1)
    cadence = int(cfg.get("scan_every_hours", 6) or 6)
    sharding = bool(gen.get("shard_across_slots", True)) and shards > 1

    st = {"cadence_hours": cadence, "shards": shards, "has_data": False,
          "recent": [], "now_iso": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}

    # The full rolling grid we aim to cover (fixed itineraries + generated sweep).
    try:
        grid = collect_mod.generate_grid(cfg)
    except Exception:
        grid = []
    fixed = list(cfg.get("itineraries") or [])
    st["grid_total"] = len(grid) + len(fixed)
    st["routes"] = sorted({f'{g["origin"]}-{g["destination"]}' for g in grid + fixed})
    if grid:
        deps = sorted({g["depart_date"] for g in grid})
        st["grid_from"], st["grid_to"] = deps[0], deps[-1]

    # Next scheduled run + which shard (and therefore which dates) it will scrape.
    nxt = _next_tick(datetime.utcnow(), cadence)
    st["next_run_iso"] = nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if grid and sharding:
        idx = collect_mod._shard_index(nxt, shards)
        nxt_grid = [g for g in grid if collect_mod._itin_shard(g, shards) == idx]
        st["next_shard"] = idx + 1
        st["next_count"] = len(nxt_grid) + len(fixed)
        if nxt_grid:
            nd = sorted({g["depart_date"] for g in nxt_grid})
            st["next_from"], st["next_to"] = nd[0], nd[-1]
    else:
        st["next_shard"], st["next_count"] = 1, st["grid_total"]

    if df is None or df.empty or "scan_datetime" not in df.columns:
        return st

    st["has_data"] = True
    sd = df["scan_datetime"].dropna().astype(str)
    st["last_scan_iso"] = sd.max() if not sd.empty else ""

    # Per-slot (per-run) summary for the most recent runs: how many itineraries
    # came back OK vs empty vs errored, and how many fares were harvested.
    g = df.copy()
    g["scan_slot"] = g["scan_slot"].astype(str)
    slots = sorted({s for s in g["scan_slot"].unique() if s and s != "nan"},
                   reverse=True)[:8]
    for s in slots:
        sub = g[g["scan_slot"] == s]

        def _itins(status):
            x = sub[sub["status"] == status]
            return {(r.origin, r.destination, r.depart_date, r.return_date)
                    for r in x.itertuples()}

        ok, nr, er = _itins("ok"), _itins("no_results"), _itins("error")
        attempted = len(ok | nr | er)
        st["recent"].append({
            "slot": s, "ok": len(ok), "no_results": len(nr), "errors": len(er),
            "attempted": attempted, "offers": int((sub["status"] == "ok").sum()),
            "success": round(100 * len(ok) / attempted) if attempted else 0,
        })
    if st["recent"]:
        st["latest"] = st["recent"][0]

    okrows = df[df["status"] == "ok"]
    if not okrows.empty:
        dd = okrows["depart_date"].dropna().astype(str)
        if not dd.empty:
            st["covered_from"], st["covered_to"] = dd.min(), dd.max()
    return st


def _fixed_itin_keys():
    """Build the set of itin strings for the dense fixed itineraries from config."""
    from . import collect as collect_mod
    try:
        cfg = collect_mod.load_config() or {}
    except Exception:
        return set()
    fixed = list(cfg.get("itineraries") or [])
    return {f"{it['origin']}-{it['destination']} {it['depart_date']} -> {it['return_date']}"
            for it in fixed}


def _monetization():
    """Resolve the config `monetization:` block into a compact, client-ready object.

    Every booking CTA on the dashboard is a deep link into a flight search for the
    card's route+dates, built client-side from these provider URL templates. We
    resolve each provider's affiliate credential here (Travelpayouts marker vs
    Skyscanner associate id) so the page just substitutes `{m}`. With no marker
    set the links still work -- just untracked -- so the dashboard is always
    functional. Returns {enabled:false} when monetization is off / unconfigured.
    """
    from . import collect as collect_mod
    try:
        cfg = collect_mod.load_config() or {}
    except Exception:
        cfg = {}
    m = dict(cfg.get("monetization") or {})
    if not m or not m.get("enabled", False):
        return {"enabled": False, "providers": [], "google_flights": True}
    creds = {"travelpayouts": str(m.get("travelpayouts_marker") or "").strip(),
             "skyscanner": str(m.get("skyscanner_associateid") or "").strip()}
    provs = []
    for p in (m.get("providers") or []):
        if not p.get("url"):
            continue
        provs.append({"id": p.get("id"), "name": p.get("name", p.get("id")),
                      "primary": bool(p.get("primary")),
                      "m": creds.get(p.get("marker", "travelpayouts"), ""),
                      "url": p["url"]})
    # Guarantee exactly one primary so the client always has a headline CTA.
    if provs and not any(p["primary"] for p in provs):
        provs[0]["primary"] = True
    return {"enabled": True, "providers": provs,
            "sub": str(m.get("sub_id") or "").strip(),
            "cur": str(cfg.get("currency", "NZD")).lower(),
            "adults": int(cfg.get("adults", 1) or 1),
            "google_flights": bool(m.get("google_flights", True)),
            "disclosure": str(m.get("disclosure") or "").strip()}


def build():
    df = storage.load_all()
    bundle = predict.train_model(df) if not df.empty else None
    recs = predict.recommendations(df, bundle=bundle)
    ai = analytics.build(df, recs, bundle) if not df.empty and (df["status"] == "ok").any() else None

    history, insights, latest_offers, airline_market = {}, [], {}, []
    explore, explore_meta, focus = [], {}, None
    stats = {"scans": 0, "total_offers": 0, "routes": 0, "date_combos": 0,
             "airlines": 0, "avg_price": None, "fastest": None, "cheapest_ever": None}
    cities, primary = {}, None
    # All supported corridors show up even before any are scraped, so a newly
    # added route never looks "missing" -- it just reads as still collecting.
    routes_overview, highlights = _routes_overview(None), []

    fixed_keys = _fixed_itin_keys()

    if not df.empty and (df["status"] == "ok").any():
        ok = _with_itin(df[df["status"] == "ok"])

        # Friendly geography: emit every code seen as a city name, and resolve the
        # corridor's main origin/destination (for the live weather/clock widgets).
        codes = sorted(set(ok["origin"]) | set(ok["destination"]))
        cities = {c: city_name(c) for c in codes}
        dest_code = ok["destination"].mode().iloc[0]
        orig_code = ok["origin"].mode().iloc[0]
        dest_info = CITY.get(dest_code, {})
        orig_info = CITY.get(orig_code, {})
        primary = {
            "origin": {"code": orig_code, "name": city_name(orig_code),
                       "lat": orig_info.get("lat"), "lon": orig_info.get("lon"),
                       "tz": orig_info.get("tz")},
            "dest": {"code": dest_code, "name": city_name(dest_code),
                     "lat": dest_info.get("lat"), "lon": dest_info.get("lon"),
                     "tz": dest_info.get("tz")},
        }

        # Heavy per-itinerary sections (insights, offers, history, sparklines)
        # are restricted to the dense fixed itineraries to keep the page light.
        # The full grid is available via the lazy-loaded trip finder.
        ok_fixed = ok[ok["itin"].isin(fixed_keys)] if fixed_keys else ok

        daily = predict.daily_min(df)
        for itin, h in daily.groupby("itin"):
            if fixed_keys and itin not in fixed_keys:
                continue
            history[itin] = [{"d": d.strftime("%Y-%m-%d"), "p": float(p)}
                             for d, p in zip(h["scan_date"], h["price"])]

        insights = _insights(ok_fixed) if not ok_fixed.empty else []
        latest_offers = _latest_offers(ok_fixed) if not ok_fixed.empty else {}
        airline_market = _airline_market(ok)
        explore = _explore(ok)
        explore_meta = _explore_meta(explore)

        # Spotlight a configured carrier (default Singapore Airlines): its lowest
        # fare, a buy/wait call for it, and its next-3-months lows.
        try:
            from . import collect as collect_mod
            focus_airline = (collect_mod.load_config() or {}).get(
                "featured_airline", "Singapore Airlines")
        except Exception:
            focus_airline = "Singapore Airlines"
        focus = (_carrier_focus(df, explore, focus_airline)
                 if focus_airline else None)

        # Cross-route explorer + combined hero highlights (the "see every route at
        # a glance" answer, incl. routes still collecting their first fares).
        routes_overview = _routes_overview(ok)
        highlights = _highlights(df, ok, recs, ai, focus, routes_overview)

        cheap_idx = ok["price"].astype(float).idxmin()
        cheap = ok.loc[cheap_idx]
        durs_all = ok["duration_minutes"].dropna()
        durs_all = durs_all[durs_all > 0]
        clean_air = ok["airline"].map(clean_airline)
        stats = {
            "scans": int(df["scan_date"].nunique()),
            "total_offers": int(len(ok)),
            # True corridors (CHC-CMB, AKL-CMB, ...) vs date-combos (every dep x
            # length). The old "routes" conflated the two and read as ~1,800.
            # `routes` = corridors with LIVE data (drives whether rows need a route
            # chip to disambiguate); `routes_tracked` = every configured corridor.
            "routes": len({(o, d) for o, d in zip(ok["origin"], ok["destination"])}),
            "routes_tracked": len(routes_overview),
            "date_combos": int(ok["itin"].nunique()),
            "airlines": int(clean_air[clean_air != ""].nunique()),
            "avg_price": float(ok["price"].astype(float).mean()),
            "fastest": int(durs_all.min()) if not durs_all.empty else None,
            "cheapest_ever": {"price": float(cheap["price"]),
                              "itinerary": str(cheap["itin"]),
                              "airline": clean_airline(cheap["airline"]),
                              "iata": airline_iata(clean_airline(cheap["airline"])),
                              "date": cheap["scan_date"].strftime("%Y-%m-%d")},
        }

    if fixed_keys:
        recs = [r for r in recs if r["itinerary"] in fixed_keys]
        if ai:
            if ai.get("deals"):
                ai["deals"] = {k: v for k, v in ai["deals"].items() if k in fixed_keys}
            if ai.get("narratives"):
                ai["narratives"] = {k: v for k, v in ai["narratives"].items() if k in fixed_keys}
            if ai.get("anomalies"):
                ai["anomalies"] = [a for a in ai["anomalies"] if a.get("itin") in fixed_keys]

    payload = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "status": _scrape_status(df),
        "recs": recs,
        "history": history,
        "insights": insights,
        "latest_offers": latest_offers,
        "airline_market": airline_market,
        "explore": explore,
        "explore_meta": explore_meta,
        "focus": focus,
        "routes_overview": routes_overview,
        "highlights": highlights,
        "monetization": _monetization(),
        "stats": stats,
        "cities": cities,
        "primary": primary,
        "ai": ai,
        "model": ({"mae": round(bundle["mae"]), "n": bundle["n"],
                   "conformal": (round(bundle["conformal"]) if bundle.get("conformal") else None),
                   "coverage": round(bundle.get("coverage", 0.8) * 100)} if bundle else None),
        "currency": (df[df["status"] == "ok"]["currency"].iloc[0]
                     if not df.empty and (df["status"] == "ok").any() else "NZD"),
    }

    os.makedirs(DOCS_DIR, exist_ok=True)
    if explore:
        with open(os.path.join(DOCS_DIR, "explore.json"), "w", encoding="utf-8") as ef:
            json.dump(explore, ef, default=_np)
    payload["explore"] = []
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(_html(payload))
    print(f"Dashboard built: {stats['scans']} scans, {stats['total_offers']} fare "
          f"observations across {stats['routes']} routes"
          f"{', model ±'+str(round(bundle['mae'])) if bundle else ''}.")


def _np(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not serializable: {type(o)}")


def _html(p):
    data = json.dumps(p, default=_np)
    return r'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Faro &middot; know when to book</title>
<meta name="description" content="Faro tracks daily fares and tells you the perfect moment to book — with real airline logos, stops, flight times, live weather and currency.">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preconnect" href="https://pics.avs.io" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f7fc;--card:#ffffff;--ink:#0e1426;--muted:#586079;--dim:#97a0b6;
  --line:#eef1f8;--line2:#e1e6f1;
  --brand:#4361ee;--brand2:#7c3aed;--teal:#06b6a4;--pink:#ef4d6d;
  --buy:#10b981;--buy-bg:#e7f8f1;--wait:#f59e0b;--wait-bg:#fdf3e1;--watch:#64748b;--watch-bg:#eef1f8;
  --shadow:0 14px 38px -18px rgba(20,34,74,.20);--shadow-lg:0 34px 72px -26px rgba(20,34,74,.30);
  --radius:18px;
}
*{margin:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);overflow-x:hidden;
  font-family:'Sora',system-ui,-apple-system,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}
.mono{font-family:'IBM Plex Mono',monospace}
.wrap{max-width:1140px;margin:0 auto;padding:0 22px 100px}
a{color:var(--brand);text-decoration:none}
img,canvas{max-width:100%}

/* aurora bg */
.aurora{position:fixed;inset:0;z-index:-2;overflow:hidden;background:
  radial-gradient(1100px 620px at 82% -10%,#e4e7ff 0,transparent 58%),
  radial-gradient(820px 520px at -8% 4%,#d9f6f0 0,transparent 52%),var(--bg)}
.blob{position:absolute;border-radius:50%;filter:blur(84px);opacity:.46;will-change:transform}
.b1{width:520px;height:520px;left:-120px;top:-90px;background:#aeb9ff;animation:float1 20s ease-in-out infinite}
.b2{width:460px;height:460px;right:-120px;top:60px;background:#a3eede;animation:float2 24s ease-in-out infinite}
@keyframes float1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(40px,30px) scale(1.08)}}
@keyframes float2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-50px,40px) scale(1.12)}}

/* nav */
.nav{position:sticky;top:0;z-index:60;backdrop-filter:saturate(170%) blur(14px);
  background:rgba(245,247,252,.74);border-bottom:1px solid var(--line)}
.nav .row{max-width:1140px;margin:0 auto;padding:11px 22px;display:flex;align-items:center;gap:14px}
.brand{font-weight:800;letter-spacing:-.4px;display:flex;align-items:center;gap:10px;font-size:19px}
.mark{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;flex:none;
  background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 6px 16px -6px rgba(59,110,245,.7)}
.mark svg{width:17px;height:17px}
.nav .links{margin-left:auto;display:flex;gap:4px;flex-wrap:wrap}
.nav .links a{font-size:13px;color:var(--muted);padding:7px 13px;border-radius:20px;transition:.2s}
.nav .links a:hover{color:var(--ink);background:#fff;box-shadow:var(--shadow)}
.ctx{display:flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--muted)}
.ctx .pulse{width:8px;height:8px;border-radius:50%;background:var(--buy);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(18,176,124,.5)}70%{box-shadow:0 0 0 8px rgba(18,176,124,0)}100%{box-shadow:0 0 0 0 rgba(18,176,124,0)}}
@media(max-width:720px){.nav .links{display:none}}

/* route switcher (sticky corridor focus) */
.routebar{position:sticky;top:52px;z-index:55;backdrop-filter:saturate(170%) blur(14px);
  background:rgba(245,247,252,.84);border-bottom:1px solid var(--line)}
.rb-inner{max-width:1140px;margin:0 auto;padding:9px 22px;display:flex;align-items:center;gap:14px}
.rb-lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);flex:none}
.rb-pills{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;padding:2px 0;-webkit-overflow-scrolling:touch}
.rb-pills::-webkit-scrollbar{display:none}
.rpill{flex:none;display:flex;flex-direction:column;align-items:flex-start;gap:1px;cursor:pointer;
  background:#fff;border:1.5px solid var(--line2);border-radius:13px;padding:7px 14px;
  font-family:inherit;color:var(--muted);transition:.18s ease;line-height:1.2}
.rpill:hover{border-color:var(--brand);color:var(--ink);transform:translateY(-1px);box-shadow:var(--shadow)}
.rpill .rp-main{font-size:13px;font-weight:700;letter-spacing:-.2px;display:flex;align-items:center;gap:5px}
.rpill .rp-main i{font-style:normal;color:var(--dim);font-weight:500}
.rpill .rp-sub{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--dim);font-weight:500}
.rpill .rp-flag{font-size:10px;color:var(--wait)}
.rpill.on{background:linear-gradient(135deg,var(--brand),var(--brand2));border-color:transparent;color:#fff;
  box-shadow:0 10px 22px -10px rgba(59,110,245,.7)}
.rpill.on .rp-main i,.rpill.on .rp-sub{color:rgba(255,255,255,.82)}
.rpill.on .rp-flag{color:#ffe08a}
.rpill.collecting{opacity:.72}
.rpill.collecting .rp-sub{color:var(--wait)}
@media(max-width:720px){.routebar{top:50px}.rb-lbl{display:none}}

/* per-route collecting panel (shown when a still-warming corridor is focused) */
.collectpanel{display:flex;gap:30px;align-items:center;margin-top:34px;padding:34px 34px;
  background:linear-gradient(135deg,#fff,#f3f6ff);border:1px solid var(--line2);border-radius:24px;
  box-shadow:var(--shadow-lg);position:relative;overflow:hidden}
.collectpanel:before{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(420px 220px at 12% -10%,rgba(67,97,238,.10),transparent 60%)}
.cp-orbit{flex:none;width:96px;height:96px;border-radius:50%;display:grid;place-items:center;position:relative;
  background:radial-gradient(circle at 50% 50%,rgba(67,97,238,.12),transparent 70%)}
.cp-orbit:before,.cp-orbit:after{content:"";position:absolute;border-radius:50%;border:1.5px dashed var(--line2)}
.cp-orbit:before{inset:6px}.cp-orbit:after{inset:22px;border-color:rgba(67,97,238,.35)}
.cp-plane{font-size:30px;animation:cpfloat 3.4s ease-in-out infinite;filter:drop-shadow(0 6px 10px rgba(67,97,238,.3))}
@keyframes cpfloat{0%,100%{transform:translateY(-3px) rotate(-6deg)}50%{transform:translateY(3px) rotate(6deg)}}
.cp-body{flex:1;min-width:0}
.cp-eyebrow{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.2em;color:var(--brand);font-weight:600}
.cp-body h2{font-size:25px;font-weight:800;letter-spacing:-.7px;margin:6px 0 8px}
.cp-body p{color:var(--muted);font-size:14.5px;max-width:560px;line-height:1.6}
.cp-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
.cp-all{background:#fff;border:1.5px solid var(--line2);color:var(--muted);border-radius:12px;padding:10px 16px;
  font:inherit;font-size:13px;font-weight:600;cursor:pointer;transition:.18s ease}
.cp-all:hover{border-color:var(--brand);color:var(--brand);transform:translateY(-1px)}
@media(max-width:680px){.collectpanel{flex-direction:column;text-align:center;gap:18px;padding:26px 20px}.cp-actions{justify-content:center}}

/* hero */
.hero{padding:54px 0 6px;display:grid;grid-template-columns:1.02fr .98fr;gap:36px;align-items:center}
@media(max-width:920px){.hero{grid-template-columns:1fr;padding-top:40px}}
.eyebrow{font-family:'IBM Plex Mono',monospace;letter-spacing:.24em;color:var(--brand);font-size:12px;font-weight:600}
h1{font-size:clamp(36px,6vw,60px);font-weight:800;letter-spacing:-1.8px;margin:16px 0 14px;line-height:1.0}
h1 .grad{background:linear-gradient(110deg,var(--brand),var(--brand2) 46%,var(--teal));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.lead{color:var(--muted);font-size:17.5px;max-width:540px}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}
.lchip{display:flex;align-items:center;gap:10px;background:#fff;border:1px solid var(--line);
  border-radius:14px;padding:9px 13px;box-shadow:var(--shadow);font-size:13px}
.lchip .ico{font-size:16px;line-height:1;width:18px;text-align:center}
.lchip b{font-family:'IBM Plex Mono',monospace;font-weight:600;display:block}
.lchip small{color:var(--dim);font-size:11px;display:block}

/* book-now buttons / direct booking links */
.booklinks{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:2px 0 14px}
.btnbook{display:inline-flex;align-items:center;gap:8px;background:linear-gradient(135deg,var(--brand),var(--brand2));
  color:#fff;font-weight:600;font-size:13px;padding:10px 16px;border-radius:12px;
  box-shadow:0 10px 22px -10px rgba(59,110,245,.75);transition:transform .15s ease,box-shadow .2s ease;white-space:nowrap}
.btnbook:hover{transform:translateY(-1px);box-shadow:var(--shadow-lg);color:#fff}
.btnbook svg{width:15px;height:15px;stroke:#fff;fill:none;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.altbook{font-size:12px;color:var(--dim)}
.altbook a{display:inline-block;color:var(--muted);background:#fff;border:1px solid var(--line2);
  border-radius:8px;padding:3px 9px;margin:2px 1px;text-decoration:none;transition:.15s ease}
.altbook a:hover{color:var(--brand);border-color:var(--brand);transform:translateY(-1px)}

/* deal card */
.dealwrap{perspective:1200px}
.deal{position:relative;background:linear-gradient(160deg,#ffffff,#f4f8ff);border:1px solid var(--line);
  border-radius:24px;padding:24px;box-shadow:var(--shadow-lg);transform-style:preserve-3d;
  transform:rotateX(var(--rx,0deg)) rotateY(var(--ry,0deg));transition:transform .2s ease}
.deal:after{content:"";position:absolute;inset:0;border-radius:24px;pointer-events:none;opacity:0;transition:opacity .3s;
  background:radial-gradient(360px 360px at var(--mx,50%) var(--my,0%),rgba(255,255,255,.85),transparent 60%)}
.deal.hot:after{opacity:.9}
.deal .tagrow{display:flex;align-items:center;gap:10px;transform:translateZ(40px)}
.deal .lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
.deal .route{font-size:22px;font-weight:800;letter-spacing:-.5px;margin:14px 0 2px;transform:translateZ(50px);
  display:flex;align-items:center;gap:10px}
.deal .dates{color:var(--muted);font-size:13.5px;transform:translateZ(34px)}
.deal .pricebig{font-family:'IBM Plex Mono',monospace;font-weight:600;letter-spacing:-1.5px;
  margin:16px 0 0;transform:translateZ(64px);line-height:1;display:flex;align-items:baseline;gap:8px}
.deal .pricebig .cur{font-size:15px;color:var(--dim);letter-spacing:0;font-weight:500}
.deal .pricebig .v{font-size:46px}
.deal .pricebig small{font-size:13px;color:var(--dim);letter-spacing:0;font-weight:400}
.deal .meta2{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px;transform:translateZ(40px)}
.deal .fx{margin-top:14px;display:flex;gap:7px;flex-wrap:wrap;transform:translateZ(26px)}
.deal .fx span{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);background:#eef3fd;border-radius:8px;padding:3px 8px}

.sig{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:12px;padding:6px 12px;border-radius:12px;white-space:nowrap}
.BUY{background:var(--buy-bg);color:var(--buy)}.WAIT{background:var(--wait-bg);color:var(--wait)}.WATCH{background:var(--watch-bg);color:var(--watch)}
.tag{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);background:#eef3fd;border-radius:8px;padding:4px 9px}
.tag.b{color:var(--brand);background:rgba(59,110,245,.1)}

/* airline logo / avatar */
.av{position:relative;display:inline-grid;place-items:center;border-radius:9px;overflow:hidden;flex:none;
  color:#fff;font-weight:700;font-family:'IBM Plex Mono',monospace;line-height:1}
.av .ini{position:absolute;inset:0;display:grid;place-items:center}
.av img{position:relative;width:100%;height:100%;object-fit:contain;background:#fff;padding:13%}

/* stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,172px),1fr));gap:14px;margin-top:34px;perspective:1200px}
.tilt{background:#fff;border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);
  position:relative;transform-style:preserve-3d;
  transform:perspective(900px) rotateX(calc(var(--rx,0deg) + var(--srx,0deg))) rotateY(var(--ry,0deg)) translateY(calc(var(--lift,0px) * -1));
  transition:transform .18s ease,box-shadow .25s ease;will-change:transform}
.tilt:hover{box-shadow:var(--shadow-lg)}
.tilt:after{content:"";position:absolute;inset:0;border-radius:var(--radius);pointer-events:none;opacity:0;transition:opacity .3s;
  background:radial-gradient(240px 240px at var(--mx,50%) var(--my,0%),rgba(255,255,255,.9),transparent 60%)}
.tilt.hot:after{opacity:1}
.stat{display:flex;flex-direction:column;min-height:148px;padding:18px 18px 16px}
.stat .ic{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;flex:none;transform:translateZ(30px);
  background:linear-gradient(135deg,rgba(59,110,245,.14),rgba(122,92,240,.14))}
.stat .ic svg{width:21px;height:21px;stroke:var(--brand);fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.stat .n{font-family:'IBM Plex Mono',monospace;font-size:25px;font-weight:600;letter-spacing:-.6px;margin-top:14px;
  white-space:nowrap;display:flex;align-items:baseline;gap:5px;transform:translateZ(42px)}
.stat .n .cur{font-size:12px;color:var(--dim);font-weight:500;letter-spacing:0}
.stat .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.12em;margin-top:auto;padding-top:12px;transform:translateZ(20px)}
.stat .x{color:var(--muted);font-size:12px;margin-top:4px;display:flex;align-items:center;gap:6px;transform:translateZ(16px)}

/* sections */
.section{margin:62px 0 6px;display:flex;align-items:baseline;gap:14px}
.section h2{font-size:22px;font-weight:800;letter-spacing:-.6px;position:relative;padding-left:15px}
.section h2:before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:5px;height:.82em;border-radius:3px;background:linear-gradient(160deg,var(--brand),var(--brand2))}
.section .hint{font-size:12px;color:var(--dim);font-family:'IBM Plex Mono',monospace;margin-left:auto}
.rsec{display:block}

/* rec cards */
.rec{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px 22px;margin-top:16px;
  display:grid;grid-template-columns:auto 1fr auto auto;gap:20px;align-items:center;box-shadow:var(--shadow);
  transition:transform .25s ease,box-shadow .25s ease}
.rec:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.rec .route{font-weight:700;font-size:16.5px;letter-spacing:-.3px}
.rec .dates{color:var(--dim);font-size:12.5px;margin-top:1px}
.rec .reason{color:var(--muted);font-size:13.5px;margin-top:5px}
.conf{margin-top:9px;max-width:300px}
.conf .lab{font-size:11px;color:var(--dim);font-family:'IBM Plex Mono',monospace;display:flex;justify-content:space-between}
.bar{height:7px;background:#eaf0fa;border-radius:7px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;width:0;border-radius:7px;background:linear-gradient(90deg,var(--brand),var(--teal));transition:width 1.1s cubic-bezier(.2,.7,.2,1)}
.tags{margin-top:10px;display:flex;gap:7px;flex-wrap:wrap}
.spark{width:130px;height:46px}
.pricebox{text-align:right}
.price{font-family:'IBM Plex Mono',monospace;font-size:25px;font-weight:600;letter-spacing:-1px}
.pricelbl{color:var(--dim);font-size:11px;font-family:'IBM Plex Mono',monospace;margin-top:2px}
@media(max-width:820px){.rec{grid-template-columns:auto 1fr}.spark,.pricebox{grid-column:1/-1;justify-self:start}.pricebox{text-align:left}.spark{width:100%}}

/* charts */
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;margin-top:16px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow)}
.panel h3{font-size:14px;font-weight:700}.panel .ph{color:var(--dim);font-size:12px;margin:2px 0 14px}
.canvas-wrap{position:relative;height:252px}

/* insight cards */
.icards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,334px),1fr));gap:16px;margin-top:16px}
.icard{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow);transition:transform .25s,box-shadow .25s}
.icard:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.icard .top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.icard .rt{font-weight:700;font-size:15.5px;letter-spacing:-.2px}
.icard .when{font-size:11.5px;color:var(--dim);font-family:'IBM Plex Mono',monospace;margin-top:2px}
.icard .big{font-family:'IBM Plex Mono',monospace;font-size:23px;font-weight:600;letter-spacing:-.5px;color:var(--buy)}
.icard .big small{font-size:11px;color:var(--dim);font-weight:400;display:block;text-align:right}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0 4px}
.fact{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:9px 11px}
.fact .k{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em}
.fact .v{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:14px;margin-top:2px}
.alist{margin-top:14px;display:flex;flex-direction:column;gap:9px}
.aline{display:flex;align-items:center;gap:11px;font-size:13px}
.aline .nm{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.aline .pr{font-family:'IBM Plex Mono',monospace;font-weight:600;color:var(--muted)}
.stopbar{display:flex;height:8px;border-radius:6px;overflow:hidden;margin-top:14px;background:#eaf0fa}
.stopbar i{display:block;height:100%}
.stopkey{display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.stopkey span{display:flex;align-items:center;gap:5px}
.swatch{width:9px;height:9px;border-radius:3px;display:inline-block}

/* fares */
details{background:#fff;border:1px solid var(--line);border-radius:16px;margin-top:12px;padding:2px 18px;box-shadow:var(--shadow);overflow:hidden}
summary{cursor:pointer;padding:15px 0;font-weight:600;font-size:14.5px;display:flex;align-items:center;justify-content:space-between;gap:10px;list-style:none}
summary::-webkit-details-marker{display:none}
summary .pill{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--brand);background:rgba(59,110,245,.1);padding:3px 9px;border-radius:20px;white-space:nowrap}
summary:after{content:"+";color:var(--dim);font-size:18px;margin-left:6px;transition:.2s}
details[open] summary:after{transform:rotate(45deg)}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -4px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:8px}
th,td{text-align:left;padding:10px 8px;border-top:1px solid var(--line)}
th{color:var(--dim);font-family:'IBM Plex Mono',monospace;font-weight:500;font-size:10.5px;text-transform:uppercase;letter-spacing:.1em}
td.num,th.num{text-align:right;font-family:'IBM Plex Mono',monospace}
td .airline{display:flex;align-items:center;gap:10px;min-width:0}
td .airline .anm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr.best td{background:var(--buy-bg)}
/* fares table: stay legible and never clip on small screens */
@media(max-width:600px){
  details{padding:2px 13px}
  table{font-size:12px}
  th,td{padding:8px 5px}
  td .airline{gap:7px}
  td .airline .anm{max-width:38vw}
  .ft{display:none}              /* drop "flight time" col on phones */
  .cheapest{display:none}
}
.cheapest{font-size:10px;color:var(--buy);font-weight:700;font-family:'IBM Plex Mono',monospace;margin-left:8px}
.chip{font-family:'IBM Plex Mono',monospace;font-size:11px;padding:2px 8px;border-radius:20px;background:#eef3fd;color:var(--muted)}
.chip.ns{background:var(--buy-bg);color:var(--buy)}

.empty{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:54px 30px;text-align:center;margin-top:34px;box-shadow:var(--shadow)}
.empty h2{font-size:22px;margin-bottom:10px}.empty p{color:var(--muted);max-width:520px;margin:0 auto}

.foot{margin-top:70px;border-top:1px solid var(--line);padding-top:26px;color:var(--dim);font-size:12.5px;line-height:1.8}
.affdisc{margin-top:16px;padding:11px 14px;background:#fff;border:1px solid var(--line2);border-radius:12px;
  font-size:11.5px;line-height:1.65;color:var(--muted)}
.affdisc:empty{display:none}
.author{display:flex;align-items:center;gap:13px;margin-top:22px}
.author .ring{width:46px;height:46px;border-radius:14px;flex:none;display:grid;place-items:center;color:#fff;font-weight:800;
  font-family:'IBM Plex Mono',monospace;font-size:16px;background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 10px 22px -8px rgba(122,92,240,.7)}
.author .who{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.16em}
.author .nm{font-size:16px;font-weight:700;color:var(--ink);letter-spacing:-.2px}

.reveal{opacity:0;transform:translateY(26px);transition:opacity .7s ease,transform .7s cubic-bezier(.2,.7,.2,1)}
.reveal.in{opacity:1;transform:none}
@media(prefers-reduced-motion:reduce){.reveal{opacity:1;transform:none;transition:none}.bar>i{transition:none}.blob{animation:none}.tilt,.deal{transition:none}}

/* ---- AI layer ---- */
.dealbadge{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;padding:4px 9px;border-radius:9px;white-space:nowrap}
.dealbadge.great{background:var(--buy-bg);color:var(--buy)}.dealbadge.good{background:rgba(59,110,245,.1);color:var(--brand)}
.dealbadge.fair{background:var(--watch-bg);color:var(--watch)}.dealbadge.high{background:var(--wait-bg);color:var(--wait)}
.narr{color:var(--muted);font-size:13px;line-height:1.6;margin-top:9px;border-left:3px solid var(--brand);padding-left:11px}
.alertbar{display:flex;align-items:flex-start;gap:12px;background:linear-gradient(120deg,#fff6ec,#ffeef4);border:1px solid var(--line);
  border-radius:16px;padding:14px 18px;margin-top:18px;box-shadow:var(--shadow)}
.alertbar .ab-ic{font-size:20px;line-height:1}
.alertbar .ab-t{font-weight:700;font-size:14px}.alertbar .ab-s{color:var(--muted);font-size:12.5px;margin-top:2px}
.cols3{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:16px;margin-top:16px}
.bookcard{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow)}
.bookcard .rt{font-weight:700;font-size:14.5px}.bookcard .when{font-size:12px;color:var(--dim);font-family:'IBM Plex Mono',monospace;margin-top:2px}
.bookcard .verdict{font-family:'IBM Plex Mono',monospace;font-weight:600;margin-top:12px;font-size:14px}
.bookcard .verdict.now{color:var(--buy)}.bookcard .verdict.wait{color:var(--wait)}
.bookcard .sub{color:var(--muted);font-size:12.5px;margin-top:6px}
.heat{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.heat .cell{flex:1;min-width:42px;text-align:center;border-radius:10px;padding:9px 4px;border:1px solid var(--line);background:var(--bg)}
.heat .cell .d{font-size:11px;color:var(--dim);font-family:'IBM Plex Mono',monospace}
.heat .cell .p{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:12.5px;margin-top:3px}
.heat .cell.best{background:var(--buy-bg);border-color:#bfe9d8}.heat .cell.best .p{color:var(--buy)}
.digest{display:flex;flex-direction:column;gap:9px;margin-top:6px}
.digest .row{display:flex;align-items:center;gap:10px;font-size:13px;padding:8px 0;border-top:1px solid var(--line)}
.digest .row:first-child{border-top:0}
.digest .mv{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:12.5px;margin-left:auto}
.mv.down{color:var(--buy)}.mv.up{color:var(--wait)}
.bigstat{display:flex;align-items:baseline;gap:8px;margin:4px 0 2px}
.bigstat .v{font-family:'IBM Plex Mono',monospace;font-size:34px;font-weight:600;letter-spacing:-1px}
.bigstat .u{color:var(--dim);font-size:13px}
.sigrow{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:12.5px;color:var(--muted)}
.sigrow b{font-family:'IBM Plex Mono',monospace;color:var(--ink)}

/* ---- live scraper operations panel ---- */
.ops{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:20px 22px;margin:8px 0 26px;position:relative;overflow:hidden}
.ops::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--dim);transition:.4s}
.ops.live::before{background:linear-gradient(var(--buy),#14c98c)}
.ops.due::before{background:var(--wait)}
.ops.stale::before{background:var(--pink)}
.ops-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ops-badge{display:inline-flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;
  font-size:12px;font-weight:600;letter-spacing:.4px;padding:6px 12px;border-radius:30px;text-transform:uppercase}
.ops-badge.live{color:#0a8f63;background:var(--buy-bg)}
.ops-badge.due{color:#b9701a;background:var(--wait-bg)}
.ops-badge.stale{color:#c0436a;background:#fdeaf0}
.ops-badge .dot{width:8px;height:8px;border-radius:50%;background:currentColor;position:relative}
.ops-badge.live .dot{animation:opspulse 1.6s ease-out infinite}
@keyframes opspulse{0%{box-shadow:0 0 0 0 rgba(18,176,124,.55)}100%{box-shadow:0 0 0 9px rgba(18,176,124,0)}}
.ops-title{font-weight:700;font-size:16px;letter-spacing:-.2px}
.ops-sub{color:var(--dim);font-size:12.5px;margin-left:auto;font-family:'IBM Plex Mono',monospace}
.ops-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:12px;margin-top:18px}
.ops-cell{background:#f7faff;border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.ops-cell .k{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);font-weight:600}
.ops-cell .v{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600;color:var(--ink);margin-top:5px;letter-spacing:-.5px}
.ops-cell .s{font-size:11.5px;color:var(--muted);margin-top:3px}
.ops-cell .v small{font-size:12px;color:var(--muted);font-weight:400}
.ops-next{margin-top:16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  background:linear-gradient(100deg,#eef3ff,#f3f0ff);border:1px solid var(--line2);border-radius:14px;padding:12px 15px;font-size:13px;color:var(--muted)}
.ops-next b{color:var(--ink)}
.ops-next .nxt-ic{width:26px;height:26px;border-radius:8px;flex:none;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;font-size:14px}
.ops-runs{margin-top:16px}
.ops-runs .rh{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);font-weight:600;margin-bottom:9px}
.ops-bars{display:flex;gap:7px;align-items:flex-end;flex-wrap:wrap}
.runbar{flex:1;min-width:46px;max-width:90px}
.runbar .stack{height:46px;display:flex;flex-direction:column-reverse;border-radius:7px;overflow:hidden;background:#eef2fa;border:1px solid var(--line)}
.runbar .seg{width:100%}
.runbar .seg.ok{background:linear-gradient(var(--buy),#16c78d)}
.runbar .seg.nr{background:#cfd9ea}
.runbar .seg.er{background:var(--pink)}
.runbar .cap{font-size:10px;color:var(--dim);text-align:center;margin-top:5px;font-family:'IBM Plex Mono',monospace}
.runbar.now .cap{color:var(--brand);font-weight:700}
.ops-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11.5px;color:var(--muted)}
.ops-legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.ops-routes{margin-top:14px;font-size:12px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.ops-routes .rt{background:#eef2fa;border:1px solid var(--line);border-radius:20px;padding:3px 10px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--muted)}
@media(max-width:560px){.ops-sub{margin-left:0;width:100%}}

/* ---- trip finder (filters + fare calendar) ---- */
.finder{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:20px 22px;margin-top:16px}
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:14px}
.fld{display:flex;flex-direction:column;gap:6px}
.fld label{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);font-weight:600}
.fld input,.fld select{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--ink);background:#f7faff;
  border:1px solid var(--line2);border-radius:11px;padding:9px 11px;width:100%;transition:.15s}
/* replace the browser's default dark dropdown triangle with a soft branded chevron */
.fld select{appearance:none;-webkit-appearance:none;-moz-appearance:none;padding-right:34px;cursor:pointer;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2356678a' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center;background-size:13px}
.fld select::-ms-expand{display:none}
.fld input:focus,.fld select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(59,110,245,.12);background-color:#fff}
.fld.range{gap:8px}
.fld .rangeval{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--brand);font-weight:600}
.fld input[type=range]{padding:0;accent-color:var(--brand);background:transparent;border:0}
.seg-ctrl{display:flex;background:#eef2fa;border:1px solid var(--line2);border-radius:11px;padding:3px;gap:2px}
.seg-ctrl button{flex:1;border:0;background:transparent;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted);
  padding:6px 4px;border-radius:8px;cursor:pointer;transition:.15s;white-space:nowrap}
.seg-ctrl button.on{background:#fff;color:var(--brand);box-shadow:var(--shadow);font-weight:600}
.finder-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}
.finder-bar .count{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--ink);font-weight:600}
.finder-bar .count b{color:var(--brand)}
.finder-bar .reset{margin-left:auto;font-size:12px;color:var(--muted);background:none;border:0;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.finder-bar .reset:hover{color:var(--brand)}
.viewtoggle{display:flex;background:#eef2fa;border:1px solid var(--line2);border-radius:10px;padding:3px;gap:2px}
.viewtoggle button{border:0;background:transparent;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--muted);padding:5px 12px;border-radius:7px;cursor:pointer}
.viewtoggle button.on{background:#fff;color:var(--brand);box-shadow:var(--shadow);font-weight:600}

/* calendar */
.cal-wrap{margin-top:16px}
.cal-legend{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted);margin-bottom:10px;flex-wrap:wrap}
.cal-legend .scale{display:flex;height:9px;width:120px;border-radius:5px;overflow:hidden}
.cal-legend .scale i{flex:1}
.cal-months{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:18px}
/* let the calendar tracks/cells shrink below their content so 7 columns always
   fit the container instead of being clipped by body{overflow-x:hidden} */
.cal-months,.cal-month,.cal-grid,.cal-cell,.cal-grid .dow{min-width:0}
.cal-month .mlabel{font-weight:700;font-size:14px;margin-bottom:9px}
.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px}
.cal-grid .dow{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);text-align:center;font-family:'IBM Plex Mono',monospace;padding-bottom:2px}
.cal-cell{aspect-ratio:1;border-radius:9px;border:1px solid var(--line);display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:2px;cursor:default;position:relative;transition:transform .12s,box-shadow .12s}
.cal-cell .dnum{font-size:10px;color:var(--dim);font-family:'IBM Plex Mono',monospace;line-height:1}
.cal-cell .cp{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;line-height:1.1;margin-top:2px}
.cal-cell.has{cursor:pointer;color:#0d2a4d}
.cal-cell.has:hover{transform:translateY(-2px);box-shadow:var(--shadow);z-index:2}
.cal-cell.empty{background:#f4f7fc;opacity:.55}
.cal-cell.blank{border:0;background:transparent}
.cal-cell.sel{outline:2px solid var(--brand);outline-offset:1px;box-shadow:0 6px 16px -6px rgba(59,110,245,.6)}
.cal-cell.cheapest:after{content:"★";position:absolute;top:2px;right:4px;font-size:9px;color:var(--buy)}

/* trip result cards */
.trips{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,290px),1fr));gap:14px;margin-top:16px}
.trip{background:#fff;border:1px solid var(--line);border-radius:16px;padding:15px 16px;box-shadow:var(--shadow);
  display:flex;flex-direction:column;gap:9px;transition:transform .2s,box-shadow .2s}
.trip:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.trip .th{display:flex;align-items:center;gap:10px}
.trip .rt{font-weight:700;font-size:14px;letter-spacing:-.2px}
.trip .pr{margin-left:auto;font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:17px;letter-spacing:-.5px}
.trip .dt{font-size:12px;color:var(--muted);font-family:'IBM Plex Mono',monospace}
.trip .mt{display:flex;gap:6px;flex-wrap:wrap}
.trip .mt span{font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--muted);background:#eef3fd;border-radius:7px;padding:2px 7px}
.trip .mt span.ns{background:var(--buy-bg);color:var(--buy)}
.trip .mt span.whole{background:rgba(18,176,124,.13);color:#0f7a52;font-weight:600}
.trip .mt span.leg{background:rgba(232,144,42,.12);color:#9a6418}
.trip .go{display:flex;align-items:center;gap:10px;margin-top:2px}
.trip .go .btnbook{padding:8px 13px;font-size:12px}
.trip .go .cmp{font-size:11px;color:var(--dim)}
.trip .go .cmp a{color:var(--muted);text-decoration:underline;text-underline-offset:2px}
.finder-empty{text-align:center;color:var(--muted);padding:34px 20px;font-size:13.5px}
.morebtn{display:block;margin:16px auto 2px;background:#fff;border:1px solid var(--line2);border-radius:11px;
  padding:9px 18px;font-family:'IBM Plex Mono',monospace;font-size:12.5px;color:var(--brand);cursor:pointer;box-shadow:var(--shadow)}
.morebtn:hover{box-shadow:var(--shadow-lg)}
/* book link inside fare tables */
.bookrow{display:inline-flex;align-items:center;gap:5px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--brand);white-space:nowrap}
.bookrow svg{width:12px;height:12px;stroke:var(--brand);fill:none;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}
.bookrow:hover{text-decoration:underline}
@media(max-width:560px){.cal-months{grid-template-columns:1fr}}

/* ---- bento command center: live market pulse fused with the headline stats ---- */
.bento{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:34px;
  grid-auto-rows:minmax(102px,auto);grid-auto-flow:row dense}
.btile{position:relative;overflow:hidden;background:var(--card);border:1px solid var(--line);
  border-radius:18px;padding:15px 16px;box-shadow:var(--shadow);display:flex;flex-direction:column;
  transition:transform .22s cubic-bezier(.2,.7,.2,1),box-shadow .25s ease}
.btile::after{content:"";position:absolute;inset:0;border-radius:18px;pointer-events:none;opacity:0;transition:opacity .35s;
  background:radial-gradient(280px circle at var(--mx,50%) var(--my,0%),rgba(59,110,245,.13),transparent 55%)}
.btile:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg)}
.btile:hover::after{opacity:1}
.btile .b-ic{width:36px;height:36px;border-radius:11px;display:grid;place-items:center;flex:none;
  background:linear-gradient(135deg,rgba(59,110,245,.14),rgba(122,92,240,.14))}
.btile .b-ic svg{width:18px;height:18px;stroke:var(--brand);fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.btile .b-n{font-family:'IBM Plex Mono',monospace;font-size:25px;font-weight:600;letter-spacing:-1px;
  margin-top:auto;padding-top:12px;display:flex;align-items:baseline;gap:5px;white-space:nowrap}
.btile .b-n .cur{font-size:12px;color:var(--dim);font-weight:500;letter-spacing:0}
.btile .b-l{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.13em;margin-top:5px}
.btile .b-x{color:var(--muted);font-size:12px;margin-top:7px;display:flex;align-items:center;gap:6px;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btile .b-x .av{flex:none}
/* lowest-ever accent tile (the trophy stat) */
.b-lowest{grid-column:span 2;background:linear-gradient(150deg,#ffffff,#ecfaf3);border-color:#cdeede}
.b-lowest .b-ic{background:linear-gradient(135deg,rgba(18,176,124,.20),rgba(15,182,168,.16))}
.b-lowest .b-ic svg{stroke:var(--buy)}
.b-lowest .b-n{color:#0c8f63}
/* spotlight carrier tile: lowest fare + buy/wait call + next-3-months lows */
.b-focus{grid-column:span 2;grid-row:span 2;gap:0;
  background:linear-gradient(155deg,#ffffff,#f4f1ff 58%,#eef3ff);border-color:#ddd6f3}
.b-focus::after{background:radial-gradient(420px circle at var(--mx,70%) var(--my,0%),rgba(122,92,240,.12),transparent 55%)}
.b-focus .bf-head{display:flex;align-items:center;gap:9px}
.b-focus .bf-name{font-weight:700;font-size:14px;letter-spacing:-.2px}
.b-focus .bf-head .sig{margin-left:auto}
.b-focus .bf-price{font-family:'IBM Plex Mono',monospace;font-weight:600;letter-spacing:-1px;
  display:flex;align-items:baseline;gap:6px;margin-top:14px}
.b-focus .bf-price .cur{font-size:12px;color:var(--dim);font-weight:500}
.b-focus .bf-price .v{font-size:33px;color:#5b43c9}
.b-focus .bf-price small{font-size:10px;color:var(--dim);font-weight:400;letter-spacing:.1em;
  text-transform:uppercase;margin-left:3px}
.b-focus .bf-trip{font-size:13px;font-weight:600;color:var(--ink);margin-top:9px}
.b-focus .bf-via{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.b-focus .bf-via span{font-size:11px;color:var(--muted);background:rgba(255,255,255,.7);
  border:1px solid var(--line);border-radius:8px;padding:3px 8px}
.b-focus .bf-metal{color:#127a52!important;background:rgba(18,176,124,.12)!important;border-color:rgba(18,176,124,.3)!important;font-weight:600}
.b-focus .bf-conn{color:#7a5b2e!important;background:rgba(232,144,42,.1)!important;border-color:rgba(232,144,42,.28)!important}
.b-focus .bf-solo{font-size:11.5px;color:var(--muted);margin-top:9px;padding:7px 10px;
  background:rgba(122,92,240,.07);border:1px dashed rgba(122,92,240,.32);border-radius:9px;line-height:1.4}
.b-focus .bf-solo b{color:#5b43c9}
.b-focus .bf-call{font-size:12.5px;color:var(--muted);margin-top:13px;line-height:1.45}
.b-focus .bf-call b{color:var(--ink)}.b-focus .bf-call i{color:var(--dim);font-style:normal}
.b-focus .bf-months{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:13px}
.b-focus .bf-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim)}
.b-focus .mchip{display:flex;flex-direction:column;line-height:1.25;padding:5px 10px;
  background:rgba(255,255,255,.75);border:1px solid var(--line);border-radius:10px}
.b-focus .mchip i{font-style:normal;font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em}
.b-focus .mchip b{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600}
.b-focus .bf-go{margin-top:auto;padding-top:14px}
.b-focus .bf-go .btnbook{width:100%;justify-content:center}
/* live market-pulse hero tile (the centrepiece) */
.b-pulse{grid-column:span 2;grid-row:span 2;flex-direction:row;align-items:center;gap:22px;padding:22px 24px;
  background:linear-gradient(150deg,#ffffff,#f3f7ff 55%,#eef3ff);border-color:var(--line2)}
.b-pulse::after{background:radial-gradient(420px circle at var(--mx,70%) var(--my,0%),rgba(122,92,240,.12),transparent 55%)}
.b-pulse .bp-gauge{flex:none}
.b-pulse .gauge{width:150px;height:150px}
.b-pulse .gn{font-size:42px}
.bp-body{flex:1;min-width:0}
.bp-live{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.16em;font-weight:600;
  color:var(--buy);display:flex;align-items:center;gap:7px}
.bp-live .dot{width:7px;height:7px;border-radius:50%;background:var(--buy);animation:opspulse 1.6s ease-out infinite}
.bp-label{font-size:23px;font-weight:800;letter-spacing:-.5px;margin-top:9px;line-height:1.1}
.bp-note{color:var(--muted);font-size:13px;margin-top:5px;max-width:390px}
.bp-metrics{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
.bp-metrics>div{background:rgba(255,255,255,.7);border:1px solid var(--line);border-radius:12px;padding:8px 12px}
.bp-metrics .k{display:block;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim)}
.bp-metrics .v{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:15px;margin-top:4px;display:block}
.bp-metrics .v small{font-size:10px;color:var(--dim);font-weight:400}
.bp-metrics .v.down{color:var(--buy)}.bp-metrics .v.up{color:var(--wait)}
.bp-metrics .sigmix{display:flex;height:7px;width:128px;border-radius:5px;overflow:hidden;background:#eef2fa;margin-top:7px}
.bp-metrics .sigmix i{display:block;height:100%}
@media(max-width:900px){.bento{grid-template-columns:repeat(2,1fr)}.b-pulse,.b-lowest,.b-focus{grid-column:span 2}.b-pulse{grid-row:span 2}.b-focus{grid-row:auto}}
@media(max-width:560px){
  .bento{gap:10px}
  .btile{border-radius:15px;padding:13px 14px}
  .btile .b-n{font-size:21px}
  .b-pulse{flex-direction:column;text-align:center;align-items:center;gap:14px;grid-row:auto}
  .b-pulse .gauge{width:130px;height:130px}.b-pulse .gn{font-size:34px}
  .bp-note{max-width:none}.bp-metrics{justify-content:center}
}

/* ---- market analytics: pulse gauge, leaderboard, mini-stats ---- */
@property --gp{syntax:'<number>';inherits:false;initial-value:0}
.mkt-hero{display:grid;grid-template-columns:300px 1fr;gap:16px;margin-top:16px}
@media(max-width:820px){.mkt-hero{grid-template-columns:1fr}}
.gauge-panel{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.gauge{--gp:0;--gcol:var(--brand);width:184px;height:184px;border-radius:50%;position:relative;
  background:conic-gradient(var(--gcol) calc(var(--gp)*1%),#e8eef8 0);transition:--gp 1.3s cubic-bezier(.2,.7,.2,1);margin:6px 0 4px}
.gauge::before{content:"";position:absolute;inset:15px;border-radius:50%;background:#fff;box-shadow:inset 0 2px 10px rgba(24,46,92,.06)}
.gauge .gv{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.gauge .gn{font-family:'IBM Plex Mono',monospace;font-size:46px;font-weight:600;letter-spacing:-2px;line-height:1}
.gauge .gn small{font-size:15px;color:var(--dim);letter-spacing:0}
.gauge .gt{font-size:10.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--dim);margin-top:2px}
.gauge-lbl{font-weight:700;font-size:17px;letter-spacing:-.3px;margin-top:8px}
.gauge-note{color:var(--muted);font-size:12.5px;margin-top:4px;max-width:260px}
.pulse-bits{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,120px),1fr));gap:12px;align-content:center}
.pbit{background:#f7faff;border:1px solid var(--line);border-radius:14px;padding:14px 15px}
.pbit .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);font-weight:600}
.pbit .v{font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:600;letter-spacing:-.5px;margin-top:6px;display:flex;align-items:baseline;gap:6px}
.pbit .v small{font-size:12px;color:var(--muted);font-weight:400}
.pbit .s{font-size:11.5px;color:var(--muted);margin-top:3px}
.pbit .v.down{color:var(--buy)}.pbit .v.up{color:var(--wait)}
.sigmix{display:flex;height:9px;border-radius:6px;overflow:hidden;margin-top:9px;background:#eef2fa}
.sigmix i{display:block;height:100%}
.callout{display:inline-flex;align-items:center;gap:8px;background:linear-gradient(100deg,#eef3ff,#f3f0ff);
  border:1px solid var(--line2);border-radius:12px;padding:8px 13px;margin-top:14px;font-size:12.5px;color:var(--muted)}
.callout b{color:var(--ink);font-family:'IBM Plex Mono',monospace}
.lead-list{display:flex;flex-direction:column;gap:10px;margin-top:6px}
.lead-row{display:grid;grid-template-columns:26px 1fr auto;gap:12px;align-items:center;padding:12px 14px;
  background:#f7faff;border:1px solid var(--line);border-radius:14px;transition:transform .18s,box-shadow .18s}
.lead-row:hover{transform:translateX(3px);box-shadow:var(--shadow)}
.lead-row .rk{font-family:'IBM Plex Mono',monospace;font-weight:600;color:var(--dim);font-size:13px;text-align:center}
.lead-row .ld-main{min-width:0}
.lead-row .ld-r{font-weight:700;font-size:13.5px;letter-spacing:-.2px;font-family:'IBM Plex Mono',monospace}
.lead-row .ld-out{color:var(--dim);font-weight:500;font-size:11px}
.lead-row .ld-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:5px}
.lead-row .ld-air{display:flex;align-items:center;gap:7px;margin-top:6px;font-size:12px;color:var(--muted);min-width:0}
.lead-row .ld-air .anm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:130px}
.lead-row .ld-book{margin-top:8px}
.lead-row .ld-p{text-align:right;white-space:nowrap}
.lead-row .ld-p .now{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:16px}
.lead-row .ld-p .was{font-size:11px;color:var(--dim);text-decoration:line-through}
.lead-row .ld-s{font-family:'IBM Plex Mono',monospace;font-size:10.5px;font-weight:600;color:var(--buy);background:var(--buy-bg);border-radius:8px;padding:3px 8px;margin-top:5px;display:inline-block}

/* shared compact chips: route + stops/layover + mini signal -------------------- */
.rchip{display:inline-flex;align-items:center;gap:4px;font-family:'IBM Plex Mono',monospace;font-size:10.5px;font-weight:600;
  color:var(--muted);background:#eef3fd;border:1px solid var(--line);border-radius:7px;padding:2px 7px}
.rchip i{font-style:normal;color:var(--dim)}
.mini{font-family:'IBM Plex Mono',monospace;font-size:10.5px;color:var(--muted);background:#f0f3f9;border-radius:7px;padding:2px 7px}
.mini.ns{background:var(--buy-bg);color:var(--buy);font-weight:600}
.mini.via{background:rgba(122,92,240,.1);color:#5b43c9;font-weight:600}
.sigmini{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:700;border-radius:7px;padding:2px 7px;letter-spacing:.02em}
.sigmini.BUY{background:var(--buy-bg);color:var(--buy)}.sigmini.WAIT{background:var(--wait-bg,#fdf0e1);color:var(--wait)}.sigmini.WATCH{background:#eef1f6;color:#6b7ba0}
.viachip{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#5b43c9;background:rgba(122,92,240,.1);border-radius:6px;padding:2px 6px;margin-left:6px}

/* combined cross-route highlights --------------------------------------------- */
.hlgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:14px;margin-top:16px}
.hlcard{display:flex;gap:13px;background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 17px;box-shadow:var(--shadow);
  transition:transform .22s,box-shadow .22s}
.hlcard:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.hlcard .hl-ic{font-size:22px;line-height:1;flex:none;width:42px;height:42px;display:grid;place-items:center;border-radius:12px;
  background:linear-gradient(135deg,#eef3ff,#f3f0ff);border:1px solid var(--line2)}
.hlcard .hl-body{min-width:0}
.hlcard .hl-t{font-weight:700;font-size:14px;letter-spacing:-.2px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.hlcard .hl-score{font-family:'IBM Plex Mono',monospace;color:var(--brand);font-weight:700}
.hlcard .hl-score small{color:var(--dim);font-weight:500;font-size:10px}
.hlcard .hl-s{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.45}
.hlcard .hl-go{margin-top:9px}

/* "Routes we track" overview --------------------------------------------------- */
.routegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,250px),1fr));gap:14px;margin-top:16px}
.routecard{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 17px;box-shadow:var(--shadow);
  display:flex;flex-direction:column;transition:transform .22s,box-shadow .22s}
.routecard:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.routecard.collecting{background:#f9fbfe;border-style:dashed}
.rc-head{display:flex;align-items:center;gap:10px}
.rc-pin{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:700;color:#5b43c9;background:rgba(122,92,240,.1);
  border-radius:9px;padding:6px 8px;letter-spacing:.04em}
.rc-cities{display:flex;flex-direction:column;line-height:1.15;min-width:0}
.rc-cities b{font-size:14px;letter-spacing:-.2px}.rc-cities span{font-size:11.5px;color:var(--muted)}
.rc-head .sigmini{margin-left:auto}
.rc-price{margin-top:13px;font-family:'IBM Plex Mono',monospace;display:flex;align-items:baseline;gap:5px}
.rc-price .cur{font-size:11px;color:var(--dim);font-weight:500}.rc-price b{font-size:25px;font-weight:600;letter-spacing:-.5px;color:var(--buy)}
.rc-price small{font-size:10px;color:var(--dim);letter-spacing:.06em;margin-left:2px}
.rc-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.rc-trip{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-top:10px;flex-wrap:wrap}
.rc-go{display:flex;align-items:center;gap:12px;margin-top:auto;padding-top:14px}
.rc-go .btnbook{flex:none}
.rc-explore{font-size:12px;color:var(--brand);font-weight:600;text-decoration:none}.rc-explore:hover{text-decoration:underline}
.rc-collect{display:flex;align-items:center;gap:9px;margin-top:14px;font-weight:600;font-size:13.5px;color:var(--muted)}
.rc-note{font-size:11.5px;color:var(--dim);margin-top:6px;line-height:1.4}
.spin{width:13px;height:13px;border:2px solid var(--line2);border-top-color:var(--brand);border-radius:50%;display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* signal + insight cards now lead with dates; route is a secondary chip --------- */
.rec .rec-sub{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:3px}
.rec .rec-trip{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:8px;font-size:12.5px;color:var(--muted)}
.rec .rec-trip .anm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px}
.rec .dates.lead{font-weight:700;font-size:16px;letter-spacing:-.3px;color:var(--ink);font-family:'IBM Plex Mono',monospace}
.wc-trip{display:flex;align-items:center;gap:7px;font-size:12.5px;font-family:'IBM Plex Mono',monospace}
@media(max-width:560px){.lead-row .ld-air .anm{max-width:40vw}}

/* ---- global mobile polish: reclaim width, tighten dense components ---- */
@media(max-width:560px){
  .wrap{padding:0 14px 70px}
  .nav .row{padding:11px 14px}
  .panel{padding:16px}
  .finder{padding:16px 14px}
  .ops{padding:16px 15px}
  .icard,.rec,.bookcard,.hlcard,.routecard{padding:15px}
  .hlgrid,.routegrid{gap:11px}
  .hlcard .hl-ic{width:38px;height:38px;font-size:20px}
  .rc-price b{font-size:23px}
  .lead-row{grid-template-columns:22px 1fr auto;gap:10px;padding:11px 12px}
  .section{margin:48px 0 4px}
  .section h2{font-size:19px}
  .gauge{width:160px;height:160px}
  .deal{padding:20px}
  .deal .pricebig .v{font-size:40px}
  /* denser fare calendar so a month fits a phone comfortably */
  .cal-grid{gap:3px}
  .cal-cell{border-radius:7px}
  .cal-cell .dnum{font-size:8.5px}
  .cal-cell .cp{font-size:9.5px;margin-top:1px}
  .cal-cell.cheapest:after{font-size:8px;top:1px;right:3px}
  .cal-legend{font-size:11px;gap:6px}
}
@media(max-width:380px){
  .cal-cell .cp{font-size:8.5px}
  .stat .n{font-size:22px}
}
</style></head><body>

<div class="aurora"><span class="blob b1"></span><span class="blob b2"></span></div>

<nav class="nav"><div class="row">
  <div class="brand"><span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"
    stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v3M9 8h6l1.5 11h-9L9 8zM7.5 19h9M10 8l.5-3h3l.5 3"/></svg></span>Faro</div>
  <div class="links"><a href="#highlights">Highlights</a><a href="#routes">Routes</a><a href="#finder">Find a trip</a><a href="#market">Market</a><a href="#deals">Signals</a><a href="#insights">Insights</a><a href="#fares">Fares</a></div>
  <div class="ctx"><span class="pulse"></span><span id="clock">live</span><span id="navwx"></span></div>
</div></nav>
<div class="routebar" id="routebar"></div>

<div class="wrap">
  <header class="hero">
    <div class="reveal">
      <div class="eyebrow" id="eyebrow">SMART FARE TIMING</div>
      <h1>Book your flight<br>at the <span class="grad">perfect moment.</span></h1>
      <div class="lead" id="lead">Faro tracks fares daily and tells you whether to book now or wait for a
        better price — showing the airline, stops, flight time and booking links for every option.</div>
      <div class="chips" id="hchips"></div>
    </div>
    <div class="reveal">
      <div class="dealwrap"><div class="deal" id="deal"></div></div>
    </div>
  </header>

  <div class="bento" id="bento"></div>
  <div id="scopeEmpty" style="display:none"></div>
  <div id="body"></div>

  <div class="foot reveal">
    Faro checks fares several times a day and refreshes this page automatically. Prices sourced from Google Flights; airline logos
    from <a href="https://www.air-hex.com">avs.io</a>, live weather from
    <a href="https://open-meteo.com">Open-Meteo</a> and exchange rates from <a href="https://www.exchangerate-api.com">ExchangeRate-API</a>.
    The buy/wait signals are generated by a machine-learning model trained on each route's own fare history.
    Informational only — always confirm the live fare before booking. Raw data is available in <code>data/</code>.
    <div class="affdisc" id="affdisc"></div>
    <div class="author"><div class="ring">YS</div>
      <div><div class="who">Built &amp; maintained by</div><div class="nm">Yasas Sri Wickramasinghe</div></div></div>
  </div>
</div>

<script>
const D = ''' + data + r''';
const CUR = D.currency || 'NZD';
const fmt = (n,c)=> (c||CUR)+' '+Math.round(n).toLocaleString();
const fmtv = n => Math.round(n).toLocaleString();
const dur = m => m ? Math.floor(m/60)+'h '+(m%60).toString().padStart(2,'0')+'m' : '—';
const stops = s => s===0 ? 'Nonstop' : s+' stop'+(s>1?'s':'');
const esc = s => (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const palette=['#3b6ef5','#7a5cf0','#0fb6a8','#e8902a','#e0567d','#5a6b86','#12b07c','#9a6cff'];
const acolor = s => palette[Math.abs([...(s||'?')].reduce((a,c)=>a*31+c.charCodeAt(0)|0,7))%palette.length];
const initials = s => (s||'?').replace(/[^A-Za-z ]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'?';
const MM=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

/* real airline logo over an initials fallback */
function avatar(name,iata,size){
  size=size||28; const fs=Math.round(size*0.4);
  const img = iata ? '<img src="https://pics.avs.io/120/120/'+iata+'.png" alt="'+esc(name)+
    '" loading="lazy" onerror="this.remove()">' : '';
  return '<span class="av" style="width:'+size+'px;height:'+size+'px;background:'+acolor(name)+';font-size:'+fs+'px">'+
    '<span class="ini">'+initials(name)+'</span>'+img+'</span>';
}

function parseItin(s){
  const m=(s||'').match(/^([A-Z]{3})-([A-Z]{3}) (\d{4})-(\d{2})-(\d{2}) -> (\d{4})-(\d{2})-(\d{2})$/);
  if(!m) return {title:s||'',dates:'',nights:null};
  const cn=c=>(D.cities&&D.cities[c])||c;
  const d1=new Date(+m[3],+m[4]-1,+m[5]), d2=new Date(+m[6],+m[7]-1,+m[8]);
  const f=d=>d.getDate()+' '+MM[d.getMonth()];
  return {title:cn(m[1])+' → '+cn(m[2]), dates:f(d1)+' – '+f(d2)+' '+d2.getFullYear(), nights:Math.round((d2-d1)/86400000),
    o:m[1], d:m[2]};
}

/* ---- shared itinerary identity (kills the repeated route name) ----
   On a single corridor the route label is pure repetition. So everywhere we lead
   with what actually distinguishes a trip -- its DATES + nights -- and render the
   route as a small, secondary chip (only really needed once >1 corridor). */
const CNm=c=>(D.cities&&D.cities[c])||c;
const NROUTES=()=>((D.stats&&D.stats.routes)||1);                 // corridors with live data
const NTRACKED=()=>((D.stats&&D.stats.routes_tracked)||NROUTES());// corridors we track
// The route chip only earns its place once >1 corridor has data; on a single
// corridor it's pure repetition, so we suppress it and let the dates lead.
function routeChip(o,d){if(!o||NROUTES()<=1)return '';return '<span class="rchip">'+esc(CNm(o))+'<i>→</i>'+esc(CNm(d))+'</span>';}
/* compact stops + layover ("where the stop is") badge set */
function stopsVia(stops,via,ns){
  if(ns||stops===0)return '<span class="mini ns">Nonstop</span>';
  let s=(stops!=null)?'<span class="mini">'+stops+' stop'+(stops===1?'':'s')+'</span>':'';
  if(via)s+='<span class="mini via">via '+esc(via)+'</span>';
  return s||'';
}
function signalPill(itin){const r=(D.recs||[]).find(x=>x.itinerary===itin);
  if(!r)return '';return '<span class="sigmini '+r.signal+'">'+r.signal+(r.confidence!=null?' '+r.confidence+'%':'')+'</span>';}

/* ===== route scope: the sticky corridor switcher that focuses the page =====
   The site tracks several corridors (CHC↔CMB plus the newer AKL routes). Picking
   a corridor focuses the WHOLE page on it — and critically, route-specific
   sections from OTHER corridors disappear, so you never see CHC↔CMB charts under
   the Auckland→Delhi tab. Every section declares who it belongs to via
   `.sscope[data-for]`:
     global   — always shown (route picker, scraper status, global stats)
     all      — only on the "All routes" overview (cross-route summaries)
     flagship — the deepest-history corridor's deep-dive (also shown on All)
     data     — any corridor that actually has fares (also shown on All)
     <route>  — exactly that corridor (also shown on All)
   Per-trip card grids (signals/insights/fares) filter card-by-card via .rsec.
   A corridor that's still collecting shows a dedicated empty panel — never
   another route's data. */
const routeKey=itin=>{const p=parseItin(itin);return (p.o&&p.d)?p.o+'-'+p.d:'';};
const FLAGSHIP=(D.primary&&D.primary.origin&&D.primary.dest)?
  D.primary.origin.code+'-'+D.primary.dest.code:'';
let SCOPE='all';
function routeList(){
  return (D.routes_overview||[]).map(c=>({route:c.o+'-'+c.d,from:c.from,to:c.to,
    has:c.has_data,min:c.min,flag:(c.o+'-'+c.d)===FLAGSHIP}));
}
function routeInfo(key){return routeList().find(c=>c.route===key)||null;}
function routeHasData(key){const r=routeInfo(key);return !!(r&&r.has);}
/* Does a section tagged `forV` show under the current SCOPE? */
function scopeShows(forV){
  if(forV==='global')return true;
  if(SCOPE==='all')return forV!=='hidden';           // overview shows everything
  if(forV==='all')return false;                       // cross-route-only
  if(forV==='flagship')return SCOPE===FLAGSHIP;
  if(forV==='data')return routeHasData(SCOPE);
  return forV===SCOPE;                                // an explicit route key
}
function renderRouteSwitcher(){
  const bar=document.getElementById('routebar');if(!bar)return;
  const R=routeList();
  if(R.length<=1){bar.style.display='none';return;}   // pointless with one corridor
  const pill=(key,inner,sub,cls)=>'<button class="rpill'+(cls||'')+'" data-route="'+esc(key)+'">'+
    '<span class="rp-main">'+inner+'</span><span class="rp-sub">'+sub+'</span></button>';
  let html=pill('all','All routes',R.length+' corridors','');
  R.forEach(c=>{
    const label=esc(c.from)+' <i>→</i> '+esc(c.to)+(c.flag?'<span class="rp-flag">★</span>':'');
    html+=pill(c.route,label,(c.has?'from '+CUR+' '+fmtv(c.min):'collecting…'),(c.has?'':' collecting'));
  });
  bar.innerHTML='<div class="rb-inner"><span class="rb-lbl">Focus a route</span>'+
    '<div class="rb-pills">'+html+'</div></div>';
  bar.querySelectorAll('.rpill').forEach(b=>b.addEventListener('click',()=>setScope(b.dataset.route)));
}
function setScope(route){
  SCOPE=route||'all';
  const bar=document.getElementById('routebar');
  if(bar)bar.querySelectorAll('.rpill').forEach(b=>b.classList.toggle('on',b.dataset.route===SCOPE));
  document.body.setAttribute('data-scope',SCOPE);
  // Section-level scoping: every .sscope shows/hides by who it belongs to.
  document.querySelectorAll('.sscope').forEach(el=>{
    el.style.display=scopeShows(el.getAttribute('data-for')||'global')?'':'none';});
  // Per-trip card grids: hide non-matching cards, then hide the section if empty.
  document.querySelectorAll('.rsec').forEach(sec=>{
    let vis=0;
    sec.querySelectorAll('[data-route]').forEach(c=>{
      const show=(SCOPE==='all'||c.getAttribute('data-route')===SCOPE);
      c.style.display=show?'':'none';if(show)vis++;});
    sec.style.display=vis?'':'none';
  });
  updateScopeEmpty();
  if(SCOPE!=='all'&&window.__finderPickRoute)window.__finderPickRoute(SCOPE);
  if(SCOPE!=='all'){const t=document.getElementById('routebar');if(t&&t.scrollIntoView)t.scrollIntoView({block:'nearest'});}
}
/* When a still-collecting corridor is focused, show a friendly panel instead of
   any other route's data (the whole point of scoping). */
function updateScopeEmpty(){
  const el=document.getElementById('scopeEmpty');if(!el)return;
  const r=routeInfo(SCOPE);
  if(SCOPE==='all'||!r||r.has){el.style.display='none';el.innerHTML='';return;}
  const flag=routeInfo(FLAGSHIP);
  el.style.display='';
  el.innerHTML='<div class="collectpanel reveal in"><div class="cp-orbit"><span class="cp-plane">✈</span></div>'+
    '<div class="cp-body"><div class="cp-eyebrow">NEW ROUTE</div>'+
    '<h2>'+esc(r.from)+' → '+esc(r.to)+' is warming up</h2>'+
    '<p>Faro just started watching this corridor. Prices are being collected now — '+
    'expect buy/wait signals, fare forecasts and the cheapest-day calendar to appear '+
    'here within a few days.</p>'+
    '<div class="cp-actions">'+(flag?'<button class="btnbook" id="cp-toflag">'+_EXT+'Explore '+esc(flag.from)+' → '+esc(flag.to)+'</button>':'')+
    '<button class="cp-all" id="cp-all">View all routes</button></div></div></div>';
  const f=document.getElementById('cp-toflag');if(f)f.addEventListener('click',()=>setScope(FLAGSHIP));
  const a=document.getElementById('cp-all');if(a)a.addEventListener('click',()=>setScope('all'));
}

/* ===== combined cross-route highlights (the hero hook) ===== */
const HLI={gem:'💎',pulse:'📈',clock:'⏳',plane:'✈️',cal:'🗓️',length:'🗓️'};
function renderHighlights(){const H=D.highlights||[];if(!H.length)return;
  const card=h=>{const go=h.itin?bookRowLink(h.itin,'See fare ↗'):'';
    const sc=(h.score!=null)?'<span class="hl-score">'+h.score+'<small>/100</small></span>':'';
    return '<div class="hlcard reveal"><div class="hl-ic">'+(HLI[h.icon]||'✦')+'</div>'+
      '<div class="hl-body"><div class="hl-t">'+esc(h.title)+sc+'</div>'+
      '<div class="hl-s">'+esc(h.sub)+'</div>'+(go?'<div class="hl-go">'+go+'</div>':'')+'</div></div>';};
  add('<section class="sscope" data-for="all"><div class="section reveal" id="highlights"><h2>Today across every route</h2>'+
    '<span class="hint">highlights from '+NTRACKED()+' tracked routes</span></div>'+
    '<div class="hlgrid reveal">'+H.map(card).join('')+'</div></section>');}

/* ===== every supported corridor at a glance (incl. ones still collecting) ===== */
function renderRoutes(){const R=D.routes_overview||[];if(R.length<1)return;
  const card=c=>{
    const head='<div class="rc-head">'+avatarPin(c.o)+'<div class="rc-cities"><b>'+esc(c.from)+'</b><span>→ '+esc(c.to)+'</span></div>'+
      (c.has_data?signalPill(c.o+'-'+c.d+' '+(c.dep||'')+' -> '+(c.ret||'')):'')+'</div>';
    if(!c.has_data){
      return '<div class="routecard collecting reveal">'+head+
        '<div class="rc-collect"><span class="spin"></span>Collecting fares</div>'+
        '<div class="rc-note">Recently added — prices and signals will appear within a few days.</div></div>';}
    const itin=c.o+'-'+c.d+' '+c.dep+' -> '+c.ret;
    return '<div class="routecard reveal">'+head+
      '<div class="rc-price"><span class="cur">'+CUR+'</span><b>'+fmtv(c.min)+'</b><small>cheapest return</small></div>'+
      '<div class="rc-meta">'+stopsVia(c.stops,c.via,c.nonstop)+
        (c.carriers?'<span class="mini">'+c.carriers+' airline'+(c.carriers===1?'':'s')+'</span>':'')+
        (c.dtd!=null?'<span class="mini">'+c.dtd+'d out</span>':'')+'</div>'+
      '<div class="rc-trip">'+(c.airline?avatar(c.airline,c.iata,18)+esc(c.airline)+' · ':'')+fdshort(c.dep)+' – '+fdshort(c.ret)+(c.len!=null?' · '+c.len+'n':'')+'</div>'+
      '<div class="rc-go">'+bookBtn(itin,'Book')+'<a class="rc-explore" href="#finder" data-route="'+esc(c.o+'-'+c.d)+'">Explore dates →</a></div></div>';};
  add('<section class="sscope" data-for="all"><div class="section reveal" id="routes"><h2>Routes we track</h2>'+
    '<span class="hint">New Zealand ↔ Sri Lanka &amp; India · tap a route to explore</span></div>'+
    '<div class="routegrid reveal">'+R.map(card).join('')+'</div></section>');
  // "Explore dates" focuses the whole page on this corridor (and the finder).
  document.querySelectorAll('.rc-explore').forEach(a=>a.addEventListener('click',()=>{
    setScope(a.dataset.route);
    if(window.__finderPickRoute)window.__finderPickRoute(a.dataset.route);}));}
function avatarPin(code){return '<span class="rc-pin">'+esc((code||'').slice(0,3))+'</span>';}

/* ---- affiliate booking deep-links, built from the route + dates ----
   Every CTA on the page is a deep link into a flight search for that exact
   route+dates. Providers + affiliate markers come from config (monetization:),
   embedded as D.monetization, so adding your Travelpayouts marker turns every
   "Book" button into an earning link with zero code changes. With no marker the
   links still work (untracked), so the dashboard is functional out of the box. */
const MON = D.monetization || {enabled:false, providers:[], google_flights:true};
function _googleUrl(o,d,dep,ret){
  return 'https://www.google.com/travel/flights?q='+encodeURIComponent('Flights from '+o+' to '+d+' on '+dep+' through '+ret);}
function bookLinks(itin){
  const m=(itin||'').match(/^([A-Z]{3})-([A-Z]{3}) (\d{4})-(\d{2})-(\d{2}) -> (\d{4})-(\d{2})-(\d{2})$/);
  if(!m) return null;
  const o=m[1],d=m[2],dep=m[3]+'-'+m[4]+'-'+m[5],ret=m[6]+'-'+m[7]+'-'+m[8];
  // Placeholder bag for the provider URL templates.
  const ctx={o:o,d:d,ol:o.toLowerCase(),dl:d.toLowerCase(),dep:dep,ret:ret,
    depDDMM:m[5]+m[4],retDDMM:m[8]+m[7],depYMD:m[3].slice(2)+m[4]+m[5],retYMD:m[6].slice(2)+m[7]+m[8],
    adults:MON.adults||1,sub:MON.sub||'',cur:MON.cur||'nzd'};
  const fill=(tpl,mk)=>tpl.replace(/\{(\w+)\}/g,(_,k)=> k==='m' ? encodeURIComponent(mk||'') :
    (ctx[k]!=null?encodeURIComponent(ctx[k]):''));
  const provs=(MON.providers||[]).map(p=>({id:p.id,name:p.name,primary:!!p.primary,href:fill(p.url,p.m)}));
  const google=_googleUrl(o,d,dep,ret);
  let primary=provs.find(p=>p.primary)||provs[0]||null;
  if(!primary){primary={id:'google',name:'Google Flights',href:google};}
  // The compare row = every provider except the primary, plus a neutral Google link.
  const compare=provs.filter(p=>p!==primary);
  if(MON.google_flights!==false) compare.push({id:'google',name:'Google Flights',href:google});
  return {o:o,d:d,dep:dep,ret:ret,primary:primary,compare:compare,google:google};
}
const _EXT='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 7h9v9M17 7 7 17"/></svg>';
function bookBtn(itin,label){const L=bookLinks(itin);if(!L)return '';
  return '<a class="btnbook" href="'+L.primary.href+'" target="_blank" rel="noopener sponsored nofollow">'+_EXT+esc(label||('Book on '+L.primary.name))+'</a>';}
function bookBar(itin){const L=bookLinks(itin);if(!L)return '';
  const alts=L.compare.map(p=>'<a href="'+p.href+'" target="_blank" rel="noopener sponsored nofollow">'+esc(p.name)+'</a>').join(' · ');
  return '<div class="booklinks">'+bookBtn(itin,'Book on '+L.primary.name)+
    (alts?'<span class="altbook">or compare on '+alts+'</span>':'')+'</div>';}
/* compact inline "Book ↗" link for table rows / cards -- every fare we show
   gets a direct, deep-linked way to open it (primary affiliate provider). */
function bookRowLink(itin,label){const L=bookLinks(itin);if(!L)return '';
  return '<a class="bookrow" href="'+L.primary.href+'" target="_blank" rel="noopener sponsored nofollow">'+_EXT+esc(label||'Book')+'</a>';}

/* ---- clocks ---- */
const dest=(D.primary&&D.primary.dest)||null, orig=(D.primary&&D.primary.origin)||null;
function clock(){const el=document.getElementById('clock');if(!el)return;
  if(dest&&dest.tz){try{el.textContent=new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:dest.tz}).format(new Date())+' '+dest.name;}
    catch(e){el.textContent=new Date().toUTCString().slice(17,25)+' UTC';}}
  else el.textContent=new Date().toUTCString().slice(17,25)+' UTC';setTimeout(clock,1000);}
clock();

/* ---- live weather (Open-Meteo) ---- */
const WX={0:['☀️','Clear'],1:['🌤️','Mostly clear'],2:['⛅','Partly cloudy'],3:['☁️','Cloudy'],45:['🌫️','Fog'],48:['🌫️','Fog'],
  51:['🌦️','Drizzle'],53:['🌦️','Drizzle'],55:['🌦️','Drizzle'],61:['🌧️','Rain'],63:['🌧️','Rain'],65:['🌧️','Heavy rain'],
  71:['🌨️','Snow'],80:['🌦️','Showers'],81:['🌧️','Showers'],82:['⛈️','Storms'],95:['⛈️','Thunderstorm'],96:['⛈️','Thunderstorm'],99:['⛈️','Thunderstorm']};
let weather=null;
async function loadWeather(){if(!dest||dest.lat==null)return;
  try{const r=await fetch('https://api.open-meteo.com/v1/forecast?latitude='+dest.lat+'&longitude='+dest.lon+'&current=temperature_2m,weather_code');
    const j=await r.json(),c=j.current,w=WX[c.weather_code]||['🌡️',''];weather={t:Math.round(c.temperature_2m),ico:w[0],desc:w[1]};}catch(e){}
  const nav=document.getElementById('navwx');if(weather&&nav)nav.textContent='· '+weather.ico+' '+weather.t+'°C';renderHeroChips();}

/* ---- live currency (ExchangeRate-API) ---- */
let rates=null;
async function loadRates(){try{const r=await fetch('https://open.er-api.com/v6/latest/'+CUR);const j=await r.json();
  if(j&&j.result==='success')rates=j.rates;}catch(e){}renderDeal();}
function convert(a,to){return rates&&rates[to]?a*rates[to]:null;}
function fxLine(a){if(!rates)return '';return ['USD','AUD','GBP','EUR','LKR','INR'].filter(c=>c!==CUR&&rates[c]).slice(0,4)
  .map(c=>'<span>'+fmt(convert(a,c),c)+'</span>').join('');}

/* ---- hero chips ---- */
function renderHeroChips(){const el=document.getElementById('hchips');if(!el)return;const S=D.stats,ch=[];
  if(weather)ch.push(['<span class="ico">'+weather.ico+'</span>','<b>'+weather.t+'°C</b><small>'+esc(weather.desc)+' in '+esc(dest.name)+'</small>']);
  if(dest&&dest.tz)ch.push(['<span class="ico">🕐</span>','<b id="lt"></b><small>local time, '+esc(dest.name)+'</small>']);
  if(S&&S.airlines){const rt=S.routes_tracked||S.routes||0;ch.push(['<span class="ico">✈️</span>','<b>'+S.airlines+' airlines</b><small>'+rt+' route'+(rt===1?'':'s')+' tracked</small>']);}
  el.innerHTML=ch.map(c=>'<div class="lchip">'+c[0]+'<div>'+c[1]+'</div></div>').join('');if(dest&&dest.tz)ltick();}
function ltick(){const el=document.getElementById('lt');if(!el)return;
  try{el.textContent=new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:dest.tz}).format(new Date());}catch(e){}setTimeout(ltick,20000);}

/* ---- hero deal card ---- */
function renderDeal(){const el=document.getElementById('deal');if(!el)return;
  const best=(D.insights&&D.insights[0])||null;
  if(!best){el.innerHTML='<div class="lbl">Best deal</div><div class="route" style="margin-top:10px">Collecting fares…</div>'+
    '<div class="dates">The first deal appears within a few daily scans.</div>';return;}
  const pi=parseItin(best.itinerary),rec=(D.recs||[]).find(r=>r.itinerary===best.itinerary),sig=rec?rec.signal:'WATCH';
  const save=Math.max(0,Math.round((best.median||best.min)-best.min));
  const sub=rec&&rec.signal==='BUY'?'Good time to book':rec&&rec.signal==='WAIT'?'Prices may still fall':'Worth watching';
  el.innerHTML='<div class="tagrow"><span class="lbl">Best deal right now</span><span class="sig '+sig+'" style="margin-left:auto">'+sig+'</span></div>'+
    '<div class="route">'+(best.cheapest_airline?avatar(best.cheapest_airline,best.cheapest_iata,30):'')+esc(pi.title)+'</div>'+
    '<div class="dates">'+esc(pi.dates)+(pi.nights?' · '+pi.nights+' nights':'')+'</div>'+
    '<div class="pricebig"><span class="cur">'+CUR+'</span><span class="v">'+fmtv(best.min)+'</span><small>cheapest return</small></div>'+
    '<div class="dates" style="margin-top:8px">'+esc(sub)+(best.cheapest_airline?' · '+esc(best.cheapest_airline):'')+(save>0?' · save ~'+fmt(save)+' vs typical':'')+'</div>'+
    '<div class="meta2">'+(best.fastest?'<span class="tag">⏱ '+dur(best.fastest)+' fastest</span>':'')+
      (best.nonstop?'<span class="tag b">Nonstop available</span>':'<span class="tag">connections only</span>')+
      (best.days_to_departure!=null?'<span class="tag">'+best.days_to_departure+' days to go</span>':'')+'</div>'+
    '<div style="margin-top:16px;transform:translateZ(30px)">'+bookBtn(best.itinerary,'Book this trip')+'</div>'+
    '<div class="fx">'+fxLine(best.min)+'</div>';}

/* ---- stat cards ---- */
const ICON={
  records:'<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18M7 14h4"/>',
  routes:'<path d="M3 20l7-3 4 1 7-9M14 18l3 3M17 4l3 3"/><circle cx="5" cy="19" r="1.4"/>',
  airlines:'<path d="M20.6 9.4 12 4 3.4 9.4 12 14.8z"/><path d="M5 12v4.5L12 21l7-4.5V12"/>',
  scans:'<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4M8 14h.01M12 14h.01M16 14h.01"/>',
  avg:'<path d="M4 19V5M4 19h16M8 16v-5M13 16V8M18 16v-7"/>',
  fast:'<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  gem:'<path d="M6 3h12l3 6-9 12L3 9z"/><path d="M3 9h18M9 3 7 9l5 12 5-12-2-6"/>'};
const S=D.stats;
document.getElementById('eyebrow').textContent=D.primary?(D.primary.origin.name+' → '+D.primary.dest.name+' · SMART FARE TIMING'):'SMART FARE TIMING';
if(D.primary){const nR=(D.stats&&D.stats.routes_tracked)||((D.routes_overview||[]).length);
  const more=nR>1?' — one of <b>'+nR+' routes</b> it tracks across New Zealand ↔ Sri Lanka &amp; India':'';
  document.getElementById('lead').innerHTML='Faro tracks fares for <b>'+esc(D.primary.origin.name)+' → '+esc(D.primary.dest.name)+
  '</b> daily'+more+'. See every airline, compare stops and flight times, and get a clear buy or wait signal — so you never overpay.';}
renderBento();

/* Spotlight-carrier tile (replaces the old "lowest ever" stat): the featured
   airline's lowest live fare, the decision engine's buy/wait call + forecast for
   that exact fare, and its cheapest fare in each of the next few months. Returns
   '' when there's no focus data, so renderBento can fall back to the old tile. */
function focusTile(){
  const F=D.focus; if(!F||!F.cheapest)return '';
  const c=F.cheapest,pi=parseItin(c.itinerary),sig=c.signal||'WATCH';
  const verdict=sig==='BUY'?'Book now':sig==='WAIT'?'Worth waiting':'Watching';
  const bits=[];
  if(sig==='WAIT'&&c.predicted_low!=null)bits.push('model low ~'+fmt(c.predicted_low));
  if(c.confidence!=null)bits.push(c.confidence+'% confidence');
  const extra=bits.length?' <i>('+bits.join(' · ')+')</i>':'';
  const stp=c.nonstop?'Nonstop':(c.stops!=null?stops(c.stops):'');
  // Does the headline fare put the carrier on the WHOLE trip or just one leg?
  const tripTag=c.solo?'<span class="bf-metal">whole trip on '+esc(F.name)+'</span>'
    :'<span class="bf-conn">'+esc(F.name)+' flies one leg</span>';
  const months=(F.upcoming||[]).map(u=>{const m=String(u.dep||'').match(/-(\d{2})-/);
    return '<span class="mchip"><i>'+(m?MM[+m[1]-1]:'')+'</i><b>'+fmt(u.price)+'</b></span>';}).join('');
  // Whole-trip-on-own-metal fare, when it differs from the (possibly connection)
  // headline — directly answers "track <carrier> flying the entire trip".
  const wt=F.whole_trip;
  const wtLine=(wt&&!c.solo)?'<div class="bf-solo">🛫 Cheapest <b>'+esc(F.name)+
    '</b>-operated whole trip: <b>'+fmt(wt.price)+'</b> · '+fdshort(wt.dep)+
    (wt.len!=null?' · '+wt.len+' nights':'')+'</div>':'';
  return '<div class="btile b-focus reveal">'+
    '<div class="bf-head">'+avatar(F.name,F.iata,24)+'<span class="bf-name">'+esc(F.name)+'</span>'+
      '<span class="sig '+sig+'">'+sig+'</span></div>'+
    '<div class="bf-price"><span class="cur">'+CUR+'</span><span class="v">'+fmtv(c.price)+'</span>'+
      '<small>lowest fare now</small></div>'+
    '<div class="bf-trip">'+esc(pi.title)+' · '+esc(pi.dates)+(c.len!=null?' · '+c.len+' nights':'')+'</div>'+
    '<div class="bf-via">'+(stp?'<span>'+stp+'</span>':'')+(c.fastest?'<span>⏱ '+dur(c.fastest)+'</span>':'')+
      tripTag+'</div>'+
    wtLine+
    '<div class="bf-call"><b>'+verdict+'</b> — '+esc(c.reason||'Tracking this fare.')+extra+'</div>'+
    (months?'<div class="bf-months"><span class="bf-lbl">Lowest by month</span>'+months+'</div>':'')+
    '<div class="bf-go">'+bookBtn(c.itinerary,'Book this '+F.name+' fare')+'</div></div>';
}

/* The bento command center: the live "buy index" market pulse fused with the
   headline dataset stats in one clean, modern grid. Falls back gracefully to
   just the stats when there's no market read yet. */
function renderBento(){
  const el=document.getElementById('bento'); if(!el)return;
  const P=(D.ai&&D.ai.market&&D.ai.market.pulse)||null;
  const tile=(icon,label,to,kind,extra,cls)=>{
    const cur=kind==='cur'?'<span class="cur">'+CUR+'</span>':'';
    return '<div class="btile '+(cls||'')+' reveal"><div class="b-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'+ICON[icon]+'</svg></div>'+
      '<div class="b-n">'+cur+'<span class="vn" data-to="'+to+'" data-kind="'+kind+'">0</span></div>'+
      '<div class="b-l">'+label+'</div>'+(extra?'<div class="b-x">'+extra+'</div>':'')+'</div>';
  };
  let h='';
  if(P){
    const col=P.score>=70?'#12b07c':P.score>=56?'#3b6ef5':P.score>=44?'#6b7ba0':P.score>=30?'#e8902a':'#e0567d';
    const tot=Math.max(1,P.buy+P.wait+P.watch),seg=(n,c)=>n?'<i style="width:'+(n/tot*100)+'%;background:'+c+'"></i>':'';
    const mom=P.momentum_pct,momCls=mom==null?'':(mom<0?'down':mom>0?'up':''),momTxt=mom==null?'—':(mom>0?'+':'')+mom+'%';
    h+='<div class="btile b-pulse reveal" style="--gcol:'+col+'">'+
      '<div class="bp-gauge"><div class="gauge" id="pulseGauge" style="--gcol:'+col+'"><div class="gv">'+
        '<div class="gn"><span id="pulseNum">0</span><small>/100</small></div><div class="gt">buy index</div></div></div></div>'+
      '<div class="bp-body">'+
        '<div class="bp-live"><span class="dot"></span>LIVE MARKET</div>'+
        '<div class="bp-label" style="color:'+col+'">'+esc(P.label)+'</div>'+
        '<div class="bp-note">'+esc(P.note)+'</div>'+
        '<div class="bp-metrics">'+
          '<div><span class="k">Fare value</span><span class="v">'+P.value+'<small>/100</small></span></div>'+
          '<div><span class="k">Since last scan</span><span class="v '+momCls+'">'+momTxt+'</span></div>'+
          '<div><span class="k">'+P.buy+' buy · '+P.wait+' wait · '+P.watch+' watch</span><span class="sigmix">'+seg(P.buy,'#12b07c')+seg(P.wait,'#e8902a')+seg(P.watch,'#6b7ba0')+'</span></div>'+
        '</div></div></div>';
  }
  const ft=focusTile();
  if(ft)h+=ft;
  else if(S.cheapest_ever){const ce=S.cheapest_ever;
    h+=tile('gem','Lowest ever',Math.round(ce.price),'cur',
      (ce.airline?avatar(ce.airline,ce.iata,18)+esc(ce.airline):''),'b-lowest');}
  if(S.avg_price)h+=tile('avg','Average fare',Math.round(S.avg_price),'cur');
  h+=tile('records','Fare records',S.total_offers||0,'num');
  h+=tile('routes','Routes tracked',S.routes_tracked||S.routes||0,'num');
  h+=tile('airlines','Airlines seen',S.airlines||0,'num');
  h+=tile('scans','Daily scans',S.scans||0,'num');
  if(S.fastest)h+=tile('fast','Fastest trip',S.fastest,'dur');
  el.innerHTML=h;

  if(P)requestAnimationFrame(()=>{const g=document.getElementById('pulseGauge');if(g)g.style.setProperty('--gp',P.score);
    const n=document.getElementById('pulseNum');if(!n)return;let st=null;
    (function step(ts){st=st||ts;const k=Math.min(1,(ts-st)/1200);n.textContent=Math.round(P.score*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(step);})(performance.now());});

  // pointer-follow glow on each tile (skipped on touch) -- the addictive bit
  if(matchMedia('(hover:hover) and (pointer:fine)').matches)
    el.querySelectorAll('.btile').forEach(t=>t.addEventListener('pointermove',e=>{
      const r=t.getBoundingClientRect();
      t.style.setProperty('--mx',((e.clientX-r.left)/r.width*100)+'%');
      t.style.setProperty('--my',((e.clientY-r.top)/r.height*100)+'%');}));
}

function animateCount(el){if(el.dataset.done)return;el.dataset.done='1';
  const to=+el.dataset.to,kind=el.dataset.kind,sh=t=>kind==='dur'?dur(t):fmtv(t);let st=null;
  function step(ts){st=st||ts;const k=Math.min(1,(ts-st)/1100);el.textContent=sh(to*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(step);}
  requestAnimationFrame(step);}

/* ---- body ---- */
const body=document.getElementById('body');
function add(html){const d=document.createElement('div');d.innerHTML=html;while(d.firstChild)body.appendChild(d.firstChild);}

/* ===== live scraper operations panel ===== */
function fdate(s){if(!s)return '—';const m=String(s).match(/(\d{4})-(\d{2})-(\d{2})/);
  if(!m)return s;const d=new Date(+m[1],+m[2]-1,+m[3]);return d.getDate()+' '+MM[d.getMonth()]+' '+d.getFullYear();}
function fdshort(s){if(!s)return '';const m=String(s).match(/(\d{4})-(\d{2})-(\d{2})/);
  if(!m)return s;return (+m[3])+' '+MM[+m[2]-1];}
function dhms(ms,short){if(ms<0)ms=0;const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),
  mn=Math.floor(s%3600/60),se=s%60;if(d>0)return d+'d '+h+'h';if(h>0)return h+'h '+mn+'m';
  return short?(mn+'m '+String(se).padStart(2,'0')+'s'):(mn+'m '+se+'s');}
function agoTxt(iso){if(!iso)return 'never';const ms=Date.now()-Date.parse(iso);if(ms<0)return 'just now';
  const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),mn=Math.floor(s%3600/60);
  if(d>0)return d+'d '+h+'h ago';if(h>0)return h+'h '+mn+'m ago';if(mn>0)return mn+'m ago';return 'just now';}

function nextTickUTC(cad){const n=new Date();const h=(Math.floor(n.getUTCHours()/cad)+1)*cad;
  const d=new Date(Date.UTC(n.getUTCFullYear(),n.getUTCMonth(),n.getUTCDate(),0,0,0));d.setUTCHours(h);return d;}

function opsState(){const ST=D.status||{};const cad=ST.cadence_hours||6;
  if(!ST.last_scan_iso)return 'due';
  const age=Date.now()-Date.parse(ST.last_scan_iso);const hr=3600000;
  if(age <= (cad+2)*hr)return 'live';            // a run landed within the expected window
  if(age <= (cad*2+2)*hr)return 'due';           // overdue but plausibly between runs
  return 'stale';                                 // hasn't run in a long time
}

function renderOps(){const ST=D.status; if(!ST)return;
  const cad=ST.cadence_hours||6;
  const cell=(k,v,s)=>'<div class="ops-cell"><div class="k">'+k+'</div><div class="v">'+v+'</div>'+(s?'<div class="s">'+s+'</div>':'')+'</div>';
  const L=ST.latest||null;
  const cells=[];
  cells.push(cell('Last scan','<span id="ops-ago">—</span>','<span id="ops-abs"></span>'));
  cells.push(cell('Next scan','<span id="ops-next">—</span>','every '+cad+'h + on each update'));
  if(L)cells.push(cell('Last run result',L.ok+'<small> / '+L.attempted+' ok</small>',
    L.success+'% success'+(L.no_results?' · '+L.no_results+' empty':'')+(L.errors?' · '+L.errors+' err':'')));
  if(L)cells.push(cell('Fares harvested',fmtv(L.offers),'in the latest run'));
  if(ST.covered_from)cells.push(cell('Departures covered',fdshort(ST.covered_from)+' – '+fdshort(ST.covered_to),'dates with fares on file'));
  cells.push(cell('Tracking grid',fmtv(ST.grid_total||0),(ST.grid_from?fdshort(ST.grid_from)+' – '+fdshort(ST.grid_to):'')+' · '+(ST.routes?ST.routes.length:1)+' route'+((ST.routes&&ST.routes.length>1)?'s':'')));

  // recent runs as stacked bars (ok / empty / error)
  let runs='';
  if(ST.recent&&ST.recent.length){const mx=Math.max(1,...ST.recent.map(r=>r.attempted||1));
    const bars=ST.recent.slice().reverse().map((r,i,arr)=>{const isnow=i===arr.length-1;
      const h=v=>Math.round((v/mx)*100);const t=(String(r.slot).match(/T(\d{2})/)||[])[1];
      const dm=(String(r.slot).match(/(\d{2})-(\d{2})T/)||[]);const lab=(t!=null?t+':00':'')+(dm[2]?' '+dm[2]+'/'+dm[1]:'');
      return '<div class="runbar'+(isnow?' now':'')+'"><div class="stack" title="'+r.ok+' ok · '+r.no_results+' empty · '+r.errors+' err">'+
        '<div class="seg ok" style="height:'+h(r.ok)+'%"></div>'+
        '<div class="seg nr" style="height:'+h(r.no_results)+'%"></div>'+
        '<div class="seg er" style="height:'+h(r.errors)+'%"></div></div>'+
        '<div class="cap">'+lab+'</div></div>';}).join('');
    runs='<div class="ops-runs"><div class="rh">Recent runs · success / empty / error</div>'+
      '<div class="ops-bars">'+bars+'</div>'+
      '<div class="ops-legend"><span><i style="background:var(--buy)"></i>got fares</span>'+
      '<span><i style="background:#cfd9ea"></i>no results</span><span><i style="background:var(--pink)"></i>error</span></div></div>';}

  // what's expected next
  let next='';
  const nc=ST.next_count||ST.grid_total||0;
  const nrange=ST.next_from?(' · departures '+fdshort(ST.next_from)+' – '+fdshort(ST.next_to)):'';
  next='<div class="ops-next"><span class="nxt-ic">⤓</span><div>Up next: the <b id="ops-next2">next run</b> will scrape '+
    '<b>~'+fmtv(nc)+'</b> itineraries'+(ST.shards>1?' (shard <b>'+(ST.next_shard||1)+'/'+ST.shards+'</b> of the rolling '+
    fmtv(ST.grid_total)+'-itinerary grid)':'')+nrange+'. The full grid is covered across the day’s '+ST.shards+' slots.</div></div>';

  let routes='';
  if(ST.routes&&ST.routes.length)routes='<div class="ops-routes">Routes: '+
    ST.routes.slice(0,10).map(r=>'<span class="rt">'+esc(r.replace('-','→'))+'</span>').join('')+
    (ST.routes.length>10?'<span class="rt">+'+(ST.routes.length-10)+'</span>':'')+'</div>';

  add('<section class="section reveal" id="status" style="margin-bottom:2px"><h2>Data freshness</h2>'+
    '<span class="hint">updates automatically</span></section>'+
    '<div class="ops reveal" id="opscard">'+
      '<div class="ops-top"><span class="ops-badge" id="ops-badge"><span class="dot"></span><span id="ops-badge-txt">checking…</span></span>'+
        '<span class="ops-title">Fare collection engine</span>'+
        '<span class="ops-sub" id="ops-sub"></span></div>'+
      '<div class="ops-grid">'+cells.join('')+'</div>'+
      next+runs+routes+'</div>');

  tickOps();
}

function tickOps(){const ST=D.status||{};const card=document.getElementById('opscard');if(!card)return;
  const state=opsState();
  // Only toggle the state class -- never reset className, or we'd strip the
  // scroll-reveal '.in' class and the whole panel would fade back out.
  card.classList.remove('live','due','stale');card.classList.add(state);
  const badge=document.getElementById('ops-badge'),bt=document.getElementById('ops-badge-txt');
  badge.classList.remove('live','due','stale');badge.classList.add(state);
  bt.textContent=state==='live'?'Active':(state==='due'?'Scheduled':'Idle');
  const ago=document.getElementById('ops-ago');if(ago)ago.textContent=agoTxt(ST.last_scan_iso);
  const abs=document.getElementById('ops-abs');if(abs&&ST.last_scan_iso)abs.textContent=String(ST.last_scan_iso).replace('T',' ').replace(':00Z',' UTC');
  const nx=document.getElementById('ops-next');if(nx){const t=nextTickUTC(ST.cadence_hours||6);nx.textContent='in '+dhms(t-Date.now(),true);}
  const sub=document.getElementById('ops-sub');if(sub)sub.textContent='built '+(D.generated||'');
  setTimeout(tickOps,1000);
}

/* Pull a freshly deployed build without a manual refresh: every scan (and every
   merge to main) redeploys docs/index.html, so reload periodically while visible. */
setInterval(function(){if(document.visibilityState==='visible')location.reload();},300000);

if(!D.recs.length){
  add('<div class="empty reveal"><h2>Collecting fares…</h2><p>Faro has just started tracking this route. Buy/wait signals, price trends and '+
    'airline comparisons will appear once a few days of fare data have been collected.</p></div>');
}else{
  /* price-drop / new-low alert bar (the dashboard echo of the push alerts) */
  (function(){const ai=D.ai||{},drops=(ai.anomalies||[]).filter(a=>a.kind==='drop'),nl=((ai.what_changed||{}).new_lows||[]);
    if(!drops.length&&!nl.length)return;let t,s;
    if(drops.length){const pi=parseItin(drops[0].itin);t=drops.length+' price drop'+(drops.length>1?'s':'')+' detected';
      s=esc(pi.title)+' fell to '+fmt(drops[0].price)+' — '+Math.abs(drops[0].pct)+'% below its recent norm.';}
    else{const pi=parseItin(nl[0].itin);t=nl.length+' new low'+(nl.length>1?'s':'');s=esc(pi.title)+' just hit a fresh low of '+fmt(nl[0].price)+'.';}
    add('<div class="alertbar reveal"><span class="ab-ic">📉</span><div><div class="ab-t">'+t+'</div><div class="ab-s">'+s+'</div></div></div>');})();

  renderRouteSwitcher();
  renderHighlights();
  renderRoutes();

  buildFinder();

  renderMarket();

  add('<section class="sscope" data-for="flagship"><div class="section reveal" id="trend"><h2>Price trends &amp; airlines</h2></div>'+
    '<div class="grid2"><div class="panel reveal"><h3>Cheapest fare over time</h3><div class="ph">each line tracks a departure date — see how fares change day by day</div>'+
      '<div class="canvas-wrap"><canvas id="trendChart"></canvas></div></div>'+
    '<div class="panel reveal"><h3>Best fare by airline</h3><div class="ph">lowest fare each carrier offered in the latest scan</div>'+
      '<div class="canvas-wrap"><canvas id="airChart"></canvas></div></div></div></section>');

  /* ---- AI layer: when to book, cheapest day, what changed, accuracy ---- */
  const AI=D.ai;
  if(AI){
    const btb=AI.best_time_to_book||[],modelRec=D.recs.find(r=>r.curve&&r.curve.length);
    if(btb.length||modelRec){
      add('<section class="sscope" data-for="flagship"><div class="section reveal" id="plan"><h2>When to book</h2><span class="hint">model forecast</span></div>'+
        '<div class="grid2"><div class="panel reveal"><h3>Predicted booking curve</h3><div class="ph">how much you can expect to pay depending on when you book</div>'+
        '<div class="canvas-wrap"><canvas id="fanChart"></canvas></div></div>'+
        '<div class="panel reveal"><h3>Best moment to lock it in</h3><div class="ph">when the model expects the low</div><div class="cols3" style="grid-template-columns:1fr">'+
        (btb.length?btb.map(b=>{const pi=parseItin(b.itin);return '<div class="bookcard reveal"><div class="rt">'+esc(pi.dates)+(pi.nights?' · '+pi.nights+'n':'')+'</div><div class="when">'+routeChip(pi.o,pi.d)+'</div>'+
          '<div class="verdict '+(b.book_now?'now':'wait')+'">'+(b.book_now?'Book now':'Wait ~'+b.days_from_now+' days')+'</div>'+
          '<div class="sub">'+(b.book_now?'Already near the expected low of '+fmt(b.predicted_low)+'.':'Model expects a low near '+fmt(b.predicted_low)+(b.save>0?' — about '+fmt(b.save)+' under today.':'.'))+'</div>'+
          (b.book_now?'<div style="margin-top:12px">'+bookBtn(b.itin,'Book now')+'</div>':'')+'</div>';}).join('')
          :'<div class="sub" style="color:var(--dim)">Booking forecasts will appear once enough fare history has been collected.</div>')+
        '</div></div></div></section>');
    }
    const cd=AI.cheapest_day||{};
    if(cd.dep_dow&&cd.dep_dow.length){
      const cells=(arr,best)=>arr.map(c=>'<div class="cell'+(best&&c.dow===best.dow?' best':'')+'"><div class="d">'+c.label+'</div><div class="p">'+fmt(c.min)+'</div></div>').join('');
      add('<section class="sscope" data-for="flagship"><div class="section reveal"><h2>Cheapest day to fly</h2><span class="hint">latest scan</span></div>'+
        '<div class="grid2"><div class="panel reveal"><h3>By departure weekday</h3><div class="ph">cheapest return fare seen for each outbound day</div><div class="heat">'+cells(cd.dep_dow,cd.best_dep)+'</div></div>'+
        '<div class="panel reveal"><h3>By return weekday</h3><div class="ph">cheapest fare for each inbound day</div><div class="heat">'+cells(cd.ret_dow,cd.best_ret)+'</div></div></div></section>');
    }
    const wc=AI.what_changed||{},mv=(wc.movers||[]);
    if(mv.length){
      let h='<div class="panel reveal"><div class="digest">';
      mv.slice(0,6).forEach(m=>{const pi=parseItin(m.itin),dn=m.delta<0;
        h+='<div class="row"><span class="wc-trip">'+esc(pi.dates)+(pi.nights?' · '+pi.nights+'n':'')+' '+routeChip(pi.o,pi.d)+'</span>'+
           '<span class="mv '+(dn?'down':'up')+'">'+(dn?'▼ ':'▲ ')+fmt(Math.abs(m.delta))+' ('+(dn?'':'+')+m.pct+'%)</span></div>';});
      add('<section class="sscope" data-for="flagship"><div class="section reveal"><h2>What changed</h2><span class="hint">since the previous scan</span></div>'+h+'</div></div></section>');
    }
    const bt=AI.backtest;
    if(bt&&bt.n>=10){
      const bs=bt.by_signal||{},part=k=>bs[k]?'<span><b>'+bs[k].hit_rate+'%</b> of '+k+' calls ('+bs[k].n+')</span>':'';
      add('<section class="sscope" data-for="flagship"><div class="section reveal"><h2>Signal accuracy</h2><span class="hint">how often our buy/wait calls were right</span></div>'+
        '<div class="panel reveal"><div class="bigstat"><span class="v">'+bt.hit_rate+'%</span><span class="u">of past calls were right ('+bt.n+' graded)</span></div>'+
        '<div class="sigrow">'+part('BUY')+part('WAIT')+part('WATCH')+(bt.avg_buy_regret?'<span>avg missed saving on a BUY: <b>'+fmt(bt.avg_buy_regret)+'</b></span>':'')+'</div>'+
        '<div class="canvas-wrap" style="height:160px;margin-top:14px"><canvas id="accChart"></canvas></div></div></section>');
    }
  }

  if(D.insights&&D.insights.length){
    let h='<div class="icards">';
    D.insights.forEach(r=>{const pi=parseItin(r.itinerary),sb=r.stops,tot=Math.max(1,sb.nonstop+sb.one+sb.two_plus);
      const seg=(n,c)=>n?'<i style="width:'+(n/tot*100)+'%;background:'+c+'"></i>':'';
      const deal=(D.ai&&D.ai.deals&&D.ai.deals[r.itinerary])||null;
      const db=deal?' <span class="dealbadge '+esc(deal.label.toLowerCase())+'">'+deal.score+'</span>':'';
      const alist=(r.airline_prices||[]).map(a=>'<div class="aline">'+avatar(a.name,a.iata,28)+'<span class="nm">'+esc(a.name)+'</span><span class="pr">'+fmt(a.price)+'</span></div>').join('')
        ||'<div class="aline" style="color:var(--dim)">airline not reported</div>';
      h+='<div class="icard reveal" data-route="'+esc(routeKey(r.itinerary))+'"><div class="top"><div><div class="rt">'+esc(pi.dates)+(pi.nights?' · '+pi.nights+' nights':'')+db+'</div><div class="when">'+routeChip(pi.o,pi.d)+' · '+r.offers+' offers</div></div>'+
        '<div style="text-align:right"><div class="big">'+fmt(r.min)+'</div><small>cheapest</small></div></div>'+
        '<div class="facts"><div class="fact"><div class="k">Typical</div><div class="v">'+fmt(r.median)+'</div></div>'+
          '<div class="fact"><div class="k">Highest</div><div class="v">'+fmt(r.max)+'</div></div>'+
          '<div class="fact"><div class="k">Fastest</div><div class="v">'+dur(r.fastest)+'</div></div>'+
          '<div class="fact"><div class="k">Typical time</div><div class="v">'+dur(r.typical_duration)+'</div></div></div>'+
        '<div class="stopbar">'+seg(sb.nonstop,'#12b07c')+seg(sb.one,'#3b6ef5')+seg(sb.two_plus,'#e8902a')+'</div>'+
        '<div class="stopkey"><span><i class="swatch" style="background:#12b07c"></i>'+sb.nonstop+' nonstop</span>'+
          '<span><i class="swatch" style="background:#3b6ef5"></i>'+sb.one+' · 1 stop</span>'+
          '<span><i class="swatch" style="background:#e8902a"></i>'+sb.two_plus+' · 2+ stops</span></div>'+
        '<div class="alist">'+alist+'</div>'+
        '<div style="margin-top:14px">'+bookRowLink(r.itinerary,'Book this trip ↗')+'</div></div>';});
    add('<section class="rsec"><div class="section reveal" id="insights"><h2>Market insights</h2><span class="hint">latest scan</span></div>'+h+'</div></section>');}

  if(D.latest_offers&&Object.keys(D.latest_offers).length){
    let fh='';
    Object.keys(D.latest_offers).forEach(itin=>{const rows=D.latest_offers[itin];if(!rows.length)return;
      const pi=parseItin(itin),best=Math.min.apply(null,rows.map(o=>o.price));
      let t='<details class="reveal" data-route="'+esc(routeKey(itin))+'"><summary><span>'+esc(pi.dates)+(pi.nights?' · '+pi.nights+'n':'')+' '+routeChip(pi.o,pi.d)+'</span><span class="pill">'+rows.length+' offers · from '+fmt(best)+'</span></summary>'+
        bookBar(itin)+
        '<div class="tscroll"><table><thead><tr><th>Airline</th><th>Stops</th><th class="ft">Flight time</th><th class="num">Price</th><th></th></tr></thead><tbody>';
      rows.forEach(o=>{const isb=o.price===best;
        t+='<tr class="'+(isb?'best':'')+'"><td><span class="airline">'+avatar(o.airline,o.iata,24)+
          '<span class="anm">'+(esc(o.airline)||'<span style="color:var(--dim)">—</span>')+'</span>'+(isb?'<span class="cheapest">CHEAPEST</span>':'')+'</span></td>'+
          '<td><span class="chip'+(o.stops===0?' ns':'')+'">'+stops(o.stops)+'</span>'+(o.via?'<span class="viachip">via '+esc(o.via)+'</span>':'')+'</td>'+
          '<td class="mono ft">'+dur(o.duration)+'</td><td class="num">'+fmt(o.price)+'</td>'+
          '<td class="num">'+bookRowLink(itin,'Book ↗')+'</td></tr>';});
      fh+=t+'</tbody></table></div></details>';});
    add('<section class="rsec"><div class="section reveal" id="fares"><h2>Latest fares</h2><span class="hint">cheapest per route</span></div>'+fh+'</section>');}

  /* Today's BUY/WAIT/WATCH signals -- moved to the bottom of the page. */
  let dh='';
  D.recs.forEach((r,i)=>{const pi=parseItin(r.itinerary),conf=r.confidence||0,tags=[];
    const deal=(D.ai&&D.ai.deals&&D.ai.deals[r.itinerary])||null;
    const narr=(D.ai&&D.ai.narratives&&D.ai.narratives[r.itinerary])||'';
    // The actual cheapest bookable option for this trip -- airline / stops / where
    // the stop is -- so a signal isn't just an abstract number.
    const offs=(D.latest_offers&&D.latest_offers[r.itinerary])||[],o0=offs[0]||null;
    if(r.predicted_low)tags.push(['forecast low '+fmt(r.predicted_low),1]);
    if(r.signal==='WAIT'&&r.expected_savings)tags.push(['save ~'+fmt(r.expected_savings)+' by waiting',1]);
    if(r.prob_drop!=null)tags.push([r.prob_drop+'% chance of a drop',0]);
    if(r.days_to_departure!=null)tags.push([r.days_to_departure+' days to go',0]);
    const db=deal?'<span class="dealbadge '+esc(deal.label.toLowerCase())+'">'+deal.score+' · '+esc(deal.label)+' deal</span>':'';
    const detail=o0?'<div class="rec-trip">'+(o0.airline?avatar(o0.airline,o0.iata,20)+'<span class="anm">'+esc(o0.airline)+'</span>':'')+
      stopsVia(o0.stops,o0.via,o0.stops===0)+(o0.duration?'<span class="mini">⏱ '+dur(o0.duration)+'</span>':'')+'</div>':'';
    dh+='<div class="rec reveal" data-route="'+esc(routeKey(r.itinerary))+'"><span class="sig '+r.signal+'">'+r.signal+'</span>'+
      '<div class="rec-main"><div class="dates lead">'+esc(pi.dates)+(pi.nights?' · '+pi.nights+' nights':'')+'</div>'+
        '<div class="rec-sub">'+routeChip(pi.o,pi.d)+db+'</div>'+
        detail+
        '<div class="reason">'+esc(r.reason)+'</div>'+
        (narr?'<div class="narr">'+esc(narr)+'</div>':'')+
        '<div class="conf"><div class="lab"><span>confidence</span><span>'+conf+'%</span></div><div class="bar"><i data-w="'+conf+'"></i></div></div>'+
        '<div class="tags">'+tags.map(t=>'<span class="tag'+(t[1]?' b':'')+'">'+esc(t[0])+'</span>').join('')+'</div></div>'+
      '<canvas class="spark" id="spark'+i+'"></canvas>'+
      '<div class="pricebox"><div class="price">'+fmt(r.price)+'</div><div class="pricelbl">low '+fmt(r.trailing_min)+' · '+r.points+' pts</div>'+
        '<div style="margin-top:8px">'+bookRowLink(r.itinerary,'Book ↗')+'</div></div></div>';});
  add(‘<section class="rsec"><div class="section reveal" id="deals"><h2>Today\’s signals</h2><span class="hint">should you book now or wait?</span></div>’+dh+’</section>’);
}

/* Live scraper status -- useful but secondary, so it renders last (deprioritised
   below the deals, trip finder and analytics). */
renderOps();

/* ===== trip finder: filters + fare calendar =====
   All client-side over the embedded D.explore grid (every departure date x trip
   length we have scraped), so a visitor can narrow by dates / length / price /
   stops / airline, see the cheapest fare painted on a calendar, and deep-link
   straight to the booking page for any option. */
function buildFinder(){
  let EX=[], EXM=D.explore_meta||{};
  if(!EXM.dep_min)return;
  let loaded=false;
  const lmin=EXM.len_min, lmax=EXM.len_max, hasLen=(lmin!=null&&lmax!=null&&lmax>lmin);
  const F={depFrom:EXM.dep_min,depTo:EXM.dep_max,lenMin:lmin,lenMax:lmax,
           maxPrice:EXM.price_max,stops:'any',airline:'',route:'',sort:'price',day:'',view:'cal'};
  let lim=24;
  const $=id=>document.getElementById(id);
  const cn=c=>(D.cities&&D.cities[c])||c;

  const exDate=s=>{const m=(s||'').match(/(\d{4})-(\d{2})-(\d{2})/);return m?new Date(+m[1],+m[2]-1,+m[3]):null;};
  const exFmt=s=>{const d=exDate(s);return d?d.getDate()+' '+MM[d.getMonth()]:s;};
  const kprice=n=>n>=1000?(n/1000).toFixed(n>=10000?0:1).replace(/\.0$/,'')+'k':String(Math.round(n));
  const itinOf=e=>e.o+'-'+e.d+' '+e.dep+' -> '+e.ret;

  // When an airline filter is active, every figure a card shows -- price, logo,
  // stops, duration, fare count -- comes from THAT carrier's own offers, not the
  // itinerary's overall cheapest (which may be a completely different airline).
  const ab=e=>(F.airline&&e.byair&&e.byair[F.airline])||null;
  const priceOf=e=>{const b=ab(e);return b?b.min:e.min;};
  const airOf=e=>{const b=ab(e);return b?b.label:e.airline;};
  const iataOf=e=>{const b=ab(e);return b?b.iata:e.iata;};
  const stopsOf=e=>{const b=ab(e);return b?b.stops:e.stops;};
  const nsOf=e=>{const b=ab(e);return b?b.ns:e.nonstop;};
  const fastOf=e=>{const b=ab(e);return b?b.fast:e.fastest;};
  const offersOf=e=>{const b=ab(e);return b?b.n:e.offers;};
  const flies=(e,a)=>e.byair?(a in e.byair):((e.al||[]).includes(a));

  function match(e,ignoreDay){
    if(F.depFrom&&e.dep<F.depFrom)return false;
    if(F.depTo&&e.dep>F.depTo)return false;
    if(F.lenMin!=null&&(e.len==null||e.len<F.lenMin))return false;
    if(F.lenMax!=null&&(e.len==null||e.len>F.lenMax))return false;
    if(F.airline&&!flies(e,F.airline))return false;
    if(F.maxPrice!=null&&priceOf(e)>F.maxPrice)return false;
    if(F.stops==='nonstop'&&!nsOf(e))return false;
    if(F.stops==='1'&&(stopsOf(e)==null||stopsOf(e)>1))return false;
    if(F.route&&(e.o+'-'+e.d)!==F.route)return false;
    if(!ignoreDay&&F.day&&e.dep!==F.day)return false;
    return true;
  }
  const SORT={price:(a,b)=>priceOf(a)-priceOf(b),
    date:(a,b)=>a.dep<b.dep?-1:a.dep>b.dep?1:(a.len||0)-(b.len||0),
    length:(a,b)=>(a.len||0)-(b.len||0),
    fast:(a,b)=>(fastOf(a)||1e9)-(fastOf(b)||1e9)};
  const heat=(p,lo,hi)=>{if(hi<=lo)return 'hsl(150 62% 90%)';
    const t=Math.max(0,Math.min(1,(p-lo)/(hi-lo)));return 'hsl('+(132-(132-6)*t).toFixed(0)+' 72% '+(91-t*16).toFixed(0)+'%)';};

  async function ensureLoaded(){
    if(loaded)return;
    try{const r=await fetch('explore.json');EX=await r.json();}catch(e){EX=D.explore||[];}
    loaded=true;
  }

  function buildCalendar(){
    const cal=$('cal');if(!cal)return;
    const list=EX.filter(e=>match(e,true)),perDay={};
    list.forEach(e=>{const p=priceOf(e),c=perDay[e.dep];if(!c){perDay[e.dep]={min:p,n:1};}else{c.n++;if(p<c.min)c.min=p;}});
    const days=Object.keys(perDay);
    if(!days.length){cal.innerHTML='<div class="finder-empty">No departure days match these filters.</div>';return;}
    const ps=days.map(d=>perDay[d].min),lo=Math.min.apply(null,ps),hi=Math.max.apply(null,ps);
    const cheapDay=days.reduce((a,b)=>perDay[b].min<perDay[a].min?b:a);
    const sorted=days.map(exDate).sort((a,b)=>a-b);
    let cur=new Date(sorted[0].getFullYear(),sorted[0].getMonth(),1);
    const end=new Date(sorted[sorted.length-1].getFullYear(),sorted[sorted.length-1].getMonth(),1);
    const DH=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];let html='';
    while(cur<=end){const y=cur.getFullYear(),mo=cur.getMonth();
      html+='<div class="cal-month"><div class="mlabel">'+MM[mo]+' '+y+'</div><div class="cal-grid">'+
        DH.map(d=>'<div class="dow">'+d+'</div>').join('');
      const lead=(new Date(y,mo,1).getDay()+6)%7;
      for(let i=0;i<lead;i++)html+='<div class="cal-cell blank"></div>';
      const dim=new Date(y,mo+1,0).getDate();
      for(let dd=1;dd<=dim;dd++){const iso=y+'-'+String(mo+1).padStart(2,'0')+'-'+String(dd).padStart(2,'0'),rec=perDay[iso];
        if(!rec){html+='<div class="cal-cell empty"><span class="dnum">'+dd+'</span></div>';continue;}
        html+='<div class="cal-cell has'+(F.day===iso?' sel':'')+(iso===cheapDay?' cheapest':'')+'" data-day="'+iso+'" '+
          'style="background:'+heat(rec.min,lo,hi)+'" title="'+rec.n+' trips · from '+fmt(rec.min)+'">'+
          '<span class="dnum">'+dd+'</span><span class="cp">'+kprice(rec.min)+'</span></div>';}
      html+='</div></div>';cur=new Date(y,mo+1,1);}
    cal.innerHTML='<div class="cal-legend">Cheapest fare by departure day · <span>'+fmt(lo)+'</span>'+
      '<span class="scale"><i style="background:'+heat(lo,lo,hi)+'"></i><i style="background:'+heat((lo+hi)/2,lo,hi)+'"></i><i style="background:'+heat(hi,lo,hi)+'"></i></span>'+
      '<span>'+fmt(hi)+'</span> · ★ cheapest day · tap a day to filter</div><div class="cal-months">'+html+'</div>';
    cal.querySelectorAll('.cal-cell.has').forEach(c=>c.addEventListener('click',()=>{
      F.day=F.day===c.dataset.day?'':c.dataset.day;lim=24;render();}));
  }

  function card(e){const L=bookLinks(itinOf(e));
    const price=priceOf(e),al=airOf(e),ia=iataOf(e),ns=nsOf(e),stp=stopsOf(e),fast=fastOf(e),off=offersOf(e);
    const st=ns?'<span class="ns">Nonstop</span>':(stp!=null?'<span>'+stops(stp)+'</span>':'');
    // When an airline filter is active, flag whether that carrier flies the WHOLE
    // trip on its own metal here, or only one leg of the connection.
    const b=ab(e);
    const metal=(F.airline&&b)?(b.solo
      ?'<span class="whole">whole trip on '+esc(F.airline)+'</span>'
      :'<span class="leg">'+esc(F.airline)+' flies one leg</span>'):'';
    return '<div class="trip"><div class="th">'+avatar(al,ia,26)+
      '<span class="rt">'+esc(cn(e.o))+' → '+esc(cn(e.d))+'</span><span class="pr">'+fmt(price)+'</span></div>'+
      '<div class="dt">'+exFmt(e.dep)+' – '+exFmt(e.ret)+(e.len!=null?' · '+e.len+' nights':'')+'</div>'+
      '<div class="mt">'+st+(fast?'<span>⏱ '+dur(fast)+'</span>':'')+
        (al?'<span>'+esc(al)+'</span>':'')+metal+(off?'<span>'+off+' fare'+(off===1?'':'s')+'</span>':'')+'</div>'+
      (L?'<div class="go"><a class="btnbook" href="'+L.google+'" target="_blank" rel="noopener nofollow">'+_EXT+'Book</a>'+
        '<span class="cmp">or <a href="'+L.kayak+'" target="_blank" rel="noopener nofollow">Kayak</a> · '+
        '<a href="'+L.sky+'" target="_blank" rel="noopener nofollow">Skyscanner</a></span></div>':'')+'</div>';}

  function buildTrips(){const wrap=$('trips');if(!wrap)return;
    const list=EX.filter(e=>match(e,false)).sort(SORT[F.sort]||SORT.price),total=list.length;
    const cEl=$('ex-count');
    if(cEl)cEl.innerHTML=total?'<b>'+total+'</b> trip'+(total===1?'':'s')+(F.airline?' on '+esc(F.airline):'')+(F.day?' · '+exFmt(F.day):'')+' · from '+fmt(Math.min.apply(null,list.map(priceOf))):'<b>0</b> trips match';
    if(!total){wrap.innerHTML='<div class="finder-empty">No trips match these filters.'+
        (F.airline?' We haven’t seen <b>'+esc(F.airline)+'</b> on a matching trip — try <a href="#" id="ex-clearair">any airline</a> or':' Try')+
        ' widening the dates, price or trip length.</div>';
      const ca=$('ex-clearair');if(ca)ca.addEventListener('click',ev=>{ev.preventDefault();F.airline='';$('f-air').value='';lim=24;render();});
      const mb=$('ex-more');if(mb)mb.style.display='none';return;}
    wrap.innerHTML=list.slice(0,lim).map(card).join('');
    const mb=$('ex-more');if(mb)mb.style.display=total>lim?'block':'none';}

  function render(){const box=$('calbox');if(box)box.style.display=F.view==='cal'?'block':'none';
    if(F.view==='cal')buildCalendar();buildTrips();}
  async function doSearch(){await ensureLoaded();lim=24;render();}

  // ---- controls ----
  const amins=EXM.airline_min||{};
  const airOpts='<option value="">Any airline</option>'+(EXM.airlines||[]).map(a=>
    '<option value="'+esc(a)+'">'+esc(a)+(amins[a]!=null?' · from '+fmt(amins[a]):'')+'</option>').join('');
  const nopts=sel=>{let o='';for(let n=lmin;n<=lmax;n++)o+='<option value="'+n+'"'+(n===sel?' selected':'')+'>'+n+' nights</option>';return o;};
  const lenFlds=hasLen?'<div class="fld"><label>Min nights</label><select id="f-lmin">'+nopts(lmin)+'</select></div>'+
      '<div class="fld"><label>Max nights</label><select id="f-lmax">'+nopts(lmax)+'</select></div>':'';
  const routeFld=(EXM.routes&&EXM.routes.length>1)?'<div class="fld"><label>Route</label><select id="f-route"><option value="">All routes</option>'+
      EXM.routes.map(r=>'<option value="'+esc(r)+'">'+esc(r.replace('-','→'))+'</option>').join('')+'</select></div>':'';
  const stopsBtns='<button data-v="any" class="on">Any</button><button data-v="1">≤1 stop</button>'+(EXM.nonstop_any?'<button data-v="nonstop">Nonstop</button>':'');

  add('<section class="sscope" data-for="data"><div class="section reveal" id="finder"><h2>Find a trip</h2><span class="hint">'+(EXM.count||0)+' date combos · set filters &amp; search</span></div>'+
   '<div class="finder reveal"><div class="filters">'+
     '<div class="fld"><label>Depart from</label><input type="date" id="f-df" min="'+EXM.dep_min+'" max="'+EXM.dep_max+'" value="'+EXM.dep_min+'"></div>'+
     '<div class="fld"><label>Depart to</label><input type="date" id="f-dt" min="'+EXM.dep_min+'" max="'+EXM.dep_max+'" value="'+EXM.dep_max+'"></div>'+
     lenFlds+
     '<div class="fld range"><label>Max price <span class="rangeval" id="f-pv">'+fmt(EXM.price_max)+'</span></label>'+
       '<input type="range" id="f-price" min="'+EXM.price_min+'" max="'+EXM.price_max+'" value="'+EXM.price_max+'" step="10"></div>'+
     routeFld+
     '<div class="fld"><label>Airline</label><select id="f-air">'+airOpts+'</select></div>'+
     '<div class="fld"><label>Stops</label><div class="seg-ctrl" id="f-stops">'+stopsBtns+'</div></div>'+
     '<div class="fld"><label>Sort by</label><select id="f-sort"><option value="price">Cheapest</option>'+
       '<option value="date">Departure date</option><option value="length">Trip length</option><option value="fast">Fastest</option></select></div>'+
     '<div class="fld"><label>&nbsp;</label><button class="btnbook" id="f-search" style="width:100%;justify-content:center;border:0;cursor:pointer">'+_EXT+'Search flights</button></div>'+
   '</div>'+
   '<div class="finder-bar"><span class="count" id="ex-count">Set your dates and click Search</span>'+
     '<div class="viewtoggle" id="f-view"><button data-v="cal" class="on">Calendar</button><button data-v="list">List</button></div>'+
     '<button class="reset" id="f-reset">Reset</button></div>'+
   '<div class="cal-wrap" id="calbox" style="display:none"><div id="cal"></div></div>'+
   '<div class="trips" id="trips"></div>'+
   '<button class="morebtn" id="ex-more" style="display:none">Show more trips</button></div></section>');

  // ---- wiring ----
  $('f-search').addEventListener('click',()=>doSearch());
  $('f-df').addEventListener('change',e=>{F.depFrom=e.target.value;F.day='';doSearch();});
  $('f-dt').addEventListener('change',e=>{F.depTo=e.target.value;F.day='';doSearch();});
  if($('f-lmin'))$('f-lmin').addEventListener('change',e=>{F.lenMin=+e.target.value;doSearch();});
  if($('f-lmax'))$('f-lmax').addEventListener('change',e=>{F.lenMax=+e.target.value;doSearch();});
  $('f-price').addEventListener('input',e=>{F.maxPrice=+e.target.value;$('f-pv').textContent=fmt(F.maxPrice);if(loaded){lim=24;render();}});
  $('f-air').addEventListener('change',e=>{F.airline=e.target.value;doSearch();});
  if($('f-route'))$('f-route').addEventListener('change',e=>{F.route=e.target.value;doSearch();});
  $('f-sort').addEventListener('change',e=>{F.sort=e.target.value;if(loaded){lim=24;render();}});
  $('f-stops').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    F.stops=b.dataset.v;$('f-stops').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));if(loaded){lim=24;render();}}));
  $('f-view').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
    F.view=b.dataset.v;$('f-view').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));if(loaded)render();}));
  $('ex-more').addEventListener('click',()=>{lim+=24;buildTrips();});
  $('f-reset').addEventListener('click',()=>{
    F.depFrom=EXM.dep_min;F.depTo=EXM.dep_max;F.lenMin=lmin;F.lenMax=lmax;F.maxPrice=EXM.price_max;
    F.stops='any';F.airline='';F.route='';F.sort='price';F.day='';lim=24;
    $('f-df').value=EXM.dep_min;$('f-dt').value=EXM.dep_max;$('f-price').value=EXM.price_max;$('f-pv').textContent=fmt(EXM.price_max);
    if($('f-lmin'))$('f-lmin').value=lmin;if($('f-lmax'))$('f-lmax').value=lmax;
    $('f-air').value='';if($('f-route'))$('f-route').value='';$('f-sort').value='price';
    $('f-stops').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.v==='any'));if(loaded){render();}});
  // Let the "Routes we track" cards jump here with a corridor preselected.
  window.__finderPickRoute=function(rt){F.route=rt;if($('f-route'))$('f-route').value=rt;
    F.view='cal';$('f-view').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.v==='cal'));
    doSearch();};
}

/* ===== market analytics: pulse gauge, booking-window curve, trip-length value,
   fare distribution and a standout-deals leaderboard -- all derived offline
   from the whole rolling grid (D.ai.market). ===== */
function renderMarket(){
  const M=(D.ai&&D.ai.market)||null; if(!M)return;
  // The buy-index pulse now lives in the bento command center up top; this
  // section focuses on the supporting charts and the deals leaderboard.
  const AC=M.advance_curve,LC=M.length_curve;
  let mkt='<section class="sscope" data-for="all"><div class="section reveal" id="market"><h2>Market analytics</h2><span class="hint">pricing patterns across all routes</span></div>';

  mkt+='<div class="grid2">'+
    '<div class="panel reveal"><h3>How far ahead to book</h3><div class="ph">median cheapest fare by days before departure, across the grid</div>'+
      (AC?'<div class="callout">📅 Cheapest around <b>'+AC.best_dtd+' days</b> out (~'+fmt(AC.best_price)+') · last-minute can cost <b>'+fmt(AC.save_vs_worst)+'</b> more</div>':'')+
      '<div class="canvas-wrap"><canvas id="advChart"></canvas></div></div>'+
    '<div class="panel reveal"><h3>Best value by trip length</h3><div class="ph">cheapest return fare for each trip duration</div>'+
      (LC?'<div class="callout">🎯 Sweet spot: <b>'+LC.best_len+' nights</b> from <b>'+fmt(LC.best_price)+'</b></div>':'')+
      '<div class="canvas-wrap"><canvas id="lenChart"></canvas></div></div></div>';

  const PD=M.price_distribution,SV=M.savings||[];
  mkt+='<div class="grid2">'+
    '<div class="panel reveal"><h3>Where fares sit today</h3><div class="ph">'+(PD?PD.n+' trips · median '+fmt(PD.median)+' · '+fmt(PD.min)+'–'+fmt(PD.max):'spread of current fares')+'</div>'+
      '<div class="canvas-wrap"><canvas id="distChart"></canvas></div></div>'+
    '<div class="panel reveal"><h3>Standout deals right now</h3><div class="ph">trips priced well below their usual fare — with airline, stops &amp; booking links</div>'+
      (SV.length?'<div class="lead-list">'+SV.map((s,i)=>{
        const air=s.airline?avatar(s.airline,s.iata,20)+'<span class="anm">'+esc(s.airline)+'</span>':'';
        return '<div class="lead-row"><div class="rk">'+(i+1)+'</div>'+
          '<div class="ld-main"><div class="ld-r">'+fdshort(s.dep)+' – '+fdshort(s.ret)+' · '+s.len+' nights'+
            (s.dtd!=null?' <span class="ld-out">'+s.dtd+'d out</span>':'')+'</div>'+
          '<div class="ld-meta">'+routeChip(s.o,s.d)+stopsVia(s.stops,s.via,false)+'</div>'+
          (air?'<div class="ld-air">'+air+signalPill(s.itin)+'</div>':'')+
          '<div class="ld-book">'+bookRowLink(s.itin,'Book ↗')+'</div></div>'+
          '<div class="ld-p"><div class="now">'+fmt(s.price)+'</div><div class="was">'+fmt(s.typical)+'</div>'+
            '<div class="ld-s">'+s.pct+'% off · −'+fmt(s.save)+'</div></div></div>';}).join('')+'</div>'
        :'<div class="finder-empty">No trips are sitting below their typical fare right now.</div>')+
      '</div></div>';
  add(mkt+'</section>');
}

/* ---- charts ---- */
function gridOpts(){return{responsive:true,maintainAspectRatio:false,
  plugins:{legend:{labels:{font:{family:'Sora',size:11},color:'#56678a',boxWidth:12,padding:14}}},
  scales:{x:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}},
          y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}}}};}
function placeholder(id,msg){const c=document.getElementById(id);if(!c)return;
  (c.closest('.canvas-wrap')||c.parentNode).innerHTML='<div style="height:100%;display:grid;place-items:center;color:var(--dim);font-size:13px;text-align:center;padding:0 20px">'+msg+'</div>';}
let charted=false;
function drawCharts(){if(charted)return;charted=true;
  if(typeof Chart==='undefined'){['trendChart','airChart','advChart','lenChart','distChart','fanChart','accChart']
    .forEach(id=>placeholder(id,'Chart library unavailable.'));return;}
  D.recs.forEach((r,i)=>{const h=D.history[r.itinerary]||[],c=document.getElementById('spark'+i);if(!c||h.length<2)return;
    const g=c.getContext('2d').createLinearGradient(0,0,0,46);g.addColorStop(0,'rgba(59,110,245,.28)');g.addColorStop(1,'rgba(59,110,245,0)');
    new Chart(c,{type:'line',data:{labels:h.map(x=>x.d),datasets:[{data:h.map(x=>x.p),borderColor:'#3b6ef5',borderWidth:2,pointRadius:0,tension:.35,fill:true,backgroundColor:g}]},
      options:{responsive:false,plugins:{legend:{display:false},tooltip:{enabled:false}},scales:{x:{display:false},y:{display:false}}}});});
  const tc=document.getElementById('trendChart'),enough=Object.values(D.history).some(h=>h.length>=2);
  if(tc&&!enough)placeholder('trendChart','Price history charts appear after the first couple of daily scans — check back soon.');
  else if(tc){let itins=Object.keys(D.history).filter(k=>(D.history[k]||[]).length>=2);
    // Label each line by what makes it DISTINCT -- its departure + nights (and the
    // route only when more than one corridor is charted) -- never 9x the same route.
    itins.sort((a,b)=>{const A=parseItin(a),B=parseItin(b);return (A.o+A.d).localeCompare(B.o+B.d)|| (a<b?-1:1);});
    const labels=[...new Set([].concat(...itins.map(k=>D.history[k].map(x=>x.d))))].sort();
    const multi=NROUTES()>1;
    const ds=itins.map((k,i)=>{const m=Object.fromEntries(D.history[k].map(x=>[x.d,x.p])),pi=parseItin(k);
      const dep=(pi.dates||'').split(' – ')[0];
      const lab=(multi?pi.o+'→'+pi.d+' · ':'')+dep+(pi.nights?' · '+pi.nights+'n':'');
      return{label:lab,data:labels.map(d=>m[d]??null),borderColor:palette[i%palette.length],backgroundColor:palette[i%palette.length],borderWidth:2,pointRadius:1.5,tension:.3,spanGaps:true};});
    new Chart(tc,{type:'line',data:{labels,datasets:ds},options:Object.assign(gridOpts(),{plugins:{legend:{position:'bottom',labels:{font:{family:'Sora',size:10},color:'#56678a',boxWidth:10,padding:9,usePointStyle:true}}}})});}
  const ac=document.getElementById('airChart');
  if(ac&&D.airline_market&&D.airline_market.length){const m=D.airline_market;
    new Chart(ac,{type:'bar',data:{labels:m.map(a=>a.name),datasets:[{label:'Lowest fare ('+CUR+')',data:m.map(a=>a.min),backgroundColor:m.map(a=>acolor(a.name)),borderRadius:8,maxBarThickness:30}]},
      options:Object.assign(gridOpts(),{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>' '+fmt(c.raw)+' · '+m[c.dataIndex].offers+' offers'}}}})});}
  /* predicted booking-curve fan chart (median + calibrated band) */
  const fcx=document.getElementById('fanChart'),mr=D.recs.find(r=>r.curve&&r.curve.length);
  if(fcx&&mr){const cv=mr.curve;
    new Chart(fcx,{type:'line',data:{labels:cv.map(c=>c.dtd),datasets:[
      {data:cv.map(c=>c.hi),borderColor:'rgba(59,110,245,0)',backgroundColor:'rgba(59,110,245,.12)',pointRadius:0,fill:'+1',tension:.3},
      {data:cv.map(c=>c.lo),borderColor:'rgba(59,110,245,0)',pointRadius:0,fill:false,tension:.3},
      {data:cv.map(c=>c.p),borderColor:'#3b6ef5',borderWidth:2,pointRadius:0,tension:.3}]},
      options:Object.assign(gridOpts(),{plugins:{legend:{display:false},tooltip:{callbacks:{title:it=>it[0].label+' days before departure',label:c=>' '+fmt(c.raw)}}},
        scales:{x:{title:{display:true,text:'days before departure',color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}},grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:9},maxTicksLimit:8}},
                y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}}}})});}
  else if(fcx)placeholder('fanChart','Fare predictions will appear once enough price history has been collected.');
  /* model accuracy over time (from the offline backtest) */
  const acc=document.getElementById('accChart');
  if(acc&&D.ai&&D.ai.backtest&&(D.ai.backtest.series||[]).length>=2){const s=D.ai.backtest.series;
    new Chart(acc,{type:'line',data:{labels:s.map(x=>x.d),datasets:[{data:s.map(x=>x.acc),borderColor:'#12b07c',backgroundColor:'rgba(18,176,124,.14)',borderWidth:2,pointRadius:0,tension:.3,fill:true}]},
      options:Object.assign(gridOpts(),{plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>' '+c.raw+'% right'}}},scales:{x:{display:false},y:{min:0,max:100,grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10},callback:v=>v+'%'}}}})});}
  else if(acc)placeholder('accChart','Signal accuracy tracking appears once enough past predictions can be verified.');
  /* ---- market analytics graphs ---- */
  const MK=(D.ai&&D.ai.market)||null;
  if(MK){
    const adv=document.getElementById('advChart');
    if(adv&&MK.advance_curve&&MK.advance_curve.points.length>=3){const pts=MK.advance_curve.points;
      const g=adv.getContext('2d').createLinearGradient(0,0,0,252);g.addColorStop(0,'rgba(122,92,240,.22)');g.addColorStop(1,'rgba(122,92,240,0)');
      new Chart(adv,{type:'line',data:{labels:pts.map(p=>p.dtd),datasets:[{data:pts.map(p=>p.p),borderColor:'#7a5cf0',borderWidth:2,pointRadius:0,tension:.35,fill:true,backgroundColor:g}]},
        options:Object.assign(gridOpts(),{plugins:{legend:{display:false},tooltip:{callbacks:{title:it=>it[0].label+' days before departure',label:c=>' '+fmt(c.raw)+' median'}}},
          scales:{x:{reverse:true,title:{display:true,text:'days before departure',color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}},grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:9},maxTicksLimit:9}},
                  y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}}}})});}
    else if(adv)placeholder('advChart','This chart appears once fares have been collected across different booking windows.');

    const len=document.getElementById('lenChart');
    if(len&&MK.length_curve&&MK.length_curve.points.length>=2){const pts=MK.length_curve.points,best=MK.length_curve.best_len;
      new Chart(len,{type:'bar',data:{labels:pts.map(p=>p.len),datasets:[{data:pts.map(p=>p.min),
        backgroundColor:pts.map(p=>p.len===best?'#12b07c':'rgba(59,110,245,.55)'),borderRadius:6,maxBarThickness:24}]},
        options:Object.assign(gridOpts(),{plugins:{legend:{display:false},tooltip:{callbacks:{title:it=>it[0].label+' nights',label:c=>' from '+fmt(c.raw)}}},
          scales:{x:{title:{display:true,text:'trip length (nights)',color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}},grid:{display:false},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:9},maxTicksLimit:11}},
                  y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}}}})});}
    else if(len)placeholder('lenChart','Trip-length comparison appears once fares for different durations have been collected.');

    const dist=document.getElementById('distChart');
    if(dist&&MK.price_distribution&&MK.price_distribution.bins.length){const b=MK.price_distribution.bins,med=MK.price_distribution.median;
      new Chart(dist,{type:'bar',data:{labels:b.map(x=>x.lo),datasets:[{data:b.map(x=>x.count),
        backgroundColor:b.map(x=>(med>=x.lo&&med<x.hi)?'#3b6ef5':'rgba(59,110,245,.4)'),borderRadius:5,barPercentage:1,categoryPercentage:.97}]},
        options:Object.assign(gridOpts(),{plugins:{legend:{display:false},tooltip:{callbacks:{title:it=>{const x=b[it[0].dataIndex];return fmt(x.lo)+' – '+fmt(x.hi);},label:c=>' '+c.raw+' trips'}}},
          scales:{x:{grid:{display:false},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:9},maxTicksLimit:8,callback:function(v){return b[v]?fmtv(b[v].lo):'';}}},
                  y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10},precision:0}}}})});}
    else if(dist)placeholder('distChart','Fare distribution chart appears once enough prices have been collected.');
  }}

/* ---- tilt ---- */
const fine=matchMedia('(hover:hover) and (pointer:fine)').matches&&!matchMedia('(prefers-reduced-motion:reduce)').matches;
function bindTilt(card,mx,my){card.addEventListener('pointermove',e=>{const r=card.getBoundingClientRect();
  const x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
  card.style.setProperty('--rx',(-y*mx).toFixed(2)+'deg');card.style.setProperty('--ry',(x*my).toFixed(2)+'deg');
  card.style.setProperty('--mx',(x*100+50)+'%');card.style.setProperty('--my',(y*100+50)+'%');card.classList.add('hot');});
  card.addEventListener('pointerleave',()=>{card.style.setProperty('--rx','0deg');card.style.setProperty('--ry','0deg');card.classList.remove('hot');});}
function initTilt(){if(!fine)return;document.querySelectorAll('.tilt').forEach(c=>bindTilt(c,9,11));const d=document.getElementById('deal');if(d)bindTilt(d,6,8);}
let ticking=false;
function scrollTilt(){if(!fine)return;document.querySelectorAll('.tilt').forEach(card=>{const r=card.getBoundingClientRect(),vh=innerHeight,d=(r.top+r.height/2-vh/2)/vh;
  card.style.setProperty('--srx',(Math.max(-1,Math.min(1,d))*7).toFixed(2)+'deg');card.style.setProperty('--lift',(Math.max(0,1-Math.abs(d)*1.6)*8).toFixed(1)+'px');});}
addEventListener('scroll',()=>{if(!ticking){ticking=true;requestAnimationFrame(()=>{scrollTilt();ticking=false;});}},{passive:true});

/* ---- reveal + boot ---- */
function fireReveal(el){el.classList.add('in');
  el.querySelectorAll&&el.querySelectorAll('.vn[data-to]').forEach(animateCount);
  el.querySelectorAll&&el.querySelectorAll('.bar>i[data-w]').forEach(b=>b.style.width=b.dataset.w+'%');}
function revealAll(){document.querySelectorAll('.reveal').forEach(fireReveal);document.querySelectorAll('.vn[data-to]').forEach(animateCount);}
if(!('IntersectionObserver' in window))revealAll();
else{const io=new IntersectionObserver(es=>es.forEach(e=>{if(!e.isIntersecting)return;fireReveal(e.target);io.unobserve(e.target);}),{threshold:.14});
  document.querySelectorAll('.reveal').forEach(el=>io.observe(el));}
setTimeout(()=>document.querySelectorAll('.reveal:not(.in)').forEach(el=>{if(el.getBoundingClientRect().top<innerHeight*1.2)fireReveal(el);}),1200);

/* affiliate disclosure (config-driven, only when monetization is on) */
(function(){const el=document.getElementById('affdisc');
  if(el&&MON.enabled&&MON.disclosure)el.textContent=MON.disclosure;})();

renderDeal();renderHeroChips();initTilt();scrollTilt();
requestAnimationFrame(drawCharts);
loadWeather();loadRates();
/* set the initial "All routes" scope (activates the pill, tags the body) */
if(document.getElementById('routebar'))setScope('all');
</script></body></html>'''
