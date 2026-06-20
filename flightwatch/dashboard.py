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

    if not df.empty and (df["status"] == "ok").any():
        ok = _with_itin(df[df["status"] == "ok"])

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
<title>FlightWatch &middot; CHC &harr; CMB fare tracker</title>
<meta name="description" content="Daily Christchurch&harr;Colombo fares with a forecast-driven buy / wait signal, airline mix, stops and flight times.">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f3f6fc;--bg2:#eaf0fb;--card:#ffffff;--ink:#0f1b2e;--muted:#5a6b86;--dim:#8a99b5;
  --line:#e6ecf6;--line2:#dde6f4;
  --brand:#3b6ef5;--brand2:#7a5cf0;--teal:#10b5a8;
  --buy:#13b07c;--buy-bg:#e7f7f0;--wait:#e8902a;--wait-bg:#fdf1e2;--watch:#6b7ba0;--watch-bg:#eef1f8;
  --shadow:0 10px 30px -12px rgba(28,52,99,.18);--shadow-lg:0 24px 60px -20px rgba(28,52,99,.28);
}
*{margin:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:radial-gradient(1200px 600px at 80% -10%,#e7edff 0,transparent 55%),
  radial-gradient(900px 500px at -10% 10%,#e4fbf6 0,transparent 50%),var(--bg);
  color:var(--ink);font-family:'Sora',system-ui,-apple-system,sans-serif;line-height:1.55;
  -webkit-font-smoothing:antialiased}
.mono{font-family:'IBM Plex Mono',monospace}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 90px}
a{color:var(--brand);text-decoration:none}

/* sticky header */
.nav{position:sticky;top:0;z-index:50;backdrop-filter:saturate(160%) blur(12px);
  background:rgba(243,246,252,.72);border-bottom:1px solid var(--line);}
.nav .row{max-width:1080px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;gap:14px}
.logo{font-weight:800;letter-spacing:-.3px;display:flex;align-items:center;gap:9px;font-size:18px}
.logo .dot{width:11px;height:11px;border-radius:50%;
  background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 0 0 4px rgba(59,110,245,.14)}
.nav .links{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
.nav .links a{font-size:13px;color:var(--muted);padding:6px 12px;border-radius:20px;transition:.2s}
.nav .links a:hover{color:var(--ink);background:var(--bg2)}
.live{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--buy);display:flex;align-items:center;gap:6px}
.live .pulse{width:8px;height:8px;border-radius:50%;background:var(--buy);
  box-shadow:0 0 0 0 rgba(19,176,124,.5);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(19,176,124,.5)}70%{box-shadow:0 0 0 8px rgba(19,176,124,0)}100%{box-shadow:0 0 0 0 rgba(19,176,124,0)}}

/* hero */
.hero{padding:62px 0 8px;text-align:center;position:relative}
.eyebrow{font-family:'IBM Plex Mono',monospace;letter-spacing:.34em;color:var(--brand);font-size:12px;font-weight:600}
h1{font-size:clamp(34px,6vw,58px);font-weight:800;letter-spacing:-1.5px;margin:14px 0 8px;line-height:1.02}
h1 .grad{background:linear-gradient(120deg,var(--brand),var(--brand2) 50%,var(--teal));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--muted);font-size:17px;max-width:620px;margin:0 auto}
.meta{color:var(--dim);font-size:12.5px;font-family:'IBM Plex Mono',monospace;margin-top:14px}

/* stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-top:34px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px 18px;
  box-shadow:var(--shadow);position:relative;overflow:hidden}
.stat:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;
  background:linear-gradient(var(--brand),var(--brand2))}
.stat .n{font-family:'IBM Plex Mono',monospace;font-size:26px;font-weight:600;letter-spacing:-.5px}
.stat .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.13em;margin-top:3px}
.stat .x{color:var(--muted);font-size:12px;margin-top:5px}

/* section heading */
.section{margin:58px 0 6px;display:flex;align-items:baseline;gap:14px}
.section h2{font-size:13px;font-family:'IBM Plex Mono',monospace;letter-spacing:.2em;text-transform:uppercase;
  color:var(--muted);font-weight:600}
.section:after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--line2),transparent)}
.section .hint{font-size:12px;color:var(--dim)}

