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
renders the "Faro" visual identity -- a Linear-inspired cool-neutral dark canvas
(dark by default) with a single restrained indigo accent (#5E6AD2), flat fills
over gradients, and Sora + IBM Plex Mono type -- with restrained motion (and full
prefers-reduced-motion support), entirely client-side. Semantic traffic-light
colours stay distinct (green = buy, amber = wait, grey = watch). A cool-neutral
light theme is one toggle away. Handles the empty / early-days case gracefully.
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
    # Further carriers that appear in scraped combos on these corridors. Listing
    # them lets the tokenizer split glued labels like "Air IndiaANA" cleanly
    # instead of surfacing the second carrier stuck to the first.
    "EVA Air", "Royal Brunei", "Chongqing Airlines", "Korean Air", "flydubai",
    "Condor", "ANA",
], key=len, reverse=True)

# Lower-cased lookup so the comma fallback can tell a real carrier (incl. short
# acronyms like "ANA") from a stray airport code ("SYD") it must drop.
_KNOWN_SET = {a.lower() for a in _KNOWN_AIRLINES}

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
    "EVA Air": "BR", "Royal Brunei": "BI", "Chongqing Airlines": "OQ", "flydubai": "FZ",
    "Condor": "DE", "ANA": "NH", "THAI": "TG",
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
    # Treat commas/slashes as separators; never split on camelCase. Drop tokens
    # that are bare 3-letter airport codes -- the scraper sometimes captures a
    # layover/route list ("SYD, CAN, CKG" or "CHC, SIN") where a carrier should
    # be. Real airline acronyms (e.g. "ANA") stay because they're known carriers.
    parts = []
    for tok in re.split(r"\s*[,/]\s*", name):
        tok = tok.strip()
        if not tok:
            continue
        if re.fullmatch(r"[A-Z]{3}", tok) and tok.lower() not in _KNOWN_SET:
            continue
        if tok not in parts:
            parts.append(tok)
    return " + ".join(parts)


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


def _via_airports(val, origin="", dest="", stops=None):
    """The actual CONNECTION airport(s) for an offer -- never the trip's own
    endpoints. Google's row sometimes lists the origin/destination chips alongside
    the real layover (e.g. a 1-stop CHC->CMB shows "CHC, CMB, MEL" where only MEL
    is the connection), which made the dashboard claim impossible "via" lists. We
    strip the endpoints and, when we know the stop count, keep at most that many
    codes so a 1-stop flight can never show two layovers."""
    ends = {str(origin).upper(), str(dest).upper()}
    codes = [c for c in _clean_layover(val).split(", ") if c and c not in ends]
    if stops is not None and stops >= 0:
        codes = codes[:int(stops)]
    return ", ".join(codes)


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
    """The DISTINCT flight options from each itinerary's most recent scan.

    Each returned offer is a genuinely distinct flight, described with exactly
    what the source gives us -- operating carrier(s), stops, the real connection
    airport(s) and total duration -- so the dashboard can be specific instead of
    collapsing a carrier to one line. Two clean-ups keep it honest:

      * the same physical flight is often scraped more than once (e.g. a Jetstar
        row plus a duplicate whose airline failed to parse); we fold those into
        one option, keeping the row that actually names a carrier; and
      * "via" lists only true connection airports, never the trip's own endpoints.

    We never fabricate a flight number or a departure time -- those live only on
    the booking page, which the Book CTA deep-links to.
    """
    out = {}
    for itin, h in ok.groupby("itin"):
        day = h[h["scan_date"] == h["scan_date"].max()]
        if day.empty:
            continue
        o = str(day["origin"].iloc[0])
        d = str(day["destination"].iloc[0])
        named, blanks, phys, seen_blank = [], [], set(), set()
        for r in day.sort_values("price").itertuples():
            air = clean_airline(getattr(r, "airline", ""))
            stops = int(r.stops) if pd.notna(r.stops) else 0
            via = _via_airports(getattr(r, "layover", ""), o, d, stops)
            dur = (int(r.duration_minutes)
                   if pd.notna(r.duration_minutes) and r.duration_minutes > 0 else 0)
            price = float(r.price)
            pk = (stops, via, dur, round(price))          # one physical flight
            offer = {"price": price, "airline": air, "iata": airline_iata(air),
                     "stops": stops, "via": via, "duration": dur}
            if air:
                ik = (offer["iata"],) + pk
                if ik not in phys:                        # distinct named flight
                    phys.add(ik)
                    phys.add(("*",) + pk)                 # mark physical-covered
                    named.append(offer)
            else:
                blanks.append((pk, offer))
        # Keep an unnamed offer only if no named flight already represents it.
        for pk, offer in blanks:
            if ("*",) + pk in phys or pk in seen_blank:
                continue
            seen_blank.add(pk)
            named.append(offer)
        out[itin] = sorted(named, key=lambda x: x["price"])[:per_itin]
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
        lay = _via_airports(getattr(r, "layover", ""),
                            getattr(r, "origin", ""), getattr(r, "destination", ""), st)
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
            "via": _via_airports(getattr(cheap, "layover", ""),
                                 cheap["origin"], cheap["destination"],
                                 int(cheap["stops"]) if pd.notna(cheap["stops"]) else None),
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


def _flex_surface(explore):
    """Compact per-route price surface for the client-side Flexibility Engine.

    The Answer must answer "could I pay less by shifting my dates / length /
    origin?" WITHOUT pulling the multi-MB finder grid (explore.json). So we
    project the explore grid down to just the cheapest price per (departure date
    x trip length) per corridor, encoded as tightly as possible so it stays in
    the Answer's critical path:

        { "CHC-CMB": {"base": "2026-06-22", "cells": [[depOffsetDays, len, price], ...]} }

    `base` + `depOffset` + `len` reconstruct the exact alternative itinerary (and
    its book deep-link) client-side (dep = base + offset, ret = dep + len). Richer
    per-option detail (carrier, stops, via, times) lives in the Lab's lazy-loaded
    surface view, so the Answer payload stays small.
    """
    from datetime import date as _date
    grouped = {}
    for e in explore:
        if e.get("len") is None:
            continue
        grouped.setdefault(f'{e["o"]}-{e["d"]}', []).append(
            (str(e["dep"]), int(e["len"]), int(e["min"])))
    out = {}
    for route, rows in grouped.items():
        base = min(r[0] for r in rows)
        b = _date.fromisoformat(base)
        cells = sorted(
            [[(_date.fromisoformat(dep) - b).days, ln, pr] for dep, ln, pr in rows],
            key=lambda c: (c[0], c[1]))
        out[route] = {"base": base, "cells": cells}
    return out


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
    explore, explore_meta, focus, surface = [], {}, None, {}
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
        surface = _flex_surface(explore)

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
            # Without this, the "Best moment to lock it in" panel renders one card
            # per model rec across the WHOLE grid (~230) -- a wall of cards that
            # also stretches its neighbouring fan-chart panel into a huge blank.
            if ai.get("best_time_to_book"):
                ai["best_time_to_book"] = [b for b in ai["best_time_to_book"]
                                           if b.get("itin") in fixed_keys]

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
        "surface": surface,
        "focus": focus,
        "routes_overview": routes_overview,
        "highlights": highlights,
        "backtests": (predict.backtests_by_route(df)
                      if not df.empty and (df["status"] == "ok").any() else {}),
        "monetization": _monetization(),
        "stats": stats,
        "cities": cities,
        "primary": primary,
        "ai": ai,
        "model": ({"mae": round(bundle["mae"]), "n": bundle["n"],
                   "conformal": (round(bundle["conformal"]) if bundle.get("conformal") else None),
                   "coverage": round(bundle.get("coverage", 0.8) * 100),
                   "band_method": bundle.get("band_method", "conformal"),
                   "empirical_coverage": (round(bundle["empirical_coverage"] * 100)
                                          if bundle.get("empirical_coverage") is not None else None),
                   "features": len(bundle.get("features", [])),
                   "drop_classifier": bool(bundle.get("drop_clf") is not None),
                   "exogenous": bundle.get("exogenous")} if bundle else None),
        "currency": (df[df["status"] == "ok"]["currency"].iloc[0]
                     if not df.empty and (df["status"] == "ok").any() else "NZD"),
    }

    os.makedirs(DOCS_DIR, exist_ok=True)
    if explore:
        with open(os.path.join(DOCS_DIR, "explore.json"), "w", encoding="utf-8") as ef:
            json.dump(explore, ef, default=_np)
    payload["explore"] = []

    # Keep the Answer's critical path light: embed only the sections the three-view
    # app actually reads, and lazy-load the ~MB finder grid (explore.json) outside
    # it. The heavy analytics/insights are still computed (they feed alerts.py and
    # are guarded by their own tests) -- they're just not shipped to the browser,
    # which roughly halves the page. `ai` alone was ~170 KB of unused JSON.
    embed_keys = {"generated", "status", "recs", "history", "explore", "explore_meta",
                  "surface", "latest_offers", "routes_overview", "backtests",
                  "monetization", "stats", "cities", "currency", "model"}
    embedded = {k: v for k, v in payload.items() if k in embed_keys}
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(_html(embedded))
    _write_firebase_sw()
    print(f"Dashboard built: {stats['scans']} scans, {stats['total_offers']} fare "
          f"observations across {stats['routes']} routes"
          f"{', model ±'+str(round(bundle['mae'])) if bundle else ''}.")


def _write_firebase_sw():
    """Emit docs/firebase-messaging-sw.js for FCM web push -- ONLY when a public
    Firebase config is provided. Without it the site stays in demo mode and no
    service worker is shipped (so the $0, no-account build is unchanged)."""
    raw = (os.environ.get("FARO_FIREBASE_CONFIG") or "").strip()
    sw_path = os.path.join(DOCS_DIR, "firebase-messaging-sw.js")
    if not raw:
        # Keep the published site clean if the config was later removed.
        try:
            os.remove(sw_path)
        except OSError:
            pass
        return
    try:
        cfg = json.loads(raw)
    except Exception:
        return
    sw = (
        "/* Auto-generated by flightwatch.dashboard for FCM web push. */\n"
        "importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-app-compat.js');\n"
        "importScripts('https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging-compat.js');\n"
        "firebase.initializeApp(" + json.dumps(cfg) + ");\n"
        "const messaging = firebase.messaging();\n"
        "messaging.onBackgroundMessage(function(payload){\n"
        "  const n = payload.notification || {};\n"
        "  self.registration.showNotification(n.title || 'Faro', {body: n.body || ''});\n"
        "});\n"
    )
    with open(sw_path, "w", encoding="utf-8") as f:
        f.write(sw)


