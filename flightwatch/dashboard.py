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

from . import DOCS_DIR, storage, predict


# --------------------------------------------------------------------------- #
# Airline name tidy-up
# --------------------------------------------------------------------------- #
# The scraper sometimes concatenates the operating carriers of a multi-leg
# itinerary ("Singapore AirlinesAir New Zealand") or captures a route code
# ("CHC-CMB") instead of a carrier. Make airline names presentable so the
# dashboard can actually tell you *who* flies the fare.
_ROUTE_CODE = re.compile(r"^[A-Z]{3}\s*[-–—]\s*[A-Z]{3}$")

# Carriers that fly (or connect on) this corridor. Used to split fares whose
# scraped label glues two operating carriers together -- "JetstarQantas" or
# "Singapore AirlinesAir New Zealand" -- without breaking single names that
# legitimately contain an internal capital (e.g. "SriLankan").
_KNOWN_AIRLINES = sorted([
    "Air New Zealand", "Singapore Airlines", "Malaysia Airlines", "Cathay Pacific",
    "China Southern", "China Eastern", "Thai Airways", "Qatar Airways", "Sri Lankan",
    "SriLankan", "Qantas", "Jetstar", "Emirates", "Scoot", "Batik Air", "AirAsia",
    "Fiji Airways", "Etihad", "Korean Air", "Vietnam Airlines", "Garuda Indonesia",
], key=len, reverse=True)


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
    toks = _tokenize_airlines(name)
    if toks:
        # de-dupe while preserving order, then join distinct carriers
        seen = []
        for t in toks:
            if t not in seen:
                seen.append(t)
        return " + ".join(seen)
    # Fallback: treat commas/slashes as separators; never split on camelCase.
    return re.sub(r"\s*[,/]\s*", " + ", name).strip()


def _airlines_in(day):
    """Distinct, cleaned airline names present in a day's offers (order: cheapest first)."""
    seen, out = set(), []
    for r in day.sort_values("price").itertuples():
        a = clean_airline(getattr(r, "airline", ""))
        if a and a not in seen:
            seen.add(a)
            out.append(a)
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
            "airline_prices": [{"name": a, "price": p}
                               for a, p in sorted(per_airline.items(), key=lambda kv: kv[1])][:6],
            "cheapest_airline": (clean_airline(cheapest["airline"]) if cheapest is not None else ""),
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
                      "stops": int(r.stops) if pd.notna(r.stops) else 0,
                      "duration": int(r.duration_minutes) if pd.notna(r.duration_minutes) else 0}
                     for r in day.itertuples()]
    return out


def _airline_market(ok):
    """Across every route's latest scan: each airline's reach and best fare."""
    latest_day = ok["scan_date"].max()
    day = ok[ok["scan_date"] == latest_day]
    agg = {}
    for r in day.itertuples():
        a = clean_airline(getattr(r, "airline", ""))
        if not a:
            continue
        cur = agg.setdefault(a, {"name": a, "offers": 0, "min": float(r.price)})
        cur["offers"] += 1
        cur["min"] = min(cur["min"], float(r.price))
    return sorted(agg.values(), key=lambda d: d["min"])[:8]


def build():
    df = storage.load_all()
    bundle = predict.train_model(df) if not df.empty else None
    recs = predict.recommendations(df, bundle=bundle)

    history, insights, latest_offers, airline_market = {}, [], {}, []
    stats = {"scans": 0, "total_offers": 0, "routes": 0, "airlines": 0,
             "avg_price": None, "fastest": None, "cheapest_ever": None}
    cities, primary = {}, None

    if not df.empty and (df["status"] == "ok").any():
        ok = _with_itin(df[df["status"] == "ok"])

        # Friendly geography: emit every code seen as a city name, and resolve the
        # corridor's main origin/destination (for the live weather/clock widgets).
        codes = sorted(set(ok["origin"]) | set(ok["destination"]))
        cities = {c: city_name(c) for c in codes}
        dest_code = ok["destination"].mode().iloc[0]
        orig_code = ok["origin"].mode().iloc[0]
        dest_info = CITY.get(dest_code, {})
        primary = {
            "origin": {"code": orig_code, "name": city_name(orig_code)},
            "dest": {"code": dest_code, "name": city_name(dest_code),
                     "lat": dest_info.get("lat"), "lon": dest_info.get("lon"),
                     "tz": dest_info.get("tz")},
        }

        # One cheapest point per day for the booking-curve sparklines + trend chart.
        daily = predict.daily_min(df)
        for itin, h in daily.groupby("itin"):
            history[itin] = [{"d": d.strftime("%Y-%m-%d"), "p": float(p)}
                             for d, p in zip(h["scan_date"], h["price"])]

        insights = _insights(ok)
        latest_offers = _latest_offers(ok)
        airline_market = _airline_market(ok)

        cheap_idx = ok["price"].astype(float).idxmin()
        cheap = ok.loc[cheap_idx]
        durs_all = ok["duration_minutes"].dropna()
        durs_all = durs_all[durs_all > 0]
        clean_air = ok["airline"].map(clean_airline)
        stats = {
            "scans": int(df["scan_date"].nunique()),
            "total_offers": int(len(ok)),
            "routes": int(ok["itin"].nunique()),
            "airlines": int(clean_air[clean_air != ""].nunique()),
            "avg_price": float(ok["price"].astype(float).mean()),
            "fastest": int(durs_all.min()) if not durs_all.empty else None,
            "cheapest_ever": {"price": float(cheap["price"]),
                              "itinerary": str(cheap["itin"]),
                              "airline": clean_airline(cheap["airline"]),
                              "date": cheap["scan_date"].strftime("%Y-%m-%d")},
        }

    payload = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "recs": recs,
        "history": history,
        "insights": insights,
        "latest_offers": latest_offers,
        "airline_market": airline_market,
        "stats": stats,
        "cities": cities,
        "primary": primary,
        "model": {"mae": round(bundle["mae"]), "n": bundle["n"]} if bundle else None,
        "currency": (df[df["status"] == "ok"]["currency"].iloc[0]
                     if not df.empty and (df["status"] == "ok").any() else "NZD"),
    }

    os.makedirs(DOCS_DIR, exist_ok=True)
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
<meta name="description" content="Faro tracks daily fares and tells you the perfect moment to book — with airlines, stops, flight times, live weather and currency.">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#eef3fc;--card:#ffffff;--ink:#0d1830;--muted:#56678a;--dim:#8a99b8;
  --line:#e6ecf7;--line2:#dbe4f3;
  --brand:#3b6ef5;--brand2:#7a5cf0;--teal:#0fb6a8;--pink:#e0567d;
  --buy:#12b07c;--buy-bg:#e6f7f0;--wait:#e8902a;--wait-bg:#fdf1e2;--watch:#6b7ba0;--watch-bg:#eef1f8;
  --shadow:0 12px 34px -16px rgba(24,46,92,.22);--shadow-lg:0 32px 70px -26px rgba(24,46,92,.34);
  --radius:22px;
}
*{margin:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);overflow-x:hidden;
  font-family:'Sora',system-ui,-apple-system,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}