/* recommendation cards */
.rec{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px 22px;margin-top:16px;
  display:grid;grid-template-columns:auto 1fr auto auto;gap:20px;align-items:center;
  box-shadow:var(--shadow);transition:transform .25s ease,box-shadow .25s ease}
.rec:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.sig{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:13px;padding:8px 14px;border-radius:14px;
  white-space:nowrap;text-align:center;min-width:78px}
.BUY{background:var(--buy-bg);color:var(--buy)}
.WAIT{background:var(--wait-bg);color:var(--wait)}
.WATCH{background:var(--watch-bg);color:var(--watch)}
.rec .itin{font-weight:700;font-size:16px;letter-spacing:-.3px}
.rec .reason{color:var(--muted);font-size:13.5px;margin-top:3px}
.conf{margin-top:9px;max-width:280px}
.conf .lab{font-size:11px;color:var(--dim);font-family:'IBM Plex Mono',monospace;display:flex;justify-content:space-between}
.bar{height:7px;background:var(--bg2);border-radius:7px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;width:0;border-radius:7px;
  background:linear-gradient(90deg,var(--brand),var(--teal));transition:width 1.1s cubic-bezier(.2,.7,.2,1)}
.tags{margin-top:10px;display:flex;gap:7px;flex-wrap:wrap}
.tag{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--muted);background:var(--bg2);
  border-radius:8px;padding:4px 9px}
.tag.b{color:var(--brand);background:rgba(59,110,245,.1)}
.spark{width:130px;height:46px}
.pricebox{text-align:right}
.price{font-family:'IBM Plex Mono',monospace;font-size:27px;font-weight:600;letter-spacing:-1px}
.pricelbl{color:var(--dim);font-size:11px;font-family:'IBM Plex Mono',monospace;margin-top:2px}
@media(max-width:760px){.rec{grid-template-columns:auto 1fr;}.spark,.pricebox{grid-column:1/-1;justify-self:start}
  .pricebox{text-align:left}.spark{width:100%}}

/* charts row */
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;margin-top:16px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px;box-shadow:var(--shadow)}
.panel h3{font-size:14px;font-weight:700;margin-bottom:2px}
.panel .ph{color:var(--dim);font-size:12px;margin-bottom:14px}
.canvas-wrap{position:relative;height:240px}

/* insight cards */
.icards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;margin-top:16px}
.icard{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px;box-shadow:var(--shadow);
  transition:transform .25s,box-shadow .25s}
.icard:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.icard .top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.icard .rt{font-weight:700;font-size:15px;letter-spacing:-.2px}
.icard .when{font-size:11px;color:var(--dim);font-family:'IBM Plex Mono',monospace;margin-top:2px}
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
.stopbar{display:flex;height:8px;border-radius:6px;overflow:hidden;margin-top:14px;background:var(--bg2)}
.stopbar i{display:block;height:100%}
.stopkey{display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.stopkey span{display:flex;align-items:center;gap:5px}
.swatch{width:9px;height:9px;border-radius:3px;display:inline-block}

/* offers tables */
details{background:var(--card);border:1px solid var(--line);border-radius:16px;margin-top:12px;
  padding:2px 18px;box-shadow:var(--shadow);overflow:hidden}
summary{cursor:pointer;padding:15px 0;font-weight:600;font-size:14.5px;display:flex;align-items:center;
  justify-content:space-between;list-style:none}
summary::-webkit-details-marker{display:none}
summary .pill{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--brand);
  background:rgba(59,110,245,.1);padding:3px 9px;border-radius:20px}
summary:after{content:"+";color:var(--dim);font-size:18px;margin-left:10px;transition:.2s}
details[open] summary:after{transform:rotate(45deg)}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:8px}
th,td{text-align:left;padding:10px 8px;border-top:1px solid var(--line)}
th{color:var(--dim);font-family:'IBM Plex Mono',monospace;font-weight:500;font-size:10.5px;
  text-transform:uppercase;letter-spacing:.1em}
td.num,th.num{text-align:right;font-family:'IBM Plex Mono',monospace}
tr.best td{background:var(--buy-bg)}
.cheapest{font-size:10px;color:var(--buy);font-weight:700;font-family:'IBM Plex Mono',monospace;margin-left:8px}
.chip{font-family:'IBM Plex Mono',monospace;font-size:11px;padding:2px 8px;border-radius:20px;
  background:var(--bg2);color:var(--muted)}