def _np(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not serializable: {type(o)}")


def _firebase_config_js():
    """Read the PUBLIC Firebase web config from the environment (a GitHub repo
    variable, e.g. FARO_FIREBASE_CONFIG, holding the JSON Firebase hands you).

    The web config (apiKey, projectId, ...) is NOT a secret -- it only identifies
    the project; security is enforced by Firestore rules + Auth. When unset the
    snippet is empty, so the page omits ``window.FARO_FIREBASE`` and the Watch
    falls back to localStorage demo mode -- the $0, no-account promise survives.
    The Admin service-account JSON is never read here; it only lives in CI.
    """
    raw = (os.environ.get("FARO_FIREBASE_CONFIG") or "").strip()
    if not raw:
        return ""
    try:
        cfg = json.loads(raw)
        if not isinstance(cfg, dict) or not cfg.get("apiKey"):
            return ""
    except Exception:
        return ""
    vapid = (os.environ.get("FARO_FCM_VAPID_KEY") or "").strip()
    out = "window.FARO_FIREBASE=" + json.dumps(cfg) + ";"
    if vapid:
        out += "window.FARO_FCM_VAPID=" + json.dumps(vapid) + ";"
    return out


def _prerender_verdict(p):
    """A static, no-JS-needed snapshot of the headline verdict, injected into the
    Answer view so the page is readable (and indexable) before the script runs.
    The client replaces it with the live, interactive verdict on boot."""
    recs = p.get("recs") or []
    if not recs:
        st = (p.get("status") or {})
        msg = ("Collecting the first fares for the tracked routes — the verdict "
               "lights up as soon as there's price history to reason about.")
        return ('<div class="verdict s-WATCH"><div class="ctx mono">Faro</div>'
                '<div class="sigrow"><div class="signal">SOON</div></div>'
                '<div class="reason">' + msg + '</div></div>')
    r = recs[0]
    import re as _re
    m = _re.match(r"^([A-Z]{3})-([A-Z]{3}) (\d{4}-\d{2}-\d{2}) -> (\d{4}-\d{2}-\d{2})$",
                  r.get("itinerary", ""))
    o, d = (m.group(1), m.group(2)) if m else ("", "")
    cur = p.get("currency", "NZD")
    sig = r.get("signal", "WATCH")
    reason = (r.get("reason") or "").replace("<", "&lt;").replace(">", "&gt;")
    now = round(float(r.get("price", 0)))
    low = round(float(r.get("predicted_low") or now))
    conf = r.get("confidence", "")
    return (
        '<div class="verdict s-' + sig + '">'
        '<div class="ctx mono">' + o + ' &rarr; ' + d + '</div>'
        '<div class="sigrow"><div class="signal">' + sig + '</div></div>'
        '<div class="reason">' + reason + '</div>'
        '<div class="conf"><span>Confidence ' + str(conf) + '%</span></div>'
        '<div class="nums">'
        '<div class="num"><div class="v mono">' + cur + ' ' + format(now, ",") + '</div><div class="k">fare now</div></div>'
        '<div class="num"><div class="v mono">' + cur + ' ' + format(low, ",") + '</div><div class="k">forecast low</div></div>'
        '</div></div>')


def _html(p):
    data = json.dumps(p, default=_np)
    fb_js = _firebase_config_js()
    prerender = _prerender_verdict(p)
    return r'''<!DOCTYPE html><html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Faro &middot; know when to book</title>
<meta name="description" content="Tell Faro your trip and get one honest buy / wait / watch verdict from real scraped fares — best-buy window, confidence with provenance, 7-day fare weather, and per-route accuracy. Pin trips and get alerted when it's time to buy.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preconnect" href="https://pics.avs.io" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  /* Linear-inspired cool-neutral dark, dark by default. A single indigo accent
     carries interactivity; semantic traffic-light hues stay distinct. */
  --bg:#08090A;--card:#101216;--card2:#16181d;--ink:#E7E8EA;--muted:#8A8F98;--dim:#5b5f66;
  --line:#1b1d22;--line2:#26282e;
  --brand:#5E6AD2;--brand2:#7C87E8;--on-brand:#F4F5FD;
  --buy:#4cb782;--buy-bg:rgba(76,183,130,.12);
  --wait:#d8a23e;--wait-bg:rgba(216,162,62,.13);
  --watch:#8b9098;--watch-bg:rgba(255,255,255,.05);
  --down:#4cb782;--up:#e5484d;--cloud:#4ea7fc;
  --shadow:0 14px 38px -18px rgba(0,0,0,.6);--shadow-lg:0 30px 66px -24px rgba(0,0,0,.72);
  --r-sm:10px;--r-md:14px;--r-lg:20px;--r-pill:999px;
  --wrap:760px;          /* reading-width single column; widened on large displays */
}
html[data-theme="light"]{
  /* Cool-neutral light (not warm cream), same indigo accent. */
  --bg:#fbfbfc;--card:#ffffff;--card2:#f4f5f7;--ink:#15161a;--muted:#62666d;--dim:#8a8f98;
  --line:#ececef;--line2:#e1e2e7;
  --brand:#5E6AD2;--brand2:#4a55c0;--on-brand:#ffffff;
  --buy-bg:rgba(31,157,99,.10);--wait-bg:rgba(180,130,40,.14);--watch-bg:rgba(0,0,0,.05);
  --buy:#1f9d63;--down:#1f9d63;--up:#d83a3f;--cloud:#2f6bff;--watch:#71757c;
  --shadow:0 14px 30px -20px rgba(20,22,30,.16);--shadow-lg:0 26px 60px -28px rgba(20,22,30,.20);
}
*{margin:0;box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:'Sora',system-ui,sans-serif;
  line-height:1.55;-webkit-font-smoothing:antialiased;overflow-x:hidden}
.mono{font-family:'IBM Plex Mono',monospace;font-variant-numeric:tabular-nums}
a{color:var(--brand);text-decoration:none}
button{font-family:inherit;cursor:pointer}
input,select{font-family:inherit}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px;border-radius:4px}
.aurora{position:fixed;inset:0;z-index:-1;background:
  radial-gradient(820px 460px at 50% -16%,color-mix(in srgb,var(--brand) 15%,transparent) 0,
  color-mix(in srgb,var(--brand) 4%,transparent) 46%,transparent 72%),var(--bg)}
.wrap{max-width:var(--wrap);margin:0 auto;padding:0 18px 110px}
.hidden{display:none!important}
/* Below the large breakpoint the Answer columns are transparent wrappers, so the
   page stacks exactly as a single column (unchanged on mobile/tablet). They turn
   into a real two-column layout only when there's room -- see the @media below. */
.answer-grid,.answer-main,.answer-aside{display:contents}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}

.shell{position:sticky;top:0;z-index:20;backdrop-filter:blur(10px);
  background:color-mix(in srgb,var(--bg) 82%,transparent);border-bottom:1px solid var(--line)}
.shell-in{max-width:var(--wrap);margin:0 auto;display:flex;align-items:center;gap:12px;padding:11px 18px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;letter-spacing:-.01em}
.mark{width:24px;height:24px;border-radius:7px;background:var(--brand);
  display:grid;place-items:center;color:var(--on-brand);font-weight:700;font-size:14px}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--line2);border-radius:var(--r-pill);padding:3px}
.tab{border:none;background:none;color:var(--muted);font-size:13px;font-weight:600;padding:6px 14px;border-radius:var(--r-pill)}
.tab.on{background:var(--brand);color:var(--on-brand)}
.tab .badge{display:inline-block;min-width:17px;margin-left:5px;padding:0 4px;border-radius:9px;font-size:11px;
  background:color-mix(in srgb,var(--ink) 12%,transparent);font-family:'IBM Plex Mono',monospace}
.tab.on .badge{background:rgba(0,0,0,.18)}
.spacer{flex:1}
.outlook{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}
.outlook b{color:var(--ink)}
.iconbtn{background:var(--card);border:1px solid var(--line2);color:var(--ink);width:34px;height:34px;
  border-radius:10px;display:grid;place-items:center;font-size:15px}

.idstrip{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:18px 0 2px;padding:10px 14px;
  border:1px solid var(--line2);border-radius:var(--r-md);background:var(--card);font-size:12.5px;color:var(--muted)}
.iddot{width:8px;height:8px;border-radius:50%}
.iddot.anon{background:var(--dim)} .iddot.cloud{background:var(--cloud);box-shadow:0 0 0 3px color-mix(in srgb,var(--cloud) 25%,transparent)}
.idstrip b{color:var(--ink)}
.btn-sm{margin-left:auto;border:1px solid var(--line2);background:var(--card2);color:var(--ink);
  border-radius:var(--r-pill);padding:6px 13px;font-size:12.5px;font-weight:600;display:inline-flex;align-items:center;gap:7px}
.btn-sm.cloud{border-color:var(--cloud);color:var(--cloud)}
.gicon{width:14px;height:14px;border-radius:50%;background:conic-gradient(#ea4335 0 25%,#fbbc05 0 50%,#34a853 0 75%,#4285f4 0)}

.kicker{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--brand);font-weight:700;margin:24px 0 10px}
.composer{background:var(--card);border:1px solid var(--line2);border-radius:var(--r-lg);
  padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:10px;box-shadow:var(--shadow)}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
.field select{appearance:none;background:var(--card2);border:1px solid var(--line2);color:var(--ink);
  border-radius:var(--r-sm);padding:10px 12px;font-family:'IBM Plex Mono',monospace;font-size:14px;font-weight:500;
  background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
  background-position:calc(100% - 16px) 18px,calc(100% - 11px) 18px;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.flexrow{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:12.5px;color:var(--muted);padding-top:2px}
.flexrow label{display:flex;align-items:center;gap:7px;cursor:pointer}
.linkbtn{background:none;border:1px solid var(--line2);color:var(--muted);border-radius:var(--r-pill);padding:5px 11px;font-size:12px;display:inline-flex;align-items:center;gap:6px}
.linkbtn:hover{color:var(--ink);border-color:var(--brand)}

.verdict{margin-top:14px;background:var(--card);border:1px solid var(--line2);border-radius:var(--r-lg);
  padding:22px;box-shadow:var(--shadow-lg);position:relative;overflow:hidden}
.verdict::before{content:"";position:absolute;inset:0 0 auto 0;height:3px}
.verdict.s-BUY::before{background:var(--buy)} .verdict.s-WAIT::before{background:var(--wait)} .verdict.s-WATCH::before{background:var(--watch)}
.ctx{font-size:12.5px;color:var(--muted)} .ctx b{color:var(--ink)}
.sigrow{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:8px 0 2px}
.signal{font-size:46px;font-weight:700;letter-spacing:-1.6px;line-height:1}
.s-BUY .signal{color:var(--buy)} .s-WAIT .signal{color:var(--wait)} .s-WATCH .signal{color:var(--watch)}
.window{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;background:var(--card2);
  border:1px solid var(--line2);border-radius:var(--r-pill);padding:6px 13px}
.window b{font-family:'IBM Plex Mono',monospace}
.live{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--cloud)}
.live .dot{width:7px;height:7px;border-radius:50%;background:var(--cloud);animation:pulse 2.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.reason{font-size:18px;margin:10px 0 16px;max-width:46ch}
.conf{display:flex;align-items:center;gap:10px;margin-bottom:18px;font-size:12.5px;color:var(--muted);flex-wrap:wrap}
.track{display:flex;gap:3px}.pip{width:22px;height:7px;border-radius:3px;background:var(--line2)}
.s-BUY .pip.on{background:var(--buy)} .s-WAIT .pip.on{background:var(--wait)} .s-WATCH .pip.on{background:var(--watch)}
.nums{display:flex;gap:22px;flex-wrap:wrap;padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.num .v{font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:600}
.num .v.move.dn{color:var(--down)} .num .v.move.up{color:var(--up)}
.num .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin-top:2px}
.arrow{color:var(--dim);align-self:center;font-size:18px}
.cta{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;align-items:center}
.btn{border-radius:var(--r-md);padding:13px 18px;font-size:14.5px;font-weight:700;border:1px solid transparent;display:inline-flex;align-items:center;gap:8px}
.btn-primary{background:var(--brand);color:var(--on-brand);
  border-color:color-mix(in srgb,var(--brand) 62%,#000)}
.btn-primary:hover{background:color-mix(in srgb,var(--brand) 88%,#fff)}
.btn-ghost{background:var(--card2);border-color:var(--line2);color:var(--ink)}
.btn-pin.on{background:var(--buy-bg);border-color:var(--buy);color:var(--buy)}
.auditlink{margin-left:auto;align-self:center;background:none;border:none;color:var(--muted);font-size:13px;display:inline-flex;align-items:center;gap:6px}
.auditlink:hover{color:var(--ink)}
.audit{margin-top:14px;border-top:1px dashed var(--line2);padding-top:14px;display:none;font-size:14px;color:var(--muted)}
.audit.open{display:block;animation:fade .3s ease}
.audit ul{margin:8px 0 0;padding-left:18px;display:grid;gap:5px}
.altbook{display:block;font-size:12px;color:var(--dim);margin-top:8px}
.altbook a{color:var(--muted)}.altbook a:hover{color:var(--brand)}
@keyframes fade{from{opacity:0;transform:translateY(-4px)}to{opacity:1}}

.panel{margin-top:14px;background:var(--card);border:1px solid var(--line2);border-radius:var(--r-lg);padding:16px 18px}
.panel-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}
.panel-head h3{font-size:14px;font-weight:600}.panel-head span{font-size:12px;color:var(--dim)}
.wx-cells{display:grid;grid-template-columns:repeat(7,1fr);gap:7px}
.wx{border-radius:var(--r-sm);padding:9px 4px;text-align:center;border:1px solid var(--line2);background:var(--card2)}
.wx .d{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.wx .p{font-family:'IBM Plex Mono',monospace;font-size:12.5px;margin-top:5px;font-weight:500}
.wx.best{border-color:var(--brand);box-shadow:0 0 0 1px var(--brand) inset}
.wx.best .tag{font-size:9px;color:var(--brand);margin-top:3px;font-weight:700}
.wx .bar{height:4px;border-radius:3px;margin-top:6px}
svg.spark{width:100%;height:90px;display:block}
.trust{margin-top:14px;font-size:12.5px;color:var(--dim);line-height:1.7;text-align:center}.trust b{color:var(--muted)}

/* ── specific flight options ────────────────────────────────────────────── */
.optrow{display:grid;grid-template-columns:auto 1fr auto auto;gap:11px;align-items:center;
  padding:10px 0;border-bottom:1px solid var(--line);font-size:13px}
.optrow:last-of-type{border-bottom:none}
.opt-logo{width:22px;height:22px;border-radius:5px;background:var(--card2);object-fit:contain;border:1px solid var(--line2)}
.opt-main b{font-weight:600}
.opt-meta{font-size:11.5px;color:var(--muted)}
.opt-pr{font-family:'IBM Plex Mono',monospace;font-weight:600;text-align:right}
.opt-best{font-size:9px;color:var(--buy);font-weight:700;letter-spacing:.06em;display:block}
.opt-note{font-size:11.5px;color:var(--dim);margin-top:10px;line-height:1.6}
.opt-note b{color:var(--muted)}

/* ── flexibility engine ─────────────────────────────────────────────────── */
.flex-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:4px}
.flexcard{position:relative;background:var(--card2);border:1px solid var(--line2);border-radius:var(--r-md);
  padding:12px 13px;text-align:left;display:flex;flex-direction:column;gap:3px;transition:border-color .15s,transform .15s}
.flexcard:hover{border-color:var(--buy);transform:translateY(-2px)}
.flexcard .fx-save{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:18px;color:var(--buy)}
.flexcard .fx-what{font-size:13px;font-weight:600;color:var(--ink)}
.flexcard .fx-sub{font-size:11.5px;color:var(--muted)}
.flexcard .fx-book{margin-top:6px;font-size:11.5px;color:var(--brand);display:inline-flex;align-items:center;gap:4px;align-self:flex-start}
.flexcard .fx-book svg{width:11px;height:11px}
.flex-none{font-size:12.5px;color:var(--muted);padding:4px 0}
.flexbar-wrap{margin-top:14px}
.flexbar-cap{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px}
.flexbar{display:flex;gap:3px;align-items:flex-end;height:64px}
.flexbar .fb-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;min-width:0}
.flexbar .fb-bar{width:100%;border-radius:3px 3px 0 0;background:var(--brand);transition:opacity .15s}
.flexbar .fb-col:hover .fb-bar{opacity:.75}
.flexbar .fb-col.anchor .fb-bar{background:var(--ink)}
.flexbar .fb-col.cheap .fb-bar{background:var(--buy)}
.flexbar .fb-d{font-size:8.5px;color:var(--dim);font-family:'IBM Plex Mono',monospace;white-space:nowrap}
.flexbar-legend{display:flex;gap:14px;font-size:11px;color:var(--dim);margin-top:8px;flex-wrap:wrap}
.flexbar-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:middle}

/* ── price surface (Lab) ────────────────────────────────────────────────── */
.surf-scroll{overflow-x:auto;margin-top:10px;padding-bottom:6px}
.surf{display:grid;gap:2px;width:max-content}
.surf-cell{width:11px;height:13px;border-radius:2px}
.surf-cell.empty{background:var(--inset,#101015);opacity:.5}
.surf-cell.best{outline:2px solid var(--brand);outline-offset:-1px}
.surf-rowlabel,.surf-collabel{font-size:8.5px;color:var(--dim);font-family:'IBM Plex Mono',monospace}
.surf-rowlabel{padding-right:6px;display:flex;align-items:center;justify-content:flex-end;white-space:nowrap}
.surf-legend{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--dim);margin-top:10px;flex-wrap:wrap}
.surf-ramp{height:8px;width:90px;border-radius:4px;background:linear-gradient(90deg,var(--buy),var(--brand),var(--up))}

.watch-head{display:flex;justify-content:space-between;align-items:center;margin:6px 0 4px}
.watch-head h2{font-size:20px;font-weight:700}
.simbtn{border:1px solid var(--cloud);color:var(--cloud);background:none;border-radius:var(--r-pill);padding:7px 13px;font-size:12.5px;font-weight:600}
.empty{margin-top:18px;padding:34px 22px;text-align:center;border:1px dashed var(--line2);border-radius:var(--r-lg);color:var(--muted)}
.tcard{margin-top:14px;background:var(--card);border:1px solid var(--line2);border-radius:var(--r-lg);padding:16px 18px;
  border-left:3px solid var(--brand)}
.tc-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.tc-route{font-weight:700;font-size:16px}
.chip{font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:var(--r-pill);letter-spacing:.02em}
.chip.BUY{background:var(--buy-bg);color:var(--buy)} .chip.WAIT{background:var(--wait-bg);color:var(--wait)} .chip.WATCH{background:var(--watch-bg);color:var(--watch)}
.tc-now{margin-left:auto;font-family:'IBM Plex Mono',monospace;font-weight:600}
.tc-sub{font-size:12.5px;color:var(--muted);margin-top:5px}
.tc-alerts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:13px;padding-top:13px;border-top:1px solid var(--line)}
.atog{border:1px solid var(--line2);background:var(--card2);color:var(--muted);border-radius:var(--r-pill);
  padding:6px 12px;font-size:12.5px;font-weight:600;display:inline-flex;align-items:center;gap:6px}
.atog.on{border-color:var(--buy);color:var(--buy);background:var(--buy-bg)}
.target{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}
.target input{width:78px;background:var(--card2);border:1px solid var(--line2);color:var(--ink);border-radius:var(--r-sm);
  padding:6px 8px;font-family:'IBM Plex Mono',monospace;font-size:12.5px}
.tc-actions{margin-left:auto;display:flex;gap:8px}
.txt-link{background:none;border:none;color:var(--dim);font-size:12.5px}.txt-link:hover{color:var(--up)}
.fb-explain{margin-top:26px;border:1px solid var(--line2);border-radius:var(--r-md);overflow:hidden}
.fb-explain summary{cursor:pointer;padding:13px 16px;font-size:13px;font-weight:600;color:var(--cloud);list-style:none}
.fb-explain summary::-webkit-details-marker{display:none}
.fb-explain .body{padding:2px 16px 16px;font-size:12.5px;color:var(--muted);line-height:1.7}
.fb-explain code{font-family:'IBM Plex Mono',monospace;color:var(--ink);background:var(--card2);padding:1px 5px;border-radius:5px}

.note{margin-top:30px;padding:14px 16px;border:1px dashed var(--line2);border-radius:var(--r-md);font-size:12.5px;color:var(--dim);line-height:1.65}

.lab-intro{font-size:12.5px;color:var(--muted);margin:6px 0 0}
.labsec{margin-top:14px;background:var(--card);border:1px solid var(--line2);border-radius:var(--r-lg);padding:16px 18px}
.labsec > h3{font-size:13px;font-weight:600;display:flex;align-items:baseline;gap:8px;margin-bottom:4px}
.labsec > h3 span{font-size:11.5px;color:var(--dim);font-weight:400}
.minichip{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:var(--r-pill)}
.minichip.BUY{background:var(--buy-bg);color:var(--buy)} .minichip.WAIT{background:var(--wait-bg);color:var(--wait)} .minichip.WATCH{background:var(--watch-bg);color:var(--watch)}
.focusbtn{border:1px solid var(--line2);background:none;color:var(--brand);border-radius:var(--r-pill);padding:4px 11px;font-size:12px;font-weight:600}
.focusbtn:hover{border-color:var(--brand)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.bt-strip{display:flex;gap:3px;flex-wrap:wrap;margin-top:8px}
.bt-strip i{width:14px;height:14px;border-radius:3px;display:inline-block}
.bt-strip i.r{background:var(--buy)} .bt-strip i.w{background:var(--up);opacity:.7}
.heat{display:grid;grid-template-columns:34px repeat(7,1fr);gap:4px;margin-top:10px}
.hlabel{font-size:9.5px;color:var(--dim);display:grid;place-items:center;font-family:'IBM Plex Mono',monospace}
.hcell{aspect-ratio:1.5;border-radius:5px;display:grid;place-items:center;font-size:9px;font-family:'IBM Plex Mono',monospace;color:#0b0b0e}
.hcell.best{outline:2px solid var(--brand);outline-offset:-1px}
.heat-legend{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--dim);margin-top:10px;flex-wrap:wrap}
.heat-ramp{height:8px;width:90px;border-radius:4px;background:linear-gradient(90deg,var(--buy),var(--brand),var(--up))}
svg.fan{width:100%;height:150px;display:block;margin-top:8px}
.finder-bar{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-top:8px}
.ff{display:flex;flex-direction:column;gap:4px}
.ff label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.ff select,.ff input[type=range]{background:var(--card2);border:1px solid var(--line2);color:var(--ink);border-radius:var(--r-sm);padding:7px 9px;font-size:13px}
.ff input[type=range]{padding:0;width:140px}
.fcount{font-size:12px;color:var(--muted);margin-left:auto}
.fres{display:grid;grid-template-columns:auto 1fr auto auto;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.fres .pr{font-family:'IBM Plex Mono',monospace;font-weight:600;text-align:right}
.fres .meta{font-size:11.5px;color:var(--muted)}
.bookrow{font-size:11.5px;color:var(--brand);display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.bookrow svg{width:11px;height:11px}
.gate-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}
.gate-card{background:var(--card);border:1px solid var(--line2);border-radius:var(--r-lg);padding:16px;cursor:pointer;text-align:left;transition:border-color .15s,transform .15s}
.gate-card:hover{border-color:var(--brand);transform:translateY(-2px)}
.gate-top{display:flex;align-items:center;gap:10px}
.gate-route{font-weight:700;font-size:16px}
.gate-acc{margin-top:12px;display:flex;gap:16px;font-size:11.5px;color:var(--dim);line-height:1.4}
.gate-acc b{display:block;font-family:'IBM Plex Mono',monospace;font-size:14px;color:var(--ink);font-weight:600}
.gate-cta{margin-top:12px;color:var(--brand);font-size:13px;font-weight:600}
.changebar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 0 0}
.changebar h2{font-size:20px;font-weight:700}
.score-row{display:flex;gap:12px;flex-wrap:wrap}
.score-big{flex:1;min-width:140px;background:var(--card2);border:1px solid var(--line2);border-radius:var(--r-md);padding:14px}
.score-big .v{font-family:'IBM Plex Mono',monospace;font-size:30px;font-weight:700;line-height:1}
.score-big.acc .v{color:var(--brand)} .score-big.saved .v{color:var(--buy)} .score-big.now .v{color:var(--buy)}
.score-big .k{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-top:7px}
.score-big .s{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.45}
.savesvg{width:100%;height:60px;display:block;margin-top:6px}
.honest{font-size:12.5px;color:var(--muted);margin-top:14px;line-height:1.65}.honest b{color:var(--ink)}
@media(max-width:560px){.grid2{grid-template-columns:1fr}.gate-grid{grid-template-columns:1fr}}

#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);background:var(--ink);color:var(--bg);
  font-weight:600;font-size:13.5px;padding:11px 18px;border-radius:var(--r-pill);box-shadow:var(--shadow-lg);max-width:88vw;text-align:center;
  opacity:0;pointer-events:none;transition:.28s;z-index:50}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(max-width:560px){
  .signal{font-size:40px}.reason{font-size:16.5px}.nums{gap:16px}.num .v{font-size:19px}
  .auditlink{margin-left:0;width:100%;justify-content:flex-start;order:5}
  .shell-in{gap:8px}.outlook{order:5;width:100%}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}html{scroll-behavior:auto}}

/* --- Large displays: use the extra width meaningfully ----------------------- *
 * Widen the content column and split the Answer into a sticky two-column view:
 * the DECISION (composer + verdict) pinned on the left while the supporting
 * EVIDENCE (flights, flexibility, fare weather, story) scrolls on the right --
 * so the buy/wait call stays in sight as you read the detail. The Watch list
 * also flows into a responsive multi-column grid; Lab's two-up panels get room. */
@media(min-width:1120px){
  :root{--wrap:1120px}
  .answer-grid{display:grid;grid-template-columns:minmax(360px,420px) minmax(0,1fr);
    gap:28px;align-items:start}
  .answer-main{display:flex;flex-direction:column;gap:14px;position:sticky;top:74px}
  .answer-aside{display:flex;flex-direction:column;gap:14px;min-width:0}
  .answer-main>*,.answer-aside>*{margin-top:0}
  #tripList{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
    gap:14px;align-items:start}
  #tripList>*{margin-top:0}.empty{grid-column:1/-1}
}
@media(min-width:1500px){:root{--wrap:1280px}}
</style>
</head>
<body>
<div class="aurora"></div>

<header class="shell">
  <div class="shell-in">
    <div class="brand"><span class="mark">F</span>Faro</div>
    <nav class="tabs" aria-label="Views">
      <button class="tab" id="tab-answer" data-go="#/">Answer</button>
      <button class="tab" id="tab-watch" data-go="#/watch">Watch <span class="badge" id="tabN">0</span></button>
      <button class="tab" id="tab-lab" data-go="#/lab">Lab</button>
    </nav>
    <div class="spacer"></div>
    <div class="outlook" id="outlook"></div>
    <button class="iconbtn" id="themeBtn" title="Toggle theme" aria-label="Toggle light/dark theme">&#9790;</button>
  </div>
</header>

<main class="wrap">
  <div class="idstrip" id="idstrip"></div>

  <section id="view-answer" aria-label="Answer">
   <div class="answer-grid">
    <div class="answer-main">
     <div class="kicker">Tell me your trip</div>
     <div class="composer">
      <div class="field"><label for="f-o">From</label><select id="f-o"></select></div>
      <div class="field"><label for="f-d">To</label><select id="f-d"></select></div>
      <div class="field"><label for="f-dep">Leave around</label><select id="f-dep"></select></div>
      <div class="field"><label for="f-len">Trip length</label><select id="f-len"></select></div>
      <div class="flexrow">
        <label><input type="checkbox" id="f-flex" checked> dates flexible &plusmn;3 days</label>
        <button class="linkbtn" id="copyLink">&#128279; copy link to this trip</button>
      </div>
     </div>
     <div id="verdict">''' + prerender + r'''</div>
    </div>
    <div class="answer-aside">
     <div class="panel" id="options"></div>
     <div class="panel" id="flex"></div>
     <div class="panel" id="weather"></div>
     <div class="panel" id="story"></div>
     <p class="trust" id="trust"></p>
    </div>
   </div>
  </section>

  <section id="view-watch" class="hidden" aria-label="Watch">
    <div class="watch-head">
      <h2>My trips</h2>
      <button class="simbtn" id="simBtn">&#8635; simulate a new scan</button>
    </div>
    <p style="font-size:12.5px;color:var(--muted);margin-top:2px">Pinned trips, kept in sync. We watch each one and tell you the moment it's time to buy.</p>
    <div id="tripList"></div>
    <details class="fb-explain">
      <summary>&#9729; How the Watch syncs &amp; alerts (Firebase) &#9662;</summary>
      <div class="body" id="fbBody"></div>
    </details>
  </section>

  <section id="view-lab" class="hidden" aria-label="Lab">
    <div id="labBody"></div>
  </section>

  <div class="note" id="footnote"></div>
  <p style="font-size:11.5px;color:var(--dim);margin-top:14px;text-align:center" id="affdisc"></p>
</main>

<div id="toast" role="status" aria-live="polite"></div>

<script>''' + fb_js + r'''</script>
<script>
const D = ''' + data + r''';
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const CUR = D.currency || 'NZD';
const money = n => CUR+' '+Math.round(+n||0).toLocaleString('en-NZ');
const AIRPORTS = D.cities || {};
const MONN=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const fmtD = iso => {const m=String(iso||'').split('-');return m.length===3?(+m[2]+' '+MONN[+m[1]-1]):String(iso||'');};
const cityName = c => AIRPORTS[c] || c;
const $=id=>document.getElementById(id);

/* ── adapter: turn the real payload into the per-route model the views drive ── */
function parseItin(s){const m=String(s||'').match(/^([A-Z]{3})-([A-Z]{3}) (\d{4}-\d{2}-\d{2}) -> (\d{4}-\d{2}-\d{2})$/);
  return m?{o:m[1],d:m[2],dep:m[3],ret:m[4]}:null;}
function lenOf(dep,ret){const a=new Date(dep),b=new Date(ret);return Math.round((b-a)/86400000);}

const RECS_BY_ROUTE={};
(D.recs||[]).forEach(r=>{const p=parseItin(r.itinerary);if(!p)return;const k=p.o+'-'+p.d;
  (RECS_BY_ROUTE[k]=RECS_BY_ROUTE[k]||[]).push(Object.assign({},r,p,{len:lenOf(p.dep,p.ret)}));});

const BT=D.backtests||{};                                   // {route:{calls,right,hit_rate,saved_vs_searchday,missed_cost}}
const ROVER=(D.routes_overview||[]);
const ROUTE_CARDS={};ROVER.forEach(c=>ROUTE_CARDS[c.route]=c);
// corridors we can actually answer for (have at least one rec)
const ROUTE_KEYS=ROVER.filter(c=>c.has_data && RECS_BY_ROUTE[c.route]).map(c=>c.route);
const origins=[...new Set(ROUTE_KEYS.map(r=>r.split('-')[0]))];
const dests=[...new Set(ROUTE_KEYS.map(r=>r.split('-')[1]))];

// The composer only offers trips Faro actually has a model call for, so every
// selectable month/length resolves to a real verdict (recs are the covered set).
const EM=D.explore_meta||{};
const _allRecs=Object.values(RECS_BY_ROUTE).flat();
const MONTHS=[...new Set(_allRecs.map(r=>r.dep.slice(0,7)))].sort()
  .map(m=>{const[y,mm]=m.split('-');return[m,MONN[+mm-1]+' '+y];});
const LENGTHS=[...new Set(_allRecs.map(r=>r.len))].filter(Boolean).sort((a,b)=>a-b)
  .map(L=>[L,'~'+Math.round(L/7)+' weeks ('+L+'n)']);

/* representative call for a corridor = its best current opportunity (recs come
   pre-sorted BUY > WATCH > WAIT, then by confidence). */
function repRec(route){const list=RECS_BY_ROUTE[route];return list&&list.length?list[0]:null;}
function nearestRec(o,d,depMonth,len){const list=RECS_BY_ROUTE[o+'-'+d];if(!list||!list.length)return null;
  const tgt=monthIndex(depMonth);
  let best=null,bd=1e9;for(const r of list){const md=Math.abs(monthIndex(r.dep.slice(0,7))-tgt);
    const ld=Math.abs((r.len||21)-len);const score=md*4+ld;if(score<bd){bd=score;best=r;}}return best;}
function monthIndex(ym){const[y,m]=String(ym||'2026-09').split('-').map(Number);return y*12+(m||1);}

function outlookOf(r){if(r.signal==='BUY')return'low';
  if((r.expected_savings||0)>0||(r.momentum||0)<-1)return'falling';return'steady';}
function winOf(r){if(r.signal==='BUY')return 0;
  if(r.best_dtd!=null&&r.days_to_departure!=null)return Math.max(0,r.days_to_departure-r.best_dtd);return null;}
function whyOf(r,route){const W=[];const b=BT[route];
  if(r.percentile!=null)W.push('Current fare sits around the '+r.percentile+'th percentile of this route’s recent range.');
  if((r.expected_savings||0)>0)W.push('Model expects a low near '+money(r.predicted_low)+' — about '+money(r.expected_savings)+' under today.');
  if(r.prob_drop!=null)W.push('Estimated chance of a further near-term drop: '+r.prob_drop+'%.');
  if(b&&b.hit_rate!=null)W.push('Backtest: BUY/WAIT calls on this route paid off '+b.hit_rate+'% of the time ('+b.right+'/'+b.calls+').');
  else W.push('Still learning this route — too few graded calls to headline an accuracy figure yet.');
  if(r.method!=='model')W.push('Only '+r.points+' daily points so far — using the transparent heuristic, not the trained model.');
  return W;}

function verdictFor(o,d,depMonth,len){const r=nearestRec(o,d,depMonth,len);if(!r)return null;
  const route=o+'-'+d;const b=BT[route]||{};
  const now=Math.round(r.price),low=Math.round(r.predicted_low!=null?r.predicted_low:r.price);
  const itin=route+' '+r.dep+' -> '+r.ret;
  const hist=(D.history||{})[itin];const story=hist?hist.map(h=>Math.round(h.p)):null;
  return {o,d,dep:r.dep,ret:r.ret,len:r.len,sig:r.signal,conf:r.confidence,now,low,
    win:winOf(r),obs:r.points,hit:(b.hit_rate!=null?b.hit_rate:null),thin:(r.method!=='model'||r.points<5),
    out:outlookOf(r),reason:r.reason,why:whyOf(r,route),curve:r.curve||[],dtd:r.days_to_departure,
    movePct:now?Math.round((low-now)/now*100):0,itin,airline:ROUTE_CARDS[route]?ROUTE_CARDS[route].airline:''};}

/* ══════════ Flexibility Engine — what flexibility is worth ══════════════════
   Reads the compact per-route price surface (D.surface), and for the focused
   trip finds the cheapest nearby alternatives across three levers: shift the
   departure date (same length), change the trip length (same dates), or fly from
   another NZ origin. Every suggested saving must clear a gate = max(material 4%,
   the model's conformal band) so we surface real money, never sampling noise. */
const SURFACE=D.surface||{};
function isoAdd(iso,days){const d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+days);return d.toISOString().slice(0,10);}
function dayDiff(a,b){return Math.round((new Date(b+'T00:00:00Z')-new Date(a+'T00:00:00Z'))/86400000);}
function surfaceCells(route){const s=SURFACE[route];if(!s)return null;
  return s.cells.map(c=>({off:c[0],len:c[1],p:c[2],dep:isoAdd(s.base,c[0])}));}
function flexFor(o,d,dep,len,fallbackPrice){
  const route=o+'-'+d,S=SURFACE[route];if(!S)return null;
  const cells=surfaceCells(route);const anchorOff=dayDiff(S.base,dep);
  const anchorCell=cells.find(c=>c.dep===dep&&c.len===len);
  const aPrice=anchorCell?anchorCell.p:(fallbackPrice||null);
  if(aPrice==null)return null;
  const band=(D.model&&D.model.conformal)||0;
  const gate=Math.max(Math.round(aPrice*0.04),band,40);
  const sug=[];
  // (1) shift departure date, same length, within ±21 days
  const sameLen=cells.filter(c=>c.len===len&&Math.abs(c.off-anchorOff)<=21&&c.dep!==dep);
  const day=sameLen.filter(c=>aPrice-c.p>=gate).sort((a,b)=>a.p-b.p)[0];
  if(day){const dd=day.off-anchorOff;
    sug.push({kind:'date',dir:dd<0?'earlier':'later',days:Math.abs(dd),o,d,dep:day.dep,len,price:day.p,save:aPrice-day.p});}
  // (2) change trip length, near the same departure (±3 days)
  const nearDep=cells.filter(c=>Math.abs(c.off-anchorOff)<=3&&c.len!==len);
  const lenAlt=nearDep.filter(c=>aPrice-c.p>=gate).sort((a,b)=>a.p-b.p)[0];
  if(lenAlt){sug.push({kind:'len',delta:lenAlt.len-len,o,d,dep:lenAlt.dep,len:lenAlt.len,price:lenAlt.p,save:aPrice-lenAlt.p});}
  // (3) fly from another NZ origin to the same destination, comparable dates/length
  let alt=null;
  Object.keys(SURFACE).forEach(rk=>{const p=rk.split('-');if(p[1]!==d||p[0]===o)return;
    const s2=SURFACE[rk];const cand=s2.cells.map(c=>({len:c[1],p:c[2],dep:isoAdd(s2.base,c[0])}))
      .filter(c=>c.len===len&&Math.abs(dayDiff(dep,c.dep))<=7).sort((a,b)=>a.p-b.p)[0];
    if(cand&&aPrice-cand.p>=gate&&(!alt||cand.p<alt.price))alt={kind:'origin',o:p[0],d,dep:cand.dep,len:cand.len,price:cand.p,save:aPrice-cand.p};});
  if(alt)sug.push(alt);
  sug.sort((a,b)=>b.save-a.save);
  return {route,o,d,dep,len,anchorPrice:aPrice,gate,cells,anchorOff,suggestions:sug};
}

/* ── monetization: real affiliate deep links from config (D.monetization) ───── */
const MON=D.monetization||{enabled:false,providers:[],google_flights:true};
function _googleUrl(o,d,dep,ret){return 'https://www.google.com/travel/flights?q='+encodeURIComponent('Flights from '+o+' to '+d+' on '+dep+' through '+ret);}
function bookLinks(itin){const p=parseItin(itin);if(!p)return null;
  const{o,d,dep,ret}=p;const dd=dep.split('-'),rr=ret.split('-');
  const ctx={o,d,ol:o.toLowerCase(),dl:d.toLowerCase(),dep,ret,
    depDDMM:dd[2]+dd[1],retDDMM:rr[2]+rr[1],depYMD:dd[0].slice(2)+dd[1]+dd[2],retYMD:rr[0].slice(2)+rr[1]+rr[2],
    adults:MON.adults||1,sub:MON.sub||'',cur:MON.cur||'nzd'};
  const fill=(tpl,mk)=>tpl.replace(/\{(\w+)\}/g,(_,k)=>k==='m'?encodeURIComponent(mk||''):(ctx[k]!=null?encodeURIComponent(ctx[k]):''));
  const provs=(MON.providers||[]).map(x=>({id:x.id,name:x.name,primary:!!x.primary,href:fill(x.url,x.m)}));
  const google=_googleUrl(o,d,dep,ret);
  let primary=provs.find(x=>x.primary)||provs[0]||{id:'google',name:'Google Flights',href:google};
  const compare=provs.filter(x=>x!==primary);
  if(MON.google_flights!==false)compare.push({id:'google',name:'Google Flights',href:google});
  return {o,d,dep,ret,primary,compare,google};}
const _EXT='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 7h9v9M17 7 7 17"/></svg>';
function bookRowLink(itin,label){const L=bookLinks(itin);if(!L)return'';
  return '<a class="bookrow" href="'+L.primary.href+'" target="_blank" rel="noopener sponsored nofollow">'+_EXT+esc(label||'Book')+'</a>';}

/* ══════════ FaroStore — Firebase data layer (demo localStorage / real cloud) ══ */
const CFG=window.FARO_FIREBASE||null;
const Store={
  cloud:!!CFG, user:null, _trips:[], _subs:[], _scan:0,
  async init(){
    if(this.cloud){try{await this._initCloud();}catch(e){console.warn('Firebase init failed, demo mode',e);this.cloud=false;this._initDemo();}}
    else this._initDemo();
  },
  _initDemo(){
    this.user=JSON.parse(localStorage.getItem('faro.user')||'null')||{uid:'anon-'+Math.random().toString(36).slice(2,8),anon:true};
    localStorage.setItem('faro.user',JSON.stringify(this.user));
    this._trips=JSON.parse(localStorage.getItem(this._key())||'[]');
  },
  _key(){return 'faro.trips.'+(this.user&&(this.user.email||this.user.uid)||'anon');},
  _persist(){if(!this.cloud)localStorage.setItem(this._key(),JSON.stringify(this._trips));this._emit();},
  onChange(cb){this._subs.push(cb);cb(this._trips,this.user);},
  _emit(){setTimeout(()=>this._subs.forEach(cb=>cb(this._trips,this.user)),0);},
  trips(){return this._trips;}, scanOffset(){return this._scan*-42;},
  tripId(t){return t.o+'-'+t.d+'-'+t.dep+'-'+t.len;},
  isWatched(t){const id=this.tripId(t);return this._trips.some(x=>x.id===id);},
  _tripDoc(t){return {o:t.o,d:t.d,dep:t.dep,ret:t.ret||'',len:t.len,
    alerts:{push:true,telegram:false,email:false},priceTarget:null,lastNotifiedSignal:null,createdAt:Date.now()};},
  async pin(t){const id=this.tripId(t);const exists=this._trips.some(x=>x.id===id);
    if(this.cloud){const ref=this._F.doc(this._db,'users/'+this.user.uid+'/trips/'+id);
      if(exists)await this._F.deleteDoc(ref);else await this._F.setDoc(ref,this._tripDoc(t));return;}
    if(exists)this._trips=this._trips.filter(x=>x.id!==id);
    else this._trips.push(Object.assign({id},this._tripDoc(t)));
    this._persist();},
  async remove(id){if(this.cloud){await this._F.deleteDoc(this._F.doc(this._db,'users/'+this.user.uid+'/trips/'+id));return;}
    this._trips=this._trips.filter(x=>x.id!==id);this._persist();},
  async setAlert(id,ch,on){if(this.cloud){await this._F.updateDoc(this._F.doc(this._db,'users/'+this.user.uid+'/trips/'+id),{['alerts.'+ch]:on});
      if(ch==='push'&&on)this._ensurePush();return;}
    const t=this._trips.find(x=>x.id===id);if(t){t.alerts[ch]=on;this._persist();}},
  async setTarget(id,v){if(this.cloud){await this._F.updateDoc(this._F.doc(this._db,'users/'+this.user.uid+'/trips/'+id),{priceTarget:v||null});return;}
    const t=this._trips.find(x=>x.id===id);if(t){t.priceTarget=v||null;this._persist();}},
  async signIn(){if(this.cloud)return this._cloudSignIn();
    const prev=this._trips;this.user={uid:'u-demo',email:'you@gmail.com',name:'You',anon:false};
    localStorage.setItem('faro.user',JSON.stringify(this.user));
    const existing=JSON.parse(localStorage.getItem(this._key())||'[]');const ids=new Set(existing.map(t=>t.id));
    this._trips=existing.concat(prev.filter(t=>!ids.has(t.id)));this._persist();this._emit();},
  async signOut(){if(this.cloud)return this._cloudSignOut();
    this.user={uid:'anon-'+Math.random().toString(36).slice(2,8),anon:true};
    localStorage.setItem('faro.user',JSON.stringify(this.user));
    this._trips=JSON.parse(localStorage.getItem(this._key())||'[]');this._emit();},
  simulateScan(){this._scan++;this._emit();},
  /* — real cloud backend — */
  async _initCloud(){
    const A=await import('https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js');
    const Au=await import('https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js');
    const F=await import('https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js');
    this._app=A.initializeApp(CFG);this._auth=Au.getAuth(this._app);this._db=F.getFirestore(this._app);this._F=F;this._Au=Au;
    await new Promise(res=>{Au.onAuthStateChanged(this._auth,async u=>{
      if(!u){try{await Au.signInAnonymously(this._auth);}catch(e){console.warn(e);}return;}
      this.user={uid:u.uid,email:u.email,name:u.displayName,anon:u.isAnonymous};
      F.onSnapshot(F.collection(this._db,'users/'+u.uid+'/trips'),snap=>{
        this._trips=snap.docs.map(dd=>Object.assign({id:dd.id},dd.data(),
          {alerts:dd.data().alerts||{push:false,telegram:false,email:false},
           target:dd.data().priceTarget==null?null:dd.data().priceTarget,
           len:dd.data().len!=null?dd.data().len:lenOf(dd.data().dep,dd.data().ret)}));
        this._emit();});
      res();});});
  },
  async _cloudSignIn(){const p=new this._Au.GoogleAuthProvider();
    try{await this._Au.linkWithPopup(this._auth.currentUser,p);}catch(e){await this._Au.signInWithPopup(this._auth,p);}},
  async _cloudSignOut(){await this._Au.signOut(this._auth);},
  async _ensurePush(){try{
    if(!('serviceWorker'in navigator)||!window.FARO_FCM_VAPID)return;
    const M=await import('https://www.gstatic.com/firebasejs/10.12.0/firebase-messaging.js');
    const reg=await navigator.serviceWorker.register('firebase-messaging-sw.js');
    const perm=await Notification.requestPermission();if(perm!=='granted')return;
    const msg=M.getMessaging(this._app);
    const tok=await M.getToken(msg,{vapidKey:window.FARO_FCM_VAPID,serviceWorkerRegistration:reg});
    if(tok)await this._F.setDoc(this._F.doc(this._db,'users/'+this.user.uid),
      {fcmTokens:this._F.arrayUnion(tok)},{merge:true});
  }catch(e){console.warn('FCM registration failed',e);}},
};

/* ══════════ Answer view ══════════ */
const qs=new URLSearchParams(location.search);
// Default to the strongest current call (recs are pre-sorted BUY > WATCH > WAIT,
// then confidence) so the first Answer shows the full experience, not a thin route.
const _firstRec=(D.recs||[]).map(r=>parseItin(r.itinerary)).find(p=>p&&RECS_BY_ROUTE[p.o+'-'+p.d]);
const defRoute=_firstRec?[_firstRec.o,_firstRec.d]:(ROUTE_KEYS[0]||'CHC-CMB').split('-');
const A={o:qs.get('o')||defRoute[0],d:qs.get('d')||defRoute[1],
  depMonth:(qs.get('dep')||(_firstRec?_firstRec.dep:(MONTHS[0]?MONTHS[0][0]+'-04':'2026-09'))).slice(0,7),
  len:+(qs.get('len')||(_firstRec?lenOf(_firstRec.dep,_firstRec.ret):(LENGTHS[0]?LENGTHS[0][0]:21))),flex:qs.get('flex')!=='0'};
if(!RECS_BY_ROUTE[A.o+'-'+A.d]){[A.o,A.d]=defRoute;}
const depDate=()=>{const r=nearestRec(A.o,A.d,A.depMonth,A.len);return r?r.dep:A.depMonth+'-04';};
const retDate=()=>{const r=nearestRec(A.o,A.d,A.depMonth,A.len);return r?r.ret:A.depMonth+'-25';};
function syncURL(){const p=new URLSearchParams({o:A.o,d:A.d,dep:depDate(),len:A.len,flex:A.flex?1:0});
  history.replaceState(null,'',location.pathname+'?'+p+location.hash);}

const opt=(v,t,sel)=>'<option value="'+esc(v)+'"'+(sel?' selected':'')+'>'+esc(t)+'</option>';
function fillComposer(){
  $('f-o').innerHTML=origins.map(o=>opt(o,o+' · '+cityName(o),o===A.o)).join('');
  $('f-d').innerHTML=dests.map(d=>opt(d,d+' · '+cityName(d),d===A.d)).join('');
  $('f-dep').innerHTML=MONTHS.map(m=>opt(m[0],m[1],m[0]===A.depMonth)).join('');
  $('f-len').innerHTML=LENGTHS.map(l=>opt(l[0],l[1],l[0]===A.len)).join('');
  $('f-flex').checked=A.flex;}

function renderAnswer(){
  const v=verdictFor(A.o,A.d,A.depMonth,A.len);
  if(!v){$('verdict').innerHTML='<div class="empty">No model call yet for '+esc(A.o)+'→'+esc(A.d)+' — still collecting fares.</div>';
    $('weather').innerHTML='';$('story').innerHTML='';$('trust').textContent='';return;}
  const oi={falling:['▼','falling','var(--down)'],low:['◆','low & firming','var(--brand)'],steady:['◆','steady','var(--muted)']}[v.out];
  $('outlook').innerHTML='<span style="color:'+oi[2]+'">'+oi[0]+'</span> outlook: <b>'+oi[1]+'</b>';
  let win=v.sig==='BUY'?'<span class="window">⟳ best-buy window: <b>now</b></span>'
    :(v.win!=null?'<span class="window">⟳ best-buy window: <b>~'+v.win+' days</b></span>':'');
  const pips=Math.round((v.conf||0)/20);
  const prov=v.hit!=null
    ?'based on <b>'+v.obs+' observations</b> and a backtest that called this route right <b>'+v.hit+'% of the time</b>'
    :'based on <b>'+v.obs+' observations</b> — still learning this route';
  const watching=Store.isWatched({o:A.o,d:A.d,dep:v.dep,len:v.len});
  const liveTag=Store._scan>0?'<span class="live"><span class="dot"></span>live · scan #'+(Store._scan+1)+'</span>':'';
  const L=bookLinks(v.itin);
  const compare=L&&L.compare.length?'<span class="altbook">or compare on '+L.compare.map(p=>'<a href="'+p.href+'" target="_blank" rel="noopener sponsored nofollow">'+esc(p.name)+'</a>').join(' · ')+'</span>':'';
  $('verdict').innerHTML=
   '<div class="verdict s-'+v.sig+'">'
   +'<div class="ctx mono">'+esc(A.o)+' → '+esc(A.d)+' · <b>'+fmtD(v.dep)+' – '+fmtD(v.ret)+'</b> · '+v.len+' nights'+(A.flex?' · ±3d':'')+' '+liveTag+'</div>'
   +'<div class="sigrow"><div class="signal">'+v.sig+'</div>'+win+'</div>'
   +'<div class="reason">'+esc(v.reason)+'</div>'
   +'<div class="conf"><span>Confidence</span><span class="track">'+[0,1,2,3,4].map(i=>'<span class="pip '+(i<pips?'on':'')+'"></span>').join('')+'</span>'
   +'<span>'+(v.conf||0)+'% — '+prov+'.</span></div>'
   +'<div class="nums">'
   +'<div class="num"><div class="v mono">'+money(v.now)+'</div><div class="k">fare now</div></div><div class="arrow">→</div>'
   +'<div class="num"><div class="v mono">'+money(v.low)+'</div><div class="k">forecast low</div></div>'
   +'<div class="num"><div class="v move '+(v.movePct<0?'dn':v.movePct>0?'up':'')+' mono">'+(v.movePct>0?'+':'')+v.movePct+'%</div><div class="k">expected move</div></div>'
   +'</div>'
   +'<div class="cta">'
   +'<button class="btn btn-pin '+(watching?'on':'')+'" id="pinBtn">'+(watching?'✓ Watching':'+ Pin &amp; watch this trip')+'</button>'
   +(L?'<a class="btn btn-primary" href="'+L.primary.href+'" target="_blank" rel="noopener sponsored nofollow">Book on '+esc(L.primary.name)+' →</a>':'')
   +'<button class="auditlink" id="auditBtn">why we think this ▾</button>'
   +'</div>'+compare
   +'<div class="audit" id="audit"><div>Faro’s reasoning, in full — generated from the numbers, no LLM:</div>'
   +'<ul>'+v.why.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul></div>'
   +'</div>';
  $('pinBtn').onclick=async()=>{await Store.pin({o:A.o,d:A.d,dep:v.dep,ret:v.ret,len:v.len});
    setTimeout(()=>{const w=Store.isWatched({o:A.o,d:A.d,dep:v.dep,len:v.len});
      toast(w?(Store.user.anon?'Watching ✓ — saved to this device. Sign in to sync + get alerts':'Watching ✓ — synced · we’ll alert you when it’s time'):'Removed from My trips');renderAnswer();},30);};
  const ab=$('auditBtn');ab.onclick=()=>{const a=$('audit');a.classList.toggle('open');
    ab.textContent=a.classList.contains('open')?'why we think this ▴':'why we think this ▾';};
  renderOptions(v);renderFlex(v);renderWeather(v);renderStory(v);
  const st=D.status||{};const last=st.last_scan_iso?relTime(st.last_scan_iso):'recently';
  $('trust').innerHTML='Source: <b>Google Flights</b>, scraped several times a day · last scan <b>'+esc(last)+'</b> · '+v.obs+' observations on this route.'
    +'<br>Fares are <b>informational</b> — always confirm the live price before booking.';
}
function relTime(iso){const t=Date.parse(String(iso).replace(' ','T').replace(/Z?$/,'Z'));if(isNaN(t))return'recently';
  const m=Math.round((Date.now()-t)/60000);if(m<60)return m+'m ago';const h=Math.round(m/60);if(h<48)return h+'h ago';return Math.round(h/24)+'d ago';}

function fmtDur(m){m=+m||0;if(!m)return '';const h=Math.floor(m/60),mm=m%60;return h+'h'+(mm?' '+mm+'m':'');}
/* The specific flights on this exact trip from the latest scan. We never collapse
   a carrier to one line or invent a flight number -- each row is a real distinct
   offer (a carrier can legitimately appear several times with different routing or
   timing), described with exactly what the source gives us. */
function renderOptions(v){const offers=(D.latest_offers||{})[v.itin]||[];
  if(!offers.length){$('options').innerHTML='';$('options').style.display='none';return;}
  $('options').style.display='';
  const seen=new Set(),rows=[];
  offers.forEach(o=>{const k=o.iata+'|'+o.stops+'|'+o.via+'|'+o.duration+'|'+Math.round(o.price);
    if(seen.has(k))return;seen.add(k);rows.push(o);});
  const cheapest=Math.min.apply(null,rows.map(o=>o.price));let taggedCheapest=false;
  const head='<div class="panel-head"><h3>Flights on this trip</h3><span>'+rows.length+' distinct option'+(rows.length>1?'s':'')+' · '+esc(A.o)+'→'+esc(A.d)+' '+fmtD(v.dep)+'</span></div>';
  const body=rows.slice(0,8).map(o=>{
    const stops=o.stops===0?'non-stop':(o.stops+' stop'+(o.stops>1?'s':''));
    const via=o.via?' via '+esc(o.via):'';
    const logo=o.iata?'<img class="opt-logo" loading="lazy" alt="" src="https://pics.avs.io/al_square/44/44/'+esc(o.iata)+'.png">':'<span class="opt-logo"></span>';
    const best=(o.price===cheapest&&!taggedCheapest)?(taggedCheapest=true,'<span class="opt-best">CHEAPEST</span>'):'';
    return '<div class="optrow">'+logo
      +'<span class="opt-main"><b>'+esc(o.airline||'Airline')+'</b><span class="opt-meta"> · '+stops+via+(o.duration?' · '+fmtDur(o.duration):'')+'</span></span>'
      +'<span class="opt-pr">'+money(o.price)+best+'</span></div>';
  }).join('');
  const note='<div class="opt-note">Each row is a <b>distinct flight</b> tracked on this exact trip in the latest scan — the same airline can appear more than once (different routing or timing). We capture the <b>operating carrier(s), stops, connection airport and total duration</b>; <b>exact flight numbers and departure times are confirmed on the booking page</b> — we don’t invent them.</div>';
  $('options').innerHTML=head+body+note;}

function reanchor(o,d,dep,len){[A.o,A.d]=[o,d];A.depMonth=dep.slice(0,7);A.len=len;
  fillComposer();syncURL();renderAnswer();}
function renderFlex(v){const F=flexFor(v.o,v.d,v.dep,v.len,v.now);
  const head='<div class="panel-head"><h3>Save by being flexible</h3><span>cheapest nearby trips, vs your '+money((F&&F.anchorPrice)||v.now)+'</span></div>';
  if(!F){$('flex').innerHTML=head+'<p class="flex-none">Flexibility options appear once this route has more of the date grid scraped.</p>';return;}
  let cards='';
  if(F.suggestions.length){cards='<div class="flex-cards">'+F.suggestions.slice(0,3).map(s=>{
    const itin=s.o+'-'+s.d+' '+s.dep+' -> '+isoAdd(s.dep,s.len);const L=bookLinks(itin);
    let what,sub;
    if(s.kind==='date'){what=s.days+' day'+(s.days>1?'s':'')+' '+s.dir;sub='leave '+fmtD(s.dep)+' · '+s.len+' nights';}
    else if(s.kind==='len'){what=(s.delta>0?'+'+s.delta:s.delta)+' nights ('+s.len+'n)';sub='around '+fmtD(s.dep);}
    else{what='fly from '+esc(s.o);sub=cityName(s.o)+' → '+cityName(s.d)+' · '+fmtD(s.dep)+' · '+s.len+'n';}
    return '<div class="flexcard" data-o="'+s.o+'" data-d="'+s.d+'" data-dep="'+s.dep+'" data-len="'+s.len+'">'
      +'<span class="fx-save">−'+money(s.save)+'</span>'
      +'<span class="fx-what">'+esc(what)+'</span><span class="fx-sub">'+esc(sub)+' · '+money(s.price)+'</span>'
      +(L?'<a class="fx-book" href="'+L.primary.href+'" target="_blank" rel="noopener sponsored nofollow" onclick="event.stopPropagation()">'+_EXT+'book this</a>':'')
      +'</div>';}).join('')+'</div>';
  }else{cards='<p class="flex-none">Your dates already look like the cheapest in this window — flexing nearby wouldn’t beat '+money(F.anchorPrice)+' by a meaningful margin.</p>';}
  // mini surface bar: same length, departure dates within ±10 days, coloured by price
  const win=F.cells.filter(c=>c.len===v.len&&Math.abs(c.off-F.anchorOff)<=10).sort((a,b)=>a.off-b.off);
  let bar='';
  if(win.length>=3){const ps=win.map(c=>c.p),mn=Math.min(...ps),mx=Math.max(...ps),rg=(mx-mn)||1,cheap=mn;
    bar='<div class="flexbar-wrap"><div class="flexbar-cap">price by departure date (same '+v.len+'-night trip)</div><div class="flexbar">'
      +win.map(c=>{const h=18+(1-(c.p-mn)/rg)*46;const cls=c.dep===v.dep?'anchor':(c.p===cheap?'cheap':'');
        return '<div class="fb-col '+cls+'" data-o="'+v.o+'" data-d="'+v.d+'" data-dep="'+c.dep+'" data-len="'+v.len+'" title="'+fmtD(c.dep)+' — '+money(c.p)+'">'
          +'<div class="fb-bar" style="height:'+h.toFixed(0)+'px"></div><div class="fb-d">'+(+c.dep.slice(8))+'</div></div>';}).join('')
      +'</div><div class="flexbar-legend"><span><i style="background:var(--ink)"></i>your date</span><span><i style="background:var(--buy)"></i>cheapest</span><span>click a bar to explore it</span></div></div>';}
  $('flex').innerHTML=head+cards+bar;
  $('flex').querySelectorAll('[data-dep]').forEach(el=>el.onclick=()=>{
    reanchor(el.dataset.o,el.dataset.d,el.dataset.dep,+el.dataset.len);
    toast('Now showing '+el.dataset.o+'→'+el.dataset.d+' · '+fmtD(el.dataset.dep)+' · '+el.dataset.len+'n');});
}

function renderWeather(v){
  // Real near-term forecast from the model's conformal curve (next ~7 days).
  const cur=(v.curve||[]).filter(c=>v.dtd==null||c.dtd<=v.dtd).sort((a,b)=>b.dtd-a.dtd).slice(0,7);
  if(cur.length<2){$('weather').innerHTML='<div class="panel-head"><h3>7-day fare forecast</h3><span>still learning</span></div>'
    +'<p style="font-size:12.5px;color:var(--muted)">Not enough history on this route yet for a near-term forecast — the fare weather appears once the booking curve fills in.</p>';return;}
  const prices=cur.map(c=>c.p);const mn=Math.min(...prices),best=prices.indexOf(mn);
  let h='<div class="panel-head"><h3>7-day fare forecast</h3><span>model curve, '+CUR+'</span></div><div class="wx-cells">';
  cur.forEach((c,i)=>{const prev=i?cur[i-1].p:c.p;const press=(c.p-prev)/Math.max(prev,1);
    const col=press<-0.005?'var(--down)':press>0.005?'var(--up)':'var(--watch)';
    h+='<div class="wx '+(i===best?'best':'')+'"><div class="d">'+(i===0?'now':'+'+i+'d')+'</div><div class="p">'+Math.round(c.p).toLocaleString('en-NZ')+'</div>'
      +'<div class="bar" style="background:'+col+';opacity:'+(.4+Math.min(.5,Math.abs(press)*8))+'"></div>'+(i===best?'<div class="tag">BEST BUY</div>':'')+'</div>';});
  $('weather').innerHTML=h+'</div>';}

function renderStory(v){let data=v.story;
  if(!data||data.length<2){$('story').innerHTML='<div class="panel-head"><h3>Fare story</h3><span>collecting history</span></div>'
    +'<p style="font-size:12.5px;color:var(--muted)">The fare story for this exact trip appears as daily scans accumulate.</p>';return;}
  const w=700,hh=90,pad=6,mn=Math.min(...data),mx=Math.max(...data),rng=(mx-mn)||1;
  const pts=data.map((x,i)=>[pad+i/(data.length-1)*(w-2*pad),hh-pad-(x-mn)/rng*(hh-2*pad)]);
  const line=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
  const area='M'+pts[0][0]+' '+hh+' '+pts.map(p=>'L'+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ')+'L'+pts[pts.length-1][0]+' '+hh+' Z';
  $('story').innerHTML='<div class="panel-head"><h3>Fare story</h3><span>this trip · low '+money(mn)+'</span></div>'
    +'<svg class="spark" viewBox="0 0 '+w+' '+hh+'" preserveAspectRatio="none" role="img" aria-label="Fare history, low '+money(mn)+'">'
    +'<defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--brand)" stop-opacity=".28"/><stop offset="1" stop-color="var(--brand)" stop-opacity="0"/></linearGradient></defs>'
    +'<path d="'+area+'" fill="url(#g)"/><path d="'+line+'" fill="none" stroke="var(--brand)" stroke-width="2" stroke-linejoin="round"/>'
    +'<circle cx="'+pts[pts.length-1][0].toFixed(1)+'" cy="'+pts[pts.length-1][1].toFixed(1)+'" r="3.5" fill="var(--brand)"/></svg>';}

/* ══════════ identity + Watch ══════════ */
function renderIdentity(){const u=Store.user;if(!u)return;const cloud=!u.anon;
  $('idstrip').innerHTML=cloud
    ?'<span class="iddot cloud"></span><span>Synced as <b>'+esc(u.email||u.name||'you')+'</b> · trips &amp; alerts follow you across devices</span>'
      +'<button class="btn-sm" id="authBtn">Sign out</button>'
    :'<span class="iddot anon"></span><span>Trips saved on <b>this device</b> only. Sign in to sync everywhere &amp; get alerts.</span>'
      +'<button class="btn-sm cloud" id="authBtn"><span class="gicon"></span> Sign in with Google</button>';
  $('authBtn').onclick=async()=>{cloud?await Store.signOut():await Store.signIn();
    toast(cloud?'Signed out — back to device-only':'Signed in '+(Store.cloud?'':'(demo) ')+'— your trips now sync');};}

function renderWatch(){const list=Store.trips();$('tabN').textContent=list.length;
  if(!list.length){$('tripList').innerHTML='<div class="empty">No pinned trips yet.<br>Open the <b>Answer</b> tab, find your trip, and hit <b>Pin &amp; watch</b>.<br>We’ll keep an eye on it and tell you the moment it’s time to buy.</div>';return;}
  $('tripList').innerHTML=list.map(t=>{const v=verdictFor(t.o,t.d,(t.dep||'').slice(0,7),t.len)||{sig:'WATCH',now:0,conf:''};
    const al=t.alerts||{};
    const sub=fmtD(t.dep)+' · '+t.len+' nights · '+(v.sig==='BUY'?'book now':v.win?'best-buy in ~'+v.win+'d':'watching');
    const tg=(ch,lbl)=>'<button class="atog '+(al[ch]?'on':'')+'" data-id="'+esc(t.id)+'" data-ch="'+ch+'">'+(al[ch]?'✓ ':'')+lbl+'</button>';
    return '<div class="tcard"><div class="tc-top"><span class="tc-route">'+esc(t.o)+' → '+esc(t.d)+'</span><span class="chip '+v.sig+'">'+v.sig+'</span>'
      +'<span class="tc-now">'+money(v.now)+'</span></div>'
      +'<div class="tc-sub">'+sub+(v.conf?' · '+v.conf+'% confident':'')+'</div>'
      +'<div class="tc-alerts"><span style="font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em">alert via</span>'
      +tg('push','Push')+tg('telegram','Telegram')+tg('email','Email')
      +'<span class="target">· also if under <input type="number" data-id="'+esc(t.id)+'" class="tgt" placeholder="—" value="'+(t.target!=null?t.target:'')+'"> '+CUR+'</span>'
      +'<span class="tc-actions"><button class="txt-link" data-rm="'+esc(t.id)+'">remove</button></span></div></div>';}).join('');
  $('tripList').querySelectorAll('.atog').forEach(b=>b.onclick=async()=>{
    const t=Store.trips().find(x=>x.id===b.dataset.id);const cur=t&&t.alerts?t.alerts[b.dataset.ch]:false;
    await Store.setAlert(b.dataset.id,b.dataset.ch,!cur);toast((!cur?'Alert on · ':'Alert off · ')+b.dataset.ch);});
  $('tripList').querySelectorAll('.tgt').forEach(inp=>inp.onchange=async()=>{await Store.setTarget(inp.dataset.id,+inp.value||null);
    toast(inp.value?'Price target set: under '+CUR+' '+inp.value:'Price target cleared');});
  $('tripList').querySelectorAll('[data-rm]').forEach(b=>b.onclick=async()=>{await Store.remove(b.dataset.rm);toast('Removed from My trips');});}

/* ══════════ Lab: route-first deep-dive ══════════ */
let FINDER=null,FINDER_LOADING=false;
function loadFinder(cb){if(FINDER){cb();return;}if(FINDER_LOADING){return;}FINDER_LOADING=true;
  fetch('explore.json').then(r=>r.ok?r.json():[]).then(j=>{FINDER=j||[];FINDER_LOADING=false;cb();})
    .catch(()=>{FINDER=[];FINDER_LOADING=false;cb();});}
const labRouteFromHash=()=>{const m=location.hash.match(/^#\/lab\/([A-Z]{3}-[A-Z]{3})/);return m&&RECS_BY_ROUTE[m[1]]?m[1]:null;};
function renderLab(){const r=labRouteFromHash();if(r)renderLabRoute(r);else renderLabGate();}

function renderLabGate(){
  if(!ROUTE_KEYS.length){$('labBody').innerHTML='<div class="watch-head"><h2>The Lab</h2></div><div class="empty">No routes with enough history yet — the Lab opens up once the first booking curves fill in.</div>';return;}
  const cards=ROUTE_KEYS.map(k=>{const[o,d]=k.split('-');const v=verdictFor(o,d,A.depMonth,21);const b=BT[k]||{};
    const thin=b.hit_rate==null;
    return '<button class="gate-card" data-lab="'+k+'"><div class="gate-top"><span class="gate-route">'+o+' → '+d+'</span><span class="minichip '+(v?v.sig:'WATCH')+'">'+(v?v.sig:'WATCH')+'</span></div>'
      +'<div class="gate-acc"><span><b>'+(v?money(v.now):'—')+'</b>cheapest call</span>'
      +'<span><b>'+(thin?'—':b.hit_rate+'%')+'</b>'+(thin?'still learning':'accurate')+'</span>'
      +'<span><b style="color:var(--buy)">'+(b.saved_vs_searchday!=null?money(b.saved_vs_searchday):'—')+'</b>saved so far</span></div>'
      +'<div class="gate-cta">Dig in →</div></button>';}).join('');
  $('labBody').innerHTML='<div class="watch-head"><h2>The Lab</h2></div>'
    +'<p class="lab-intro">Pick a route to dig in. Everything below is scoped to <b>just that corridor</b> — how accurate Faro’s calls have been, how much they saved, the forecast, the cheapest days, and the raw grid.</p>'
    +'<div class="gate-grid">'+cards+'</div>';
  $('labBody').querySelectorAll('[data-lab]').forEach(b=>b.onclick=()=>{location.hash='#/lab/'+b.dataset.lab;});}

function renderLabRoute(route){const[o,d]=route.split('-');
  $('labBody').innerHTML='<div class="changebar"><button class="focusbtn" id="labBack">← all routes</button><h2>'+o+' → '+d+'</h2>'
    +'<button class="focusbtn" id="labOpen" style="margin-left:auto">open in Answer →</button></div>'
    +'<div class="labsec" id="scoreSec"></div>'
    +'<div class="labsec"><h3>Price surface <span>cheapest fare by departure date × trip length — flexibility at a glance</span></h3><div id="surfPanel"></div></div>'
    +'<div class="grid2"><div class="labsec"><h3>Forecast fan <span>conformal band</span></h3><div id="fanPanel"></div></div>'
    +'<div class="labsec"><h3>Cheapest day to fly <span>departure × return weekday</span></h3><div id="heatPanel"></div></div></div>'
    +'<div class="labsec"><h3>Find a trip on '+o+' → '+d+' <span id="finderCount"></span></h3>'
    +'<div class="finder-bar"><div class="ff"><label for="ff-stops">max stops</label><select id="ff-stops"><option value="2">any</option><option value="1">≤ 1 stop</option><option value="0">non-stop</option></select></div>'
    +'<div class="ff"><label for="ff-price">max price <span id="ff-pv" class="mono"></span></label><input type="range" id="ff-price"></div>'
    +'<div class="ff"><label for="ff-sort">sort</label><select id="ff-sort"><option value="price">cheapest</option><option value="dep">soonest</option></select></div></div>'
    +'<div id="finderRes"><p style="font-size:12.5px;color:var(--muted);padding:12px 0">Loading the route’s slice of the full grid…</p></div></div>';
  $('labBack').onclick=()=>{location.hash='#/lab';};
  $('labOpen').onclick=()=>{[A.o,A.d]=[o,d];fillComposer();syncURL();location.hash='#/';toast('Opened '+o+'→'+d+' in the Answer');};
  renderScorecard(route);renderSurface(route);renderFan(route);loadFinder(()=>{renderHeat(route);initFinderScoped(route);});}

/* the price surface: a real dep-date × trip-length heatmap straight from
   D.surface — the visual heart of the flexibility story (cheaper = greener). */
function renderSurface(route){const cells=surfaceCells(route);
  if(!cells||cells.length<6){$('surfPanel').innerHTML='<p style="font-size:12.5px;color:var(--dim)">The price surface fills in as more of this route’s date grid is scraped.</p>';return;}
  const lens=[...new Set(cells.map(c=>c.len))].sort((a,b)=>a-b);
  const offs=[...new Set(cells.map(c=>c.off))].sort((a,b)=>a-b);
  const base=SURFACE[route].base;
  const map={};cells.forEach(c=>{map[c.off+'_'+c.len]=c.p;});
  const ps=cells.map(c=>c.p),mn=Math.min(...ps),mx=Math.max(...ps);
  let best=cells[0];cells.forEach(c=>{if(c.p<best.p)best=c;});
  const col=p=>{if(p==null)return null;const t=(p-mn)/((mx-mn)||1);
    return t<0.5?'color-mix(in srgb,var(--buy) '+((1-t*2)*100)+'%, var(--brand))':'color-mix(in srgb,var(--brand) '+((1-(t-0.5)*2)*100)+'%, var(--up))';};
  // header row (departure dates) + one row per trip length
  let grid='<div class="surf" style="grid-template-columns:auto repeat('+offs.length+',11px)">';
  grid+='<div class="surf-rowlabel"></div>'+offs.map(o=>{const iso=isoAdd(base,o);const dd=+iso.slice(8);
    return '<div class="surf-collabel" style="writing-mode:vertical-rl;height:34px;text-align:right">'+(dd===1||o===offs[0]?fmtD(iso):dd)+'</div>';}).join('');
  lens.forEach(L=>{grid+='<div class="surf-rowlabel">'+L+'n</div>'+offs.map(o=>{const p=map[o+'_'+L];
    if(p==null)return '<div class="surf-cell empty"></div>';const isBest=(o===best.off&&L===best.len);
    return '<div class="surf-cell'+(isBest?' best':'')+'" style="background:'+col(p)+'" title="'+fmtD(isoAdd(base,o))+' · '+L+'n — '+money(p)+'" data-o="'+route.split('-')[0]+'" data-d="'+route.split('-')[1]+'" data-dep="'+isoAdd(base,o)+'" data-len="'+L+'"></div>';}).join('');});
  grid+='</div>';
  $('surfPanel').innerHTML='<div class="surf-scroll">'+grid+'</div>'
    +'<div class="surf-legend">cheaper <span class="surf-ramp"></span> dearer · ★ cheapest: <b style="color:var(--brand)">'+money(best.p)+'</b> on '+fmtD(isoAdd(base,best.off))+' for '+best.len+' nights · rows = trip length, columns = departure date</div>';
  $('surfPanel').querySelectorAll('.surf-cell[data-dep]').forEach(el=>el.onclick=()=>{
    reanchor(el.dataset.o,el.dataset.d,el.dataset.dep,+el.dataset.len);location.hash='#/';
    toast('Opened '+fmtD(el.dataset.dep)+' · '+el.dataset.len+'n in the Answer');});}

function renderScorecard(route){const[o,d]=route.split('-');const v=verdictFor(o,d,A.depMonth,21);const b=BT[route]||{};
  const thin=b.hit_rate==null;const avg=(b.itineraries&&b.saved_vs_searchday!=null)?Math.round(b.saved_vs_searchday/Math.max(1,b.itineraries)):null;
  let nowSave,nowK,nowSub;
  if(v&&v.sig==='BUY'){const hi=v.story?Math.max(...v.story):v.now;nowSave=Math.max(0,hi-v.now);nowK='saved by buying now';nowSub='you’re at the low — vs this trip’s recent high '+money(hi);}
  else if(v){nowSave=Math.max(0,v.now-v.low);nowK='on the table right now';nowSub='wait → forecast low '+money(v.low)+' ('+v.sig+')';}
  else{nowSave=0;nowK='—';nowSub='no current call';}
  const savedStr=b.saved_vs_searchday!=null?money(b.saved_vs_searchday):'—';
  $('scoreSec').innerHTML='<h3>How accurate is Faro here — and what it’s worth <span>walk-forward backtest</span></h3>'
    +'<div class="score-row" style="margin-top:12px">'
    +'<div class="score-big acc"><div class="v">'+(thin?'—':b.hit_rate+'%')+'</div><div class="k">prediction accuracy</div>'
    +'<div class="s">'+(thin?'only '+(b.calls||0)+' graded calls so far — too thin to trust':b.right+' of '+b.calls+' BUY/WAIT calls paid off')+'</div></div>'
    +'<div class="score-big saved"><div class="v">'+savedStr+'</div><div class="k">saved vs search-day</div>'
    +'<div class="s">following Faro vs booking when you first searched'+(avg!=null?' · ~'+money(avg)+'/trip':'')+'</div></div>'
    +'<div class="score-big now"><div class="v">'+money(nowSave)+'</div><div class="k">'+nowK+'</div><div class="s">'+nowSub+'</div></div></div>'
    +'<div class="bt-strip">'+stripFor(b)+'</div>'
    +'<div style="font-size:11.5px;color:var(--dim);margin-top:6px">green = the call paid off · red = it missed</div>'
    +'<div class="honest">'+(thin
      ?'We’ve made only <b>'+(b.calls||0)+' calls</b> on this route — too few to headline an accuracy figure, so we don’t. Numbers fill in as the booking curve grows.'
      :'Following every Faro BUY/WAIT call instead of booking the day you first searched would have changed your spend by <b>'+savedStr+'</b> across '+(b.itineraries||0)+' tracked trips. <b>'+(b.calls-b.right)+' calls missed</b>'+(b.missed_cost?', costing about '+money(b.missed_cost)+' — counted against the total, because honesty is the product':'')+'.')+'</div>';}
function stripFor(b){if(!b.calls)return'<span style="font-size:12px;color:var(--dim)">no graded calls yet</span>';
  const n=Math.min(b.calls,28),rightFrac=b.right/b.calls;let out='';
  for(let i=0;i<n;i++)out+='<i class="'+((i/n)<rightFrac?'r':'w')+'"></i>';return out;}

function renderFan(route){const[o,d]=route.split('-');const r=nearestRec(o,d,A.depMonth,21);
  const cv=(r&&r.curve||[]).slice().sort((a,b)=>b.dtd-a.dtd);
  if(cv.length<3){$('fanPanel').innerHTML='<p style="font-size:12.5px;color:var(--dim)">Forecast fan appears once this route has a trained model curve.</p>';return;}
  const N=Math.min(cv.length,30),slice=cv.slice(0,N);
  const w=700,h=150,pad=8;const all=slice.flatMap(c=>[c.lo,c.hi]);const mn=Math.min(...all),mx=Math.max(...all),rg=(mx-mn)||1;
  const X=i=>pad+i/(N-1)*(w-2*pad),Y=val=>h-pad-(val-mn)/rg*(h-2*pad);
  const lineP=slice.map((c,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(c.p).toFixed(1)).join(' ');
  const bandP=slice.map((c,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(c.hi).toFixed(1)).join(' ')+' '
    +slice.map((c,i)=>'L'+X(N-1-i).toFixed(1)+' '+Y(slice[N-1-i].lo).toFixed(1)).join(' ')+'Z';
  let bi=0,bp=1e9;slice.forEach((c,i)=>{if(c.p<bp){bp=c.p;bi=i;}});
  $('fanPanel').innerHTML='<svg class="fan" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" role="img" aria-label="Forecast fan with conformal band">'
    +'<path d="'+bandP+'" fill="var(--brand)" opacity=".14"/><path d="'+lineP+'" fill="none" stroke="var(--brand)" stroke-width="2"/>'
    +'<line x1="'+X(bi)+'" y1="0" x2="'+X(bi)+'" y2="'+h+'" stroke="var(--brand)" stroke-dasharray="3 3" opacity=".55"/>'
    +'<circle cx="'+X(bi)+'" cy="'+Y(slice[bi].p)+'" r="4" fill="var(--brand)"/></svg>'
    +'<div style="font-size:11.5px;color:var(--dim)">shaded = conformal band · dashed = model’s cheapest forecast point</div>';}

const DOW=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function renderHeat(route){const rows=(FINDER||[]).filter(f=>(f.o+'-'+f.d)===route);
  if(rows.length<6){$('heatPanel').innerHTML='<p style="font-size:12.5px;color:var(--dim)">Cheapest-day heatmap appears once more dates are scraped for this route.</p>';return;}
  const g=Array.from({length:7},()=>Array(7).fill(null));
  rows.forEach(f=>{const di=(new Date(f.dep).getDay()+6)%7,ri=(new Date(f.ret).getDay()+6)%7;
    if(g[di][ri]==null||f.min<g[di][ri])g[di][ri]=f.min;});
  const vals=g.flat().filter(x=>x!=null);if(!vals.length){$('heatPanel').innerHTML='';return;}
  const min=Math.min(...vals),mx=Math.max(...vals);let minij=[0,0];
  for(let i=0;i<7;i++)for(let j=0;j<7;j++)if(g[i][j]===min)minij=[i,j];
  const col=v=>{if(v==null)return'var(--card2)';const t=(v-min)/((mx-min)||1);
    return t<0.5?'color-mix(in srgb,var(--buy) '+((1-t*2)*100)+'%, var(--brand))':'color-mix(in srgb,var(--brand) '+((1-(t-0.5)*2)*100)+'%, var(--up))';};
  let html='<div class="heat"><div class="hlabel"></div>'+DOW.map(d=>'<div class="hlabel">'+d+'</div>').join('');
  for(let i=0;i<7;i++){html+='<div class="hlabel">'+DOW[i]+'</div>';for(let j=0;j<7;j++){const best=i===minij[0]&&j===minij[1];const val=g[i][j];
    html+='<div class="hcell '+(best?'best':'')+'" style="background:'+col(val)+'" title="'+DOW[i]+' dep / '+DOW[j]+' ret'+(val!=null?' — '+money(val):'')+'">'+(best?'★':'')+'</div>';}}
  html+='</div><div class="heat-legend">cheaper <span class="heat-ramp"></span> dearer · ★ '+DOW[minij[0]]+' out / '+DOW[minij[1]]+' back is cheapest ('+money(min)+')</div>';
  $('heatPanel').innerHTML=html;}

function initFinderScoped(route){const rows=(FINDER||[]).filter(f=>(f.o+'-'+f.d)===route);
  if(!rows.length){$('finderRes').innerHTML='<p style="font-size:12.5px;color:var(--muted);padding:12px 0">No grid rows for this route yet.</p>';return;}
  const pmin=Math.min(...rows.map(f=>f.min)),pmax=Math.max(...rows.map(f=>f.min));
  const slider=$('ff-price');slider.min=Math.floor(pmin);slider.max=Math.ceil(pmax);slider.step=10;slider.value=Math.ceil(pmax);
  const draw=()=>{const st=+$('ff-stops').value,mp=+$('ff-price').value,so=$('ff-sort').value;$('ff-pv').textContent=money(mp);
    let res=rows.filter(f=>(f.stops==null||f.stops<=st)&&f.min<=mp);
    res.sort((a,b)=>so==='price'?a.min-b.min:String(a.dep).localeCompare(String(b.dep)));
    $('finderCount').textContent=res.length+' combos';
    $('finderRes').innerHTML=res.slice(0,12).map(f=>{const itin=f.o+'-'+f.d+' '+f.dep+' -> '+f.ret;
      return '<div class="fres"><span class="minichip '+( (verdictFor(f.o,f.d,String(f.dep).slice(0,7),f.len)||{sig:'WATCH'}).sig )+'">'+( (verdictFor(f.o,f.d,String(f.dep).slice(0,7),f.len)||{sig:'WATCH'}).sig )+'</span>'
        +'<span><b>'+f.o+' → '+f.d+'</b> <span class="meta">· '+fmtD(f.dep)+' – '+fmtD(f.ret)+' · '+f.len+'n · '+(f.stops===0?'non-stop':(f.stops||1)+' stop'+((f.stops||1)>1?'s':''))+(f.airline?' · '+esc(f.airline):'')+'</span></span>'
        +'<span class="pr">'+money(f.min)+'</span>'+bookRowLink(itin,'Book')+'</div>';}).join('')
      ||'<p style="font-size:13px;color:var(--muted);padding:12px 0">No combos match — loosen the filters.</p>';};
  ['ff-stops','ff-price','ff-sort'].forEach(id=>$(id).oninput=draw);draw();}

/* ══════════ router + bindings ══════════ */
function route(){const h=location.hash;
  const view=h.startsWith('#/watch')?'watch':h.startsWith('#/lab')?'lab':'answer';
  $('view-answer').classList.toggle('hidden',view!=='answer');
  $('view-watch').classList.toggle('hidden',view!=='watch');
  $('view-lab').classList.toggle('hidden',view!=='lab');
  $('tab-answer').classList.toggle('on',view==='answer');
  $('tab-watch').classList.toggle('on',view==='watch');
  $('tab-lab').classList.toggle('on',view==='lab');
  if(view==='watch')renderWatch();else if(view==='lab')renderLab();else renderAnswer();}
document.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>{location.hash=b.dataset.go;});
addEventListener('hashchange',route);

function bind(el,key,num){el.onchange=e=>{A[key]=num?+e.target.value:e.target.value;
  if(!RECS_BY_ROUTE[A.o+'-'+A.d]){const alt=ROUTE_KEYS.find(r=>r.startsWith(A.o+'-'))||ROUTE_KEYS[0];
    if(alt){toast('Not tracking '+A.o+'→'+A.d+' yet — showing the closest route I know');[A.o,A.d]=alt.split('-');fillComposer();}}
  syncURL();renderAnswer();};}
bind($('f-o'),'o');bind($('f-d'),'d');bind($('f-dep'),'depMonth');bind($('f-len'),'len',true);
$('f-flex').onchange=e=>{A.flex=e.target.checked;syncURL();renderAnswer();};
$('copyLink').onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(location.href);toast('Link copied — share this exact verdict');};
$('simBtn').onclick=()=>{Store.simulateScan();toast(Store.cloud?'Listening for the next scan (Firestore onSnapshot)':'New scan landed — verdicts updated live');};
const tb=$('themeBtn');function applyTheme(){tb.textContent=document.documentElement.dataset.theme==='dark'?'☾':'☀';}
tb.onclick=()=>{const c=document.documentElement.dataset.theme;document.documentElement.dataset.theme=c==='dark'?'light':'dark';
  localStorage.setItem('faro.theme',document.documentElement.dataset.theme);applyTheme();};
if(localStorage.getItem('faro.theme'))document.documentElement.dataset.theme=localStorage.getItem('faro.theme');
let toastT;function toast(m){const el=$('toast');el.textContent=m;el.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),3600);}

$('fbBody').innerHTML=(Store.cloud
  ?'<b>Cloud mode is live.</b> Identity is Firebase <code>Auth</code> (anonymous, upgradable to Google); pinned trips live in Firestore <code>users/{uid}/trips</code> and sync across devices via <code>onSnapshot</code>; alert toggles + price target are fields a scheduled <code>Cloud Function</code> reads to push <code>FCM</code>/Telegram/email when your trip hits BUY, a new low, or a closing window.'
  :'This build runs in <b>demo mode</b> — trips live in <code>localStorage</code> and alerts are simulated. Set <code>window.FARO_FIREBASE</code> (a public web config) and the same code paths become real <code>Auth</code> + Firestore + a scheduled alert <code>Cloud Function</code>. See <code>redesign/FIREBASE.md</code>.');
$('footnote').innerHTML='<b style="color:var(--muted)">Faro</b> — one honest <b style="color:var(--muted)">Answer</b> (buy / wait / watch with provenance), a synced <b style="color:var(--muted)">Watch</b> (pinned trips + per-trip alert channels + price target), and a route-first <b style="color:var(--muted)">Lab</b> (accuracy, money saved, forecast, cheapest days, raw grid). Built from real scraped Google Flights fares — informational only.';
(function(){const el=$('affdisc');if(el&&MON.enabled&&MON.disclosure)el.textContent=MON.disclosure;})();

/* ══════════ boot ══════════ */
(async()=>{await Store.init();
  Store.onChange(()=>{$('tabN').textContent=Store.trips().length;renderIdentity();route();});
  fillComposer();applyTheme();syncURL();route();})();
</script>
</body></html>'''