.mono{font-family:'IBM Plex Mono',monospace}
.wrap{max-width:1100px;margin:0 auto;padding:0 20px 100px}
a{color:var(--brand);text-decoration:none}
img,canvas{max-width:100%}

/* ---------- aurora background ---------- */
.aurora{position:fixed;inset:0;z-index:-2;overflow:hidden;background:
  radial-gradient(1100px 620px at 78% -8%,#dfe8ff 0,transparent 58%),
  radial-gradient(820px 520px at -8% 6%,#dafaf4 0,transparent 52%),var(--bg)}
.blob{position:absolute;border-radius:50%;filter:blur(70px);opacity:.55;will-change:transform}
.b1{width:520px;height:520px;left:-120px;top:-90px;background:#9fb8ff;animation:float1 18s ease-in-out infinite}
.b2{width:460px;height:460px;right:-120px;top:40px;background:#a9efe2;animation:float2 22s ease-in-out infinite}
.b3{width:420px;height:420px;left:36%;top:520px;background:#d9c6ff;opacity:.4;animation:float1 26s ease-in-out infinite reverse}
@keyframes float1{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(40px,30px) scale(1.08)}}
@keyframes float2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-50px,40px) scale(1.12)}}

/* ---------- nav ---------- */
.nav{position:sticky;top:0;z-index:60;backdrop-filter:saturate(170%) blur(14px);
  background:rgba(238,243,252,.7);border-bottom:1px solid var(--line)}
.nav .row{max-width:1100px;margin:0 auto;padding:11px 20px;display:flex;align-items:center;gap:14px}
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
@media(max-width:680px){.nav .links{display:none}}

/* ---------- hero ---------- */
.hero{padding:66px 0 6px;display:grid;grid-template-columns:1.05fr .95fr;gap:34px;align-items:center}
@media(max-width:860px){.hero{grid-template-columns:1fr;padding-top:46px}}
.eyebrow{font-family:'IBM Plex Mono',monospace;letter-spacing:.26em;color:var(--brand);font-size:12px;font-weight:600}
h1{font-size:clamp(36px,6.2vw,62px);font-weight:800;letter-spacing:-1.8px;margin:16px 0 14px;line-height:1.0}
h1 .grad{background:linear-gradient(110deg,var(--brand),var(--brand2) 46%,var(--teal));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.lead{color:var(--muted);font-size:18px;max-width:540px}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}
.lchip{display:flex;align-items:center;gap:9px;background:#fff;border:1px solid var(--line);
  border-radius:14px;padding:9px 13px;box-shadow:var(--shadow);font-size:13px}
.lchip .ico{font-size:17px;line-height:1}
.lchip b{font-family:'IBM Plex Mono',monospace;font-weight:600}
.lchip small{color:var(--dim);font-size:11px;display:block}

/* hero deal card (3D) */
.dealwrap{perspective:1200px}
.deal{position:relative;background:linear-gradient(160deg,#ffffff,#f4f8ff);border:1px solid var(--line);
  border-radius:26px;padding:26px;box-shadow:var(--shadow-lg);transform-style:preserve-3d;
  transform:rotateX(var(--rx,0deg)) rotateY(var(--ry,0deg));transition:transform .2s ease}
.deal:after{content:"";position:absolute;inset:0;border-radius:26px;pointer-events:none;opacity:0;
  transition:opacity .3s;background:radial-gradient(380px 380px at var(--mx,50%) var(--my,0%),rgba(255,255,255,.85),transparent 60%)}
.deal.hot:after{opacity:.9}
.deal .tagrow{display:flex;align-items:center;gap:10px;transform:translateZ(40px)}
.deal .lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}
.deal .route{font-size:23px;font-weight:800;letter-spacing:-.6px;margin:14px 0 2px;transform:translateZ(55px)}
.deal .dates{color:var(--muted);font-size:13.5px;transform:translateZ(38px)}
.deal .pricebig{font-family:'IBM Plex Mono',monospace;font-size:48px;font-weight:600;letter-spacing:-2px;
  margin:16px 0 2px;transform:translateZ(70px);line-height:1}
.deal .pricebig small{font-size:14px;color:var(--dim);letter-spacing:0}
.deal .meta2{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;transform:translateZ(45px)}
.deal .fx{margin-top:14px;display:flex;gap:7px;flex-wrap:wrap;transform:translateZ(30px)}
.deal .fx span{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);
  background:#eef3fd;border-radius:8px;padding:3px 8px}

/* ---------- generic ---------- */
.sig{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:12.5px;padding:7px 13px;border-radius:13px;
  white-space:nowrap;text-align:center}
.BUY{background:var(--buy-bg);color:var(--buy)}.WAIT{background:var(--wait-bg);color:var(--wait)}
.WATCH{background:var(--watch-bg);color:var(--watch)}
.tag{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);background:#eef3fd;border-radius:8px;padding:4px 9px}
.tag.b{color:var(--brand);background:rgba(59,110,245,.1)}

/* stats (3D tilt) */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-top:46px;perspective:1100px}
.tilt{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px;
  box-shadow:var(--shadow);position:relative;transform-style:preserve-3d;
  transform:perspective(900px) rotateX(calc(var(--rx,0deg) + var(--srx,0deg))) rotateY(var(--ry,0deg)) translateY(calc(var(--lift,0px) * -1));
  transition:transform .18s ease,box-shadow .25s ease;will-change:transform}
.tilt:hover{box-shadow:var(--shadow-lg)}
.tilt:after{content:"";position:absolute;inset:0;border-radius:var(--radius);pointer-events:none;opacity:0;transition:opacity .3s;
  background:radial-gradient(240px 240px at var(--mx,50%) var(--my,0%),rgba(255,255,255,.9),transparent 60%)}
.tilt.hot:after{opacity:1}
.stat .ico{font-size:20px;transform:translateZ(34px);display:inline-block}
.stat .n{font-family:'IBM Plex Mono',monospace;font-size:27px;font-weight:600;letter-spacing:-.6px;margin-top:6px;transform:translateZ(46px)}
.stat .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.12em;margin-top:3px;transform:translateZ(24px)}
.stat .x{color:var(--muted);font-size:12px;margin-top:5px;transform:translateZ(20px)}