.chip.ns{background:var(--buy-bg);color:var(--buy)}

/* empty */
.empty{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:54px 30px;text-align:center;
  margin-top:34px;box-shadow:var(--shadow)}
.empty h2{font-size:22px;margin-bottom:10px}.empty p{color:var(--muted);max-width:520px;margin:0 auto}

.foot{color:var(--dim);font-size:12.5px;margin-top:60px;border-top:1px solid var(--line);padding-top:22px;line-height:1.8}

/* scroll reveal */
.reveal{opacity:0;transform:translateY(22px);transition:opacity .7s ease,transform .7s cubic-bezier(.2,.7,.2,1)}
.reveal.in{opacity:1;transform:none}
@media(prefers-reduced-motion:reduce){.reveal{opacity:1;transform:none;transition:none}
  .bar>i{transition:none}}
</style></head><body>

<nav class="nav"><div class="row">
  <div class="logo"><span class="dot"></span>FlightWatch</div>
  <div class="links">
    <a href="#recs">Signals</a><a href="#trend">Trends</a>
    <a href="#insights">Insights</a><a href="#fares">Fares</a>
  </div>
  <div class="live" id="live"><span class="pulse"></span><span id="livet">live</span></div>
</div></nav>

<div class="wrap">
  <header class="hero">
    <div class="eyebrow">CHC &#8644; CMB &middot; OPEN FARE TRACKER</div>
    <h1>Stop guessing.<br><span class="grad">Book at the right price.</span></h1>
    <div class="sub">Daily fares for the Christchurch &harr; Colombo corridor &mdash; airlines, stops and flight
      times for every offer, with a forecast-driven <b>buy / wait</b> signal.</div>
    <div class="meta" id="meta"></div>
    <div class="stats" id="stats"></div>
  </header>
  <div id="body"></div>
  <div class="foot reveal">
    Updated automatically once a day via GitHub Actions. Fares are scraped from Google
    Flights across multiple browser fingerprints &mdash; every offer (airline, stops,
    duration, price) is stored as open CSV in <code>data/</code>. Signals come from a
    quantile gradient-boosting model trained on each route's own price history, with a
    heuristic fallback while history is thin. Informational only &mdash; verify the live
    fare before booking.
  </div>
</div>

<script>
const D = ''' + data + r''';
const fmt = n => D.currency + ' ' + Math.round(n).toLocaleString();
const fmt0 = n => Math.round(n).toLocaleString();
const dur = m => m ? Math.floor(m/60)+'h '+(m%60).toString().padStart(2,'0')+'m' : '—';
const stops = s => s===0 ? 'Nonstop' : s+' stop'+(s>1?'s':'');
const esc = s => (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const palette=['#3b6ef5','#7a5cf0','#10b5a8','#e8902a','#e0567d','#5a6b86','#13b07c','#9a6cff'];
const acolor = s => palette[Math.abs([...(s||'?')].reduce((a,c)=>a*31+c.charCodeAt(0)|0,7))%palette.length];
const initials = s => (s||'?').replace(/[^A-Za-z ]/g,'').split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0]).join('').toUpperCase()||'?';

/* ---- header meta + live clock ---- */
const S = D.stats;
document.getElementById('meta').textContent =
  S.scans + ' daily scans · built ' + D.generated +
  (D.model ? ' · model ±' + fmt(D.model.mae) + ' (n=' + D.model.n + ')'
           : ' · heuristic mode (collecting data)');
(function clock(){const t=new Date().toUTCString().slice(17,25);
  document.getElementById('livet').textContent=t+' UTC';setTimeout(clock,1000);})();

/* ---- count-up stats ---- */
const cells = [
  ['Fare records', S.total_offers||0, ''],
  ['Routes tracked', S.routes||0, ''],
  ['Airlines seen', S.airlines||0, ''],
  ['Daily scans', S.scans||0, ''],
];
if(S.avg_price) cells.push(['Average fare', Math.round(S.avg_price), 'cur']);
if(S.cheapest_ever) cells.push(['Cheapest seen', Math.round(S.cheapest_ever.price), 'cur',
  esc(S.cheapest_ever.airline||S.cheapest_ever.itinerary)]);