/* section heading */
.section{margin:62px 0 4px;display:flex;align-items:baseline;gap:14px}
.section h2{font-size:20px;font-weight:800;letter-spacing:-.5px}
.section .hint{font-size:12px;color:var(--dim);font-family:'IBM Plex Mono',monospace;margin-left:auto}

/* recommendation cards */
.rec{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px 22px;margin-top:16px;
  display:grid;grid-template-columns:auto 1fr auto auto;gap:20px;align-items:center;
  box-shadow:var(--shadow);transition:transform .25s ease,box-shadow .25s ease}
.rec:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.rec .route{font-weight:700;font-size:16.5px;letter-spacing:-.3px}
.rec .dates{color:var(--dim);font-size:12.5px;margin-top:1px}
.rec .reason{color:var(--muted);font-size:13.5px;margin-top:5px}
.conf{margin-top:9px;max-width:300px}
.conf .lab{font-size:11px;color:var(--dim);font-family:'IBM Plex Mono',monospace;display:flex;justify-content:space-between}
.bar{height:7px;background:#eaf0fa;border-radius:7px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;width:0;border-radius:7px;background:linear-gradient(90deg,var(--brand),var(--teal));
  transition:width 1.1s cubic-bezier(.2,.7,.2,1)}
.tags{margin-top:10px;display:flex;gap:7px;flex-wrap:wrap}
.spark{width:130px;height:46px}
.pricebox{text-align:right}
.price{font-family:'IBM Plex Mono',monospace;font-size:27px;font-weight:600;letter-spacing:-1px}
.pricelbl{color:var(--dim);font-size:11px;font-family:'IBM Plex Mono',monospace;margin-top:2px}
@media(max-width:780px){.rec{grid-template-columns:auto 1fr}.spark,.pricebox{grid-column:1/-1;justify-self:start}
  .pricebox{text-align:left}.spark{width:100%}}

/* charts */
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;margin-top:16px}
@media(max-width:780px){.grid2{grid-template-columns:1fr}}
.panel{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow)}
.panel h3{font-size:14px;font-weight:700}
.panel .ph{color:var(--dim);font-size:12px;margin:2px 0 14px}
.canvas-wrap{position:relative;height:250px}

/* insight cards */
.icards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px;margin-top:16px}
.icard{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:20px;box-shadow:var(--shadow);
  transition:transform .25s,box-shadow .25s}
.icard:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.icard .top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.icard .rt{font-weight:700;font-size:15.5px;letter-spacing:-.2px}
.icard .when{font-size:11.5px;color:var(--dim);font-family:'IBM Plex Mono',monospace;margin-top:2px}
.icard .big{font-family:'IBM Plex Mono',monospace;font-size:24px;font-weight:600;letter-spacing:-.5px;color:var(--buy)}
.icard .big small{font-size:11px;color:var(--dim);font-weight:400;display:block;text-align:right}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0 4px}
.fact{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:9px 11px}
.fact .k{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em}
.fact .v{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:14px;margin-top:2px}
.alist{margin-top:14px;display:flex;flex-direction:column;gap:7px}
.aline{display:flex;align-items:center;gap:10px;font-size:13px}
.av{width:26px;height:26px;border-radius:8px;flex:none;display:grid;place-items:center;color:#fff;
  font-size:11px;font-weight:700;font-family:'IBM Plex Mono',monospace}
.aline .nm{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.aline .pr{font-family:'IBM Plex Mono',monospace;font-weight:600;color:var(--muted)}
.stopbar{display:flex;height:8px;border-radius:6px;overflow:hidden;margin-top:14px;background:#eaf0fa}
.stopbar i{display:block;height:100%}
.stopkey{display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.stopkey span{display:flex;align-items:center;gap:5px}
.swatch{width:9px;height:9px;border-radius:3px;display:inline-block}

/* fares */
details{background:#fff;border:1px solid var(--line);border-radius:16px;margin-top:12px;padding:2px 18px;
  box-shadow:var(--shadow);overflow:hidden}
summary{cursor:pointer;padding:15px 0;font-weight:600;font-size:14.5px;display:flex;align-items:center;
  justify-content:space-between;gap:10px;list-style:none}
summary::-webkit-details-marker{display:none}
summary .pill{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--brand);
  background:rgba(59,110,245,.1);padding:3px 9px;border-radius:20px;white-space:nowrap}
summary:after{content:"+";color:var(--dim);font-size:18px;margin-left:6px;transition:.2s}
details[open] summary:after{transform:rotate(45deg)}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:8px}
th,td{text-align:left;padding:10px 8px;border-top:1px solid var(--line)}
th{color:var(--dim);font-family:'IBM Plex Mono',monospace;font-weight:500;font-size:10.5px;text-transform:uppercase;letter-spacing:.1em}
td.num,th.num{text-align:right;font-family:'IBM Plex Mono',monospace}
tr.best td{background:var(--buy-bg)}
.cheapest{font-size:10px;color:var(--buy);font-weight:700;font-family:'IBM Plex Mono',monospace;margin-left:8px}
.chip{font-family:'IBM Plex Mono',monospace;font-size:11px;padding:2px 8px;border-radius:20px;background:#eef3fd;color:var(--muted)}
.chip.ns{background:var(--buy-bg);color:var(--buy)}

/* empty */
.empty{background:#fff;border:1px solid var(--line);border-radius:var(--radius);padding:54px 30px;text-align:center;
  margin-top:34px;box-shadow:var(--shadow)}
.empty h2{font-size:22px;margin-bottom:10px}.empty p{color:var(--muted);max-width:520px;margin:0 auto}

/* footer / author logo */
.foot{margin-top:70px;border-top:1px solid var(--line);padding-top:26px;color:var(--dim);font-size:12.5px;line-height:1.8}
.author{display:flex;align-items:center;gap:13px;margin-top:22px}
.author .ring{width:46px;height:46px;border-radius:14px;flex:none;display:grid;place-items:center;color:#fff;font-weight:800;
  font-family:'IBM Plex Mono',monospace;font-size:16px;background:linear-gradient(135deg,var(--brand),var(--brand2));
  box-shadow:0 10px 22px -8px rgba(122,92,240,.7)}
.author .who{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.16em}
.author .nm{font-size:16px;font-weight:700;color:var(--ink);letter-spacing:-.2px}

/* reveal */
.reveal{opacity:0;transform:translateY(26px);transition:opacity .7s ease,transform .7s cubic-bezier(.2,.7,.2,1)}
.reveal.in{opacity:1;transform:none}
@media(prefers-reduced-motion:reduce){
  .reveal{opacity:1;transform:none;transition:none}.bar>i{transition:none}
  .blob{animation:none}.tilt,.deal{transition:none}}
</style></head><body>

<div class="aurora"><span class="blob b1"></span><span class="blob b2"></span><span class="blob b3"></span></div>

<nav class="nav"><div class="row">
  <div class="brand"><span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"
    stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v3M9 8h6l1.5 11h-9L9 8zM7.5 19h9M10 8l.5-3h3l.5 3"/></svg></span>Faro</div>
  <div class="links"><a href="#deals">Deals</a><a href="#trend">Trends</a><a href="#insights">Insights</a><a href="#fares">Fares</a></div>
  <div class="ctx"><span class="pulse"></span><span id="clock">live</span><span id="navwx"></span></div>
</div></nav>

<div class="wrap">
  <header class="hero">
    <div class="reveal">
      <div class="eyebrow" id="eyebrow">SMART FARE TIMING</div>
      <h1>Book your flight<br>at the <span class="grad">perfect moment.</span></h1>
      <div class="lead" id="lead">Faro watches the fares every day and tells you whether to grab the seat now or
        hold out for a better price — with the airline, stops and flight time for every option.</div>
      <div class="chips" id="hchips"></div>
    </div>
    <div class="dealwrap reveal"><div class="deal" id="deal"></div></div>
  </header>

  <div class="stats" id="stats"></div>
  <div id="body"></div>

  <div class="foot reveal">
    Faro refreshes once a day. Fares are gathered from Google Flights across rotating browser profiles; live weather
    and currency come from <a href="https://open-meteo.com">Open-Meteo</a> and
    <a href="https://www.exchangerate-api.com">ExchangeRate-API</a>. The buy / wait call is from a quantile
    gradient-boosting model trained on each route's own price history (heuristic fallback while history is thin).
    Informational only — confirm the live fare before booking. Open data lives in the <code>data/</code> folder.
    <div class="author">
      <div class="ring">YS</div>
      <div><div class="who">Built &amp; maintained by</div><div class="nm">Yasas Sri Wickramasinghe</div></div>
    </div>
  </div>
</div>

<script>
const D = ''' + data + r''';
const CUR = D.currency || 'NZD';
const fmt = (n,c)=> (c||CUR)+' '+Math.round(n).toLocaleString();
const fmt0 = n => Math.round(n).toLocaleString();
const dur = m => m ? Math.floor(m/60)+'h '+(m%60).toString().padStart(2,'0')+'m' : '—';
const stops = s => s===0 ? 'Nonstop' : s+' stop'+(s>1?'s':'');
const esc = s => (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const palette=['#3b6ef5','#7a5cf0','#0fb6a8','#e8902a','#e0567d','#5a6b86','#12b07c','#9a6cff'];
const acolor = s => palette[Math.abs([...(s||'?')].reduce((a,c)=>a*31+c.charCodeAt(0)|0,7))%palette.length];
const initials = s => (s||'?').replace(/[^A-Za-z ]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'?';
const MM=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

/* friendly route from an itin key like "CHC-CMB 2026-09-01 -> 2026-09-22" */
function parseItin(s){
  const m=(s||'').match(/^([A-Z]{3})-([A-Z]{3}) (\d{4})-(\d{2})-(\d{2}) -> (\d{4})-(\d{2})-(\d{2})$/);
  if(!m) return {title:s||'',dates:'',nights:null};
  const cn=c=>(D.cities&&D.cities[c])||c;
  const d1=new Date(+m[3],+m[4]-1,+m[5]), d2=new Date(+m[6],+m[7]-1,+m[8]);
  const f=d=>d.getDate()+' '+MM[d.getMonth()];
  return {title:cn(m[1])+' → '+cn(m[2]), dates:f(d1)+' – '+f(d2)+' '+d2.getFullYear(),
          nights:Math.round((d2-d1)/86400000)};
}

/* ---------- live clock (destination local time) ---------- */
const dest = (D.primary && D.primary.dest) || null;
function clock(){
  const el=document.getElementById('clock'); if(!el) return;
  if(dest && dest.tz){
    try{el.textContent = new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',
      timeZone:dest.tz}).format(new Date())+' '+dest.name;}catch(e){el.textContent=new Date().toUTCString().slice(17,25)+' UTC';}
  }else el.textContent=new Date().toUTCString().slice(17,25)+' UTC';
  setTimeout(clock,1000);
}
clock();

/* ---------- live weather (Open-Meteo, keyless) ---------- */
const WX={0:['☀️','Clear'],1:['🌤️','Mostly clear'],2:['⛅','Partly cloudy'],3:['☁️','Cloudy'],
  45:['🌫️','Fog'],48:['🌫️','Fog'],51:['🌦️','Drizzle'],53:['🌦️','Drizzle'],55:['🌦️','Drizzle'],
  61:['🌧️','Rain'],63:['🌧️','Rain'],65:['🌧️','Heavy rain'],71:['🌨️','Snow'],80:['🌦️','Showers'],
  81:['🌧️','Showers'],82:['⛈️','Storms'],95:['⛈️','Thunderstorm'],96:['⛈️','Thunderstorm'],99:['⛈️','Thunderstorm']};
let weather=null;
async function loadWeather(){
  if(!dest||dest.lat==null) return;
  try{
    const r=await fetch('https://api.open-meteo.com/v1/forecast?latitude='+dest.lat+'&longitude='+dest.lon+
      '&current=temperature_2m,weather_code');
    const j=await r.json(); const c=j.current;
    const w=WX[c.weather_code]||['🌡️','']; weather={t:Math.round(c.temperature_2m),ico:w[0],desc:w[1]};
  }catch(e){}
  const nav=document.getElementById('navwx');
  if(weather&&nav) nav.textContent='· '+weather.ico+' '+weather.t+'°C';
  renderHeroChips();
}

/* ---------- live currency (exchangerate-api open endpoint, keyless) ---------- */
let rates=null;
async function loadRates(){
  try{const r=await fetch('https://open.er-api.com/v6/latest/'+CUR);const j=await r.json();
    if(j&&j.result==='success') rates=j.rates;}catch(e){}
  renderDeal(); renderHeroChips();
}
function convert(amount,to){ if(!rates||!rates[to]) return null; return amount*rates[to]; }
function fxLine(amount){
  if(!rates) return '';
  const want=['USD','AUD','GBP','EUR','LKR','INR'].filter(c=>c!==CUR&&rates[c]);
  return want.slice(0,4).map(c=>'<span>'+fmt(convert(amount,c),c)+'</span>').join('');
}

/* ---------- header context chips ---------- */
function renderHeroChips(){
  const el=document.getElementById('hchips'); if(!el) return;
  const S=D.stats, ch=[];
  if(weather) ch.push(['<span class="ico">'+weather.ico+'</span>','<b>'+weather.t+'°C</b><small>'+esc(weather.desc)+' in '+esc(dest.name)+'</small>']);
  if(dest&&dest.tz) ch.push(['<span class="ico">🕐</span>','<b id="lt"></b><small>local time, '+esc(dest.name)+'</small>']);
  if(S&&S.airlines) ch.push(['<span class="ico">✈️</span>','<b>'+S.airlines+' airlines</b><small>'+(S.routes||0)+' routes tracked</small>']);
  el.innerHTML = ch.map(c=>'<div class="lchip">'+c[0]+'<div>'+c[1]+'</div></div>').join('');
  if(dest&&dest.tz) ltick();
}
function ltick(){const el=document.getElementById('lt'); if(!el) return;
  try{el.textContent=new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:dest.tz}).format(new Date());}catch(e){}
  setTimeout(ltick,1000*20);}