if(S.fastest) cells.push(['Fastest trip', S.fastest, 'dur']);
document.getElementById('stats').innerHTML = cells.map((c,i)=>
  '<div class="stat reveal"><div class="n" data-to="'+c[1]+'" data-kind="'+(c[2]||'')+'">0</div>'+
  '<div class="l">'+c[0]+'</div>'+(c[3]?'<div class="x">'+c[3]+'</div>':'')+'</div>').join('');

function animateCount(el){
  const to=+el.dataset.to, kind=el.dataset.kind, dec=(kind==='dur'?0:0);
  const dush=t=>kind==='cur'?fmt(t):kind==='dur'?dur(t):fmt0(t);
  let st=null,dur_ms=1100;
  function step(ts){st=st||ts;const k=Math.min(1,(ts-st)/dur_ms);
    el.textContent=dush(to*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(step);}
  requestAnimationFrame(step);
}

const body = document.getElementById('body');
function add(html){const d=document.createElement('div');d.innerHTML=html;
  while(d.firstChild)body.appendChild(d.firstChild);}

if(!D.recs.length){
  add('<div class="empty reveal"><h2>Collecting data…</h2>'+
    '<p>The tracker has just started. Recommendations, trends and the airline breakdown appear once each '+
    'route has a few days of price history (usually within a week). Come back soon — or browse the '+
    '<code>data/</code> folder in the repo.</p></div>');
}else{
  /* ===== Recommendations ===== */
  add('<div class="section reveal" id="recs"><h2>Today’s signals</h2>'+
      '<span class="hint">sorted: act-now first</span></div>');
  D.recs.forEach((r,i)=>{
    const conf=r.confidence||0, tags=[];
    if(r.predicted_low) tags.push(['forecast low '+fmt(r.predicted_low),1]);
    if(r.signal==='WAIT'&&r.expected_savings) tags.push(['save ~'+fmt(r.expected_savings)+' by waiting',1]);
    if(r.prob_drop!=null) tags.push([r.prob_drop+'% chance of a drop',0]);
    if(r.days_to_departure!=null) tags.push([r.days_to_departure+' days to departure',0]);
    tags.push([r.method,0]);
    add('<div class="rec reveal">'+
      '<span class="sig '+r.signal+'">'+r.signal+'</span>'+
      '<div><div class="itin">'+esc(r.itinerary)+'</div>'+
        '<div class="reason">'+esc(r.reason)+'</div>'+
        '<div class="conf"><div class="lab"><span>confidence</span><span>'+conf+'%</span></div>'+
          '<div class="bar"><i data-w="'+conf+'"></i></div></div>'+
        '<div class="tags">'+tags.map(t=>'<span class="tag'+(t[1]?' b':'')+'">'+esc(t[0])+'</span>').join('')+'</div>'+
      '</div>'+
      '<canvas class="spark" id="spark'+i+'"></canvas>'+
      '<div class="pricebox"><div class="price">'+fmt(r.price)+'</div>'+
        '<div class="pricelbl">low '+fmt(r.trailing_min)+' · '+r.points+' pts</div></div>'+
    '</div>');
  });

  /* ===== Trends + airline market ===== */
  add('<div class="section reveal" id="trend"><h2>Price trends & airline market</h2></div>'+
    '<div class="grid2">'+
      '<div class="panel reveal"><h3>Cheapest fare over time</h3>'+
        '<div class="ph">one cheapest-per-day point per route</div>'+
        '<div class="canvas-wrap"><canvas id="trendChart"></canvas></div></div>'+
      '<div class="panel reveal"><h3>Best fare by airline</h3>'+
        '<div class="ph">lowest fare each carrier offered in the latest scan</div>'+
        '<div class="canvas-wrap"><canvas id="airChart"></canvas></div></div>'+
    '</div>');

  /* ===== Market insights ===== */
  if(D.insights && D.insights.length){
    add('<div class="section reveal" id="insights"><h2>Market insights · latest scan</h2></div>');
    let h='<div class="icards">';
    D.insights.forEach(r=>{
      const sb=r.stops, tot=Math.max(1,sb.nonstop+sb.one+sb.two_plus);
      const seg=(n,c)=>n?'<i style="width:'+(n/tot*100)+'%;background:'+c+'"></i>':'';
      const alist=(r.airline_prices||[]).map(a=>
        '<div class="aline"><span class="av" style="background:'+acolor(a.name)+'">'+initials(a.name)+'</span>'+
        '<span class="nm">'+esc(a.name)+'</span><span class="pr">'+fmt(a.price)+'</span></div>').join('')
        || '<div class="aline" style="color:var(--dim)">airline not reported</div>';
      h+='<div class="icard reveal"><div class="top"><div>'+
          '<div class="rt">'+esc(r.itinerary)+'</div>'+
          '<div class="when">'+r.offers+' offers · '+r.scan_date+
            (r.days_to_departure!=null?' · '+r.days_to_departure+'d out':'')+'</div></div>'+
          '<div style="text-align:right"><div class="big">'+fmt(r.min)+'</div><small>cheapest</small></div></div>'+
        '<div class="facts">'+
          '<div class="fact"><div class="k">Typical</div><div class="v">'+fmt(r.median)+'</div></div>'+
          '<div class="fact"><div class="k">Highest</div><div class="v">'+fmt(r.max)+'</div></div>'+
          '<div class="fact"><div class="k">Fastest</div><div class="v">'+dur(r.fastest)+'</div></div>'+
          '<div class="fact"><div class="k">Typical time</div><div class="v">'+dur(r.typical_duration)+'</div></div>'+
        '</div>'+
        '<div class="stopbar">'+seg(sb.nonstop,'#13b07c')+seg(sb.one,'#3b6ef5')+seg(sb.two_plus,'#e8902a')+'</div>'+
        '<div class="stopkey">'+
          '<span><i class="swatch" style="background:#13b07c"></i>'+sb.nonstop+' nonstop</span>'+
          '<span><i class="swatch" style="background:#3b6ef5"></i>'+sb.one+' · 1 stop</span>'+
          '<span><i class="swatch" style="background:#e8902a"></i>'+sb.two_plus+' · 2+ stops</span></div>'+
        '<div class="alist">'+alist+'</div>'+
      '</div>';
    });
    h+='</div>';
    add(h);
  }

  /* ===== Latest fares ===== */
  if(D.latest_offers && Object.keys(D.latest_offers).length){
    add('<div class="section reveal" id="fares"><h2>Latest fares · cheapest offers per route</h2></div>');
    Object.keys(D.latest_offers).forEach(itin=>{
      const rows=D.latest_offers[itin]; if(!rows.length) return;
      const best=Math.min.apply(null,rows.map(o=>o.price));
      let t='<details class="reveal"><summary><span>'+esc(itin)+'</span>'+
        '<span class="pill">'+rows.length+' offers · from '+fmt(best)+'</span></summary>'+
        '<table><thead><tr><th>Airline</th><th>Stops</th><th>Flight time</th><th class="num">Price</th></tr></thead><tbody>';
      rows.forEach(o=>{ const isb=o.price===best;
        t+='<tr class="'+(isb?'best':'')+'"><td><span class="av" style="display:inline-grid;vertical-align:middle;'+
          'width:22px;height:22px;margin-right:8px;background:'+acolor(o.airline)+'">'+initials(o.airline)+'</span>'+
          (esc(o.airline)||'<span style="color:var(--dim)">—</span>')+(isb?'<span class="cheapest">CHEAPEST</span>':'')+'</td>'+
          '<td><span class="chip'+(o.stops===0?' ns':'')+'">'+stops(o.stops)+'</span></td>'+
          '<td class="mono">'+dur(o.duration)+'</td><td class="num">'+fmt(o.price)+'</td></tr>'; });
      t+='</tbody></table></details>';
      add(t);
    });
  }
}

/* ===== charts ===== */
function gridOpts(){return{responsive:true,maintainAspectRatio:false,
  plugins:{legend:{labels:{font:{family:'Sora',size:11},color:'#5a6b86',boxWidth:12,padding:14}}},
  scales:{x:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b5',font:{family:'IBM Plex Mono',size:10}}},
          y:{grid:{color:'#eef2f9'},ticks:{color:'#8a99b5',font:{family:'IBM Plex Mono',size:10}}}}};}

function chartMissing(){return typeof Chart==='undefined';}
function placeholder(id,msg){const c=document.getElementById(id);if(!c)return;
  const w=c.closest('.canvas-wrap')||c.parentNode;
  w.innerHTML='<div style="height:100%;display:grid;place-items:center;color:var(--dim);'+
    'font-size:13px;text-align:center;padding:0 20px">'+msg+'</div>';}

let charted=false;
function drawCharts(){
  if(charted) return; charted=true;
  if(chartMissing()){
    placeholder('trendChart','Chart library unavailable.');
    placeholder('airChart','Chart library unavailable.');
    return;
  }
  // sparklines
  D.recs.forEach((r,i)=>{
    const h=D.history[r.itinerary]||[], c=document.getElementById('spark'+i);
    if(!c||h.length<2) return;
    const g=c.getContext('2d').createLinearGradient(0,0,0,46);
    g.addColorStop(0,'rgba(59,110,245,.28)');g.addColorStop(1,'rgba(59,110,245,0)');
    new Chart(c,{type:'line',data:{labels:h.map(x=>x.d),
      datasets:[{data:h.map(x=>x.p),borderColor:'#3b6ef5',borderWidth:2,pointRadius:0,tension:.35,
        fill:true,backgroundColor:g}]},
      options:{responsive:false,plugins:{legend:{display:false},tooltip:{enabled:false}},
        scales:{x:{display:false},y:{display:false}}}});
  });
  // trend chart (all routes)
  const tc=document.getElementById('trendChart');
  const enough=Object.values(D.history).some(h=>h.length>=2);
  if(tc && !enough){
    placeholder('trendChart','Booking curves appear once each route has 2+ days of history. '+
      'Collecting now — check back tomorrow.');
  }else if(tc){
    const itins=Object.keys(D.history);
    const labels=[...new Set([].concat(...itins.map(k=>D.history[k].map(x=>x.d))))].sort();
    const ds=itins.map((k,i)=>{const m=Object.fromEntries(D.history[k].map(x=>[x.d,x.p]));
      return{label:k.split(' ')[0]+' '+k.split(' ')[1],data:labels.map(d=>m[d]??null),
        borderColor:palette[i%palette.length],backgroundColor:palette[i%palette.length],
        borderWidth:2,pointRadius:2,tension:.3,spanGaps:true};});
    new Chart(tc,{type:'line',data:{labels,datasets:ds},options:gridOpts()});
  }
  // airline best-fare bar
  const ac=document.getElementById('airChart');
  if(ac && D.airline_market && D.airline_market.length){
    const m=D.airline_market;
    new Chart(ac,{type:'bar',data:{labels:m.map(a=>a.name),
      datasets:[{label:'Lowest fare ('+D.currency+')',data:m.map(a=>a.min),
        backgroundColor:m.map(a=>acolor(a.name)),borderRadius:8,maxBarThickness:34}]},
      options:Object.assign(gridOpts(),{indexAxis:'y',plugins:{legend:{display:false},
        tooltip:{callbacks:{label:c=>' '+fmt(c.raw)+' · '+m[c.dataIndex].offers+' offers'}}}})});
  }
}

/* ===== scroll reveal + triggers ===== */
function revealAll(){document.querySelectorAll('.reveal').forEach(el=>{el.classList.add('in');
  el.querySelectorAll('.n[data-to]').forEach(animateCount);
  el.querySelectorAll('.bar>i[data-w]').forEach(b=>b.style.width=b.dataset.w+'%');});
  document.querySelectorAll('.stat .n[data-to]').forEach(animateCount);}
if(!('IntersectionObserver' in window)){revealAll();}else{
const io=new IntersectionObserver((es)=>{es.forEach(e=>{
  if(!e.isIntersecting)return; e.target.classList.add('in');
  e.target.querySelectorAll&&e.target.querySelectorAll('.n[data-to]').forEach(animateCount);
  if(e.target.classList.contains('n')&&e.target.dataset.to)animateCount(e.target);
  e.target.querySelectorAll&&e.target.querySelectorAll('.bar>i[data-w]').forEach(b=>b.style.width=b.dataset.w+'%');
  io.unobserve(e.target);
});},{threshold:.16});
document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
document.querySelectorAll('.stat .n[data-to]').forEach(el=>io.observe(el));
}
// safety net: never leave content hidden if observers somehow don't fire
setTimeout(()=>document.querySelectorAll('.reveal:not(.in)').forEach(el=>{
  const r=el.getBoundingClientRect();
  if(r.top<window.innerHeight*1.2) el.classList.add('in');}),1200);
// charts can render immediately (canvas sizing needs layout)
requestAnimationFrame(drawCharts);
</script></body></html>'''