/* ---------- hero deal card ---------- */
function renderDeal(){
  const el=document.getElementById('deal'); if(!el) return;
  const best=(D.insights&&D.insights[0])||null;
  if(!best){ el.innerHTML='<div class="lbl">Best deal</div><div class="route" style="margin-top:10px">Collecting fares…</div>'+
    '<div class="dates">Check back soon — the first deal appears within a few daily scans.</div>'; return; }
  const pi=parseItin(best.itinerary);
  const rec=(D.recs||[]).find(r=>r.itinerary===best.itinerary);
  const sig=rec?rec.signal:'WATCH';
  const save=Math.max(0,Math.round((best.median||best.min)-best.min));
  const sub=rec&&rec.signal==='BUY'?'Good time to book':rec&&rec.signal==='WAIT'?'Prices may still fall':'Worth watching';
  el.innerHTML=
    '<div class="tagrow"><span class="lbl">Best deal right now</span>'+
      '<span class="sig '+sig+'" style="margin-left:auto">'+sig+'</span></div>'+
    '<div class="route">'+esc(pi.title)+'</div>'+
    '<div class="dates">'+esc(pi.dates)+(pi.nights?' · '+pi.nights+' nights':'')+'</div>'+
    '<div class="pricebig">'+fmt(best.min)+' <small>cheapest return</small></div>'+
    '<div class="dates" style="margin-top:6px">'+esc(sub)+
      (best.cheapest_airline?' · '+esc(best.cheapest_airline):'')+
      (save>0?' · save ~'+fmt(save)+' vs typical':'')+'</div>'+
    '<div class="meta2">'+
      (best.fastest?'<span class="tag">⏱ '+dur(best.fastest)+' fastest</span>':'')+
      (best.nonstop?'<span class="tag b">Nonstop available</span>':'<span class="tag">connections only</span>')+
      (best.days_to_departure!=null?'<span class="tag">'+best.days_to_departure+' days to go</span>':'')+
    '</div>'+
    '<div class="fx">'+fxLine(best.min)+'</div>';
}

/* ---------- count-up stats with 3D tilt ---------- */
const S=D.stats;
document.getElementById('eyebrow').textContent =
  D.primary ? (D.primary.origin.name+' → '+D.primary.dest.name+' · SMART FARE TIMING') : 'SMART FARE TIMING';
if(D.primary){
  document.getElementById('lead').innerHTML =
    'Faro watches fares for <b>'+esc(D.primary.origin.name)+' → '+esc(D.primary.dest.name)+
    '</b> every day and tells you whether to book now or wait — with the airline, stops and flight time for every option.';
}
const cells=[
  ['🎫','Fare records',S.total_offers||0,''],
  ['🛫','Routes tracked',S.routes||0,''],
  ['🏷️','Airlines seen',S.airlines||0,''],
  ['📅','Daily scans',S.scans||0,''],
];
if(S.avg_price) cells.push(['📊','Average fare',Math.round(S.avg_price),'cur']);
if(S.fastest) cells.push(['⚡','Fastest trip',S.fastest,'dur']);
if(S.cheapest_ever) cells.push(['💎','Lowest ever',Math.round(S.cheapest_ever.price),'cur',esc(S.cheapest_ever.airline||'')]);
document.getElementById('stats').innerHTML = cells.map(c=>
  '<div class="tilt stat reveal"><div class="ico">'+c[0]+'</div>'+
  '<div class="n" data-to="'+c[2]+'" data-kind="'+(c[3]||'')+'">0</div>'+
  '<div class="l">'+c[1]+'</div>'+(c[4]?'<div class="x">'+c[4]+'</div>':'')+'</div>').join('');

function animateCount(el){
  if(el.dataset.done) return; el.dataset.done='1';
  const to=+el.dataset.to, kind=el.dataset.kind;
  const sh=t=>kind==='cur'?fmt(t):kind==='dur'?dur(t):fmt0(t);
  let st=null;
  function step(ts){st=st||ts;const k=Math.min(1,(ts-st)/1100);
    el.textContent=sh(to*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(step);}
  requestAnimationFrame(step);
}

/* ---------- body sections ---------- */
const body=document.getElementById('body');
function add(html){const d=document.createElement('div');d.innerHTML=html;while(d.firstChild)body.appendChild(d.firstChild);}

if(!D.recs.length){
  add('<div class="empty reveal"><h2>Collecting data…</h2><p>Faro has just started watching this route. '+
    'Signals, trends and the airline breakdown appear once there are a few days of price history — usually within a week.</p></div>');
}else{
  add('<div class="section reveal" id="deals"><h2>Today’s signals</h2><span class="hint">act-now first</span></div>');
  D.recs.forEach((r,i)=>{
    const pi=parseItin(r.itinerary), conf=r.confidence||0, tags=[];
    if(r.predicted_low) tags.push(['forecast low '+fmt(r.predicted_low),1]);
    if(r.signal==='WAIT'&&r.expected_savings) tags.push(['save ~'+fmt(r.expected_savings)+' by waiting',1]);
    if(r.prob_drop!=null) tags.push([r.prob_drop+'% chance of a drop',0]);
    if(r.days_to_departure!=null) tags.push([r.days_to_departure+' days to go',0]);
    add('<div class="rec reveal">'+
      '<span class="sig '+r.signal+'">'+r.signal+'</span>'+
      '<div><div class="route">'+esc(pi.title)+'</div><div class="dates">'+esc(pi.dates)+
        (pi.nights?' · '+pi.nights+' nights':'')+'</div>'+
        '<div class="reason">'+esc(r.reason)+'</div>'+
        '<div class="conf"><div class="lab"><span>confidence</span><span>'+conf+'%</span></div>'+
          '<div class="bar"><i data-w="'+conf+'"></i></div></div>'+
        '<div class="tags">'+tags.map(t=>'<span class="tag'+(t[1]?' b':'')+'">'+esc(t[0])+'</span>').join('')+'</div></div>'+
      '<canvas class="spark" id="spark'+i+'"></canvas>'+
      '<div class="pricebox"><div class="price">'+fmt(r.price)+'</div>'+
        '<div class="pricelbl">low '+fmt(r.trailing_min)+' · '+r.points+' pts</div></div></div>');
  });

  add('<div class="section reveal" id="trend"><h2>Price trends &amp; airlines</h2></div>'+
    '<div class="grid2"><div class="panel reveal"><h3>Cheapest fare over time</h3>'+
      '<div class="ph">one cheapest-per-day point per route</div>'+
      '<div class="canvas-wrap"><canvas id="trendChart"></canvas></div></div>'+
    '<div class="panel reveal"><h3>Best fare by airline</h3>'+
      '<div class="ph">lowest fare each carrier offered in the latest scan</div>'+
      '<div class="canvas-wrap"><canvas id="airChart"></canvas></div></div></div>');

  if(D.insights&&D.insights.length){
    add('<div class="section reveal" id="insights"><h2>Market insights</h2><span class="hint">latest scan</span></div>');
    let h='<div class="icards">';
    D.insights.forEach(r=>{
      const pi=parseItin(r.itinerary), sb=r.stops, tot=Math.max(1,sb.nonstop+sb.one+sb.two_plus);
      const seg=(n,c)=>n?'<i style="width:'+(n/tot*100)+'%;background:'+c+'"></i>':'';
      const alist=(r.airline_prices||[]).map(a=>
        '<div class="aline"><span class="av" style="background:'+acolor(a.name)+'">'+initials(a.name)+'</span>'+
        '<span class="nm">'+esc(a.name)+'</span><span class="pr">'+fmt(a.price)+'</span></div>').join('')
        ||'<div class="aline" style="color:var(--dim)">airline not reported</div>';
      h+='<div class="icard reveal"><div class="top"><div><div class="rt">'+esc(pi.title)+'</div>'+
          '<div class="when">'+esc(pi.dates)+' · '+r.offers+' offers</div></div>'+
          '<div style="text-align:right"><div class="big">'+fmt(r.min)+'</div><small>cheapest</small></div></div>'+
        '<div class="facts">'+
          '<div class="fact"><div class="k">Typical</div><div class="v">'+fmt(r.median)+'</div></div>'+
          '<div class="fact"><div class="k">Highest</div><div class="v">'+fmt(r.max)+'</div></div>'+
          '<div class="fact"><div class="k">Fastest</div><div class="v">'+dur(r.fastest)+'</div></div>'+
          '<div class="fact"><div class="k">Typical time</div><div class="v">'+dur(r.typical_duration)+'</div></div></div>'+
        '<div class="stopbar">'+seg(sb.nonstop,'#12b07c')+seg(sb.one,'#3b6ef5')+seg(sb.two_plus,'#e8902a')+'</div>'+
        '<div class="stopkey"><span><i class="swatch" style="background:#12b07c"></i>'+sb.nonstop+' nonstop</span>'+
          '<span><i class="swatch" style="background:#3b6ef5"></i>'+sb.one+' · 1 stop</span>'+
          '<span><i class="swatch" style="background:#e8902a"></i>'+sb.two_plus+' · 2+ stops</span></div>'+
        '<div class="alist">'+alist+'</div></div>';
    });
    add(h+'</div>');
  }

  if(D.latest_offers&&Object.keys(D.latest_offers).length){
    add('<div class="section reveal" id="fares"><h2>Latest fares</h2><span class="hint">cheapest per route</span></div>');
    Object.keys(D.latest_offers).forEach(itin=>{
      const rows=D.latest_offers[itin]; if(!rows.length) return;
      const pi=parseItin(itin), best=Math.min.apply(null,rows.map(o=>o.price));
      let t='<details class="reveal"><summary><span>'+esc(pi.title)+' · '+esc(pi.dates)+'</span>'+
        '<span class="pill">'+rows.length+' offers · from '+fmt(best)+'</span></summary>'+
        '<table><thead><tr><th>Airline</th><th>Stops</th><th>Flight time</th><th class="num">Price</th></tr></thead><tbody>';
      rows.forEach(o=>{const isb=o.price===best;
        t+='<tr class="'+(isb?'best':'')+'"><td><span class="av" style="display:inline-grid;vertical-align:middle;'+
          'width:22px;height:22px;margin-right:8px;background:'+acolor(o.airline)+'">'+initials(o.airline)+'</span>'+
          (esc(o.airline)||'<span style="color:var(--dim)">—</span>')+(isb?'<span class="cheapest">CHEAPEST</span>':'')+'</td>'+
          '<td><span class="chip'+(o.stops===0?' ns':'')+'">'+stops(o.stops)+'</span></td>'+
          '<td class="mono">'+dur(o.duration)+'</td><td class="num">'+fmt(o.price)+'</td></tr>';});
      add(t+'</tbody></table></details>');
    });
  }
}

/* ---------- charts ---------- */
function gridOpts(){return{responsive:true,maintainAspectRatio:false,
  plugins:{legend:{labels:{font:{family:'Sora',size:11},color:'#56678a',boxWidth:12,padding:14}}},
  scales:{x:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}},
          y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b8',font:{family:'IBM Plex Mono',size:10}}}}};}
function placeholder(id,msg){const c=document.getElementById(id);if(!c)return;
  (c.closest('.canvas-wrap')||c.parentNode).innerHTML='<div style="height:100%;display:grid;place-items:center;'+
    'color:var(--dim);font-size:13px;text-align:center;padding:0 20px">'+msg+'</div>';}
let charted=false;
function drawCharts(){
  if(charted) return; charted=true;
  if(typeof Chart==='undefined'){placeholder('trendChart','Chart library unavailable.');placeholder('airChart','Chart library unavailable.');return;}
  D.recs.forEach((r,i)=>{const h=D.history[r.itinerary]||[],c=document.getElementById('spark'+i);
    if(!c||h.length<2) return;
    const g=c.getContext('2d').createLinearGradient(0,0,0,46);
    g.addColorStop(0,'rgba(59,110,245,.28)');g.addColorStop(1,'rgba(59,110,245,0)');
    new Chart(c,{type:'line',data:{labels:h.map(x=>x.d),datasets:[{data:h.map(x=>x.p),borderColor:'#3b6ef5',
      borderWidth:2,pointRadius:0,tension:.35,fill:true,backgroundColor:g}]},
      options:{responsive:false,plugins:{legend:{display:false},tooltip:{enabled:false}},scales:{x:{display:false},y:{display:false}}}});});
  const tc=document.getElementById('trendChart'),enough=Object.values(D.history).some(h=>h.length>=2);
  if(tc&&!enough){placeholder('trendChart','Booking curves appear once each route has 2+ days of history. Check back tomorrow.');}
  else if(tc){const itins=Object.keys(D.history);
    const labels=[...new Set([].concat(...itins.map(k=>D.history[k].map(x=>x.d))))].sort();
    const ds=itins.map((k,i)=>{const m=Object.fromEntries(D.history[k].map(x=>[x.d,x.p]));const pi=parseItin(k);
      return{label:pi.title,data:labels.map(d=>m[d]??null),borderColor:palette[i%palette.length],
        backgroundColor:palette[i%palette.length],borderWidth:2,pointRadius:2,tension:.3,spanGaps:true};});
    new Chart(tc,{type:'line',data:{labels,datasets:ds},options:gridOpts()});}
  const ac=document.getElementById('airChart');
  if(ac&&D.airline_market&&D.airline_market.length){const m=D.airline_market;
    new Chart(ac,{type:'bar',data:{labels:m.map(a=>a.name),datasets:[{label:'Lowest fare ('+CUR+')',
      data:m.map(a=>a.min),backgroundColor:m.map(a=>acolor(a.name)),borderRadius:8,maxBarThickness:32}]},
      options:Object.assign(gridOpts(),{indexAxis:'y',plugins:{legend:{display:false},
        tooltip:{callbacks:{label:c=>' '+fmt(c.raw)+' · '+m[c.dataIndex].offers+' offers'}}}})});}
}

/* ---------- 3D tilt (pointer) ---------- */
const fine = matchMedia('(hover:hover) and (pointer:fine)').matches && !matchMedia('(prefers-reduced-motion:reduce)').matches;
function bindTilt(card,maxX,maxY){
  card.addEventListener('pointermove',e=>{const r=card.getBoundingClientRect();
    const px=(e.clientX-r.left)/r.width-.5, py=(e.clientY-r.top)/r.height-.5;
    card.style.setProperty('--rx',(-py*maxX).toFixed(2)+'deg');
    card.style.setProperty('--ry',(px*maxY).toFixed(2)+'deg');
    card.style.setProperty('--mx',(px*100+50)+'%');card.style.setProperty('--my',(py*100+50)+'%');
    card.classList.add('hot');});
  card.addEventListener('pointerleave',()=>{card.style.setProperty('--rx','0deg');
    card.style.setProperty('--ry','0deg');card.classList.remove('hot');});
}
function initTilt(){
  if(!fine) return;
  document.querySelectorAll('.tilt').forEach(c=>bindTilt(c,9,11));
  const deal=document.getElementById('deal'); if(deal) bindTilt(deal,7,9);
}
/* scroll-driven tilt for the stat cards */
let ticking=false;
function scrollTilt(){
  if(!fine) return;
  document.querySelectorAll('.tilt').forEach(card=>{
    const r=card.getBoundingClientRect(),vh=innerHeight,d=(r.top+r.height/2-vh/2)/vh;
    card.style.setProperty('--srx',(Math.max(-1,Math.min(1,d))*7).toFixed(2)+'deg');
    card.style.setProperty('--lift',(Math.max(0,1-Math.abs(d)*1.6)*8).toFixed(1)+'px');});
}
addEventListener('scroll',()=>{if(!ticking){ticking=true;requestAnimationFrame(()=>{scrollTilt();ticking=false;});}},{passive:true});

/* ---------- reveal + boot ---------- */
function fireReveal(el){el.classList.add('in');
  el.querySelectorAll&&el.querySelectorAll('.n[data-to]').forEach(animateCount);
  if(el.classList.contains('n')&&el.dataset.to)animateCount(el);
  el.querySelectorAll&&el.querySelectorAll('.bar>i[data-w]').forEach(b=>b.style.width=b.dataset.w+'%');}
function revealAll(){document.querySelectorAll('.reveal').forEach(fireReveal);
  document.querySelectorAll('.stat .n[data-to]').forEach(animateCount);}
if(!('IntersectionObserver' in window)){revealAll();}
else{const io=new IntersectionObserver(es=>es.forEach(e=>{if(!e.isIntersecting)return;fireReveal(e.target);io.unobserve(e.target);}),{threshold:.14});
  document.querySelectorAll('.reveal').forEach(el=>io.observe(el));}
setTimeout(()=>document.querySelectorAll('.reveal:not(.in)').forEach(el=>{
  if(el.getBoundingClientRect().top<innerHeight*1.2) fireReveal(el);}),1200);

renderDeal(); renderHeroChips(); initTilt(); scrollTilt();
requestAnimationFrame(drawCharts);
loadWeather(); loadRates();
</script></body></html>'''
