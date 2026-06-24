"""
Offline "AI" analytics layer. No external APIs, no LLM -- every insight here is
derived purely from the data FlightWatch already collects, so it stays free,
reproducible and fully self-contained.

What it produces (all consumed by the dashboard, some also by alerts):

  * deal_scores      -- a 0-100 "how good is this fare right now" per itinerary,
                        from where today's cheapest sits in its own history.
  * anomalies        -- statistically unusual drops/spikes (robust MAD z-score),
                        the trigger for price-drop alerts.
  * cheapest_day     -- which departure/return weekday (and which date-pair) is
                        cheapest, so a traveller can shift a day or two to save.
  * best_time_to_book-- from the model's forward curve: when to book and the
                        expected low, with the saving vs booking today.
  * what_changed     -- a diff of the two most recent scans: movers + new lows.
  * airline_intel    -- cheapest carrier and the nonstop premium per route.
  * narratives       -- a crisp, data-filled paragraph per route that reads like
                        an analyst wrote it (pure templating over the numbers).
"""

import numpy as np
import pandas as pd

from . import predict

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _ok_with_itin(df):
    ok = df[df["status"] == "ok"].copy()
    if ok.empty:
        return ok
    ok["route"] = ok["origin"] + "-" + ok["destination"]
    ok["itin"] = ok["route"] + " " + ok["depart_date"].astype(str) + \
                 " -> " + ok["return_date"].astype(str)
    ok["price"] = ok["price"].astype(float)
    return ok


def _clean(name):
    from .dashboard import clean_airline           # deferred: avoids import cycle
    return clean_airline(name)


def _iata(name):
    from .dashboard import airline_iata
    return airline_iata(name)


def _layover(val):
    from .dashboard import _clean_layover
    return _clean_layover(val)


# --------------------------------------------------------------------------- #
def deal_scores(df):
    """0-100 deal score for each itinerary's latest cheapest fare."""
    daily = predict.daily_min(df)
    out = {}
    for itin, h in daily.groupby("itin"):
        prices = h.sort_values("scan_date")["price"].astype(float)
        cur, lo, hi = float(prices.iloc[-1]), float(prices.min()), float(prices.max())
        med = float(prices.median())
        if len(prices) < 2:
            score = 50
        else:
            rank = float((prices <= cur).mean())          # cheaper than most -> small
            near = 100 * (1 - (cur - lo) / max(hi - lo, 1.0))
            score = round(0.6 * near + 0.4 * 100 * (1 - rank))
        score = int(max(0, min(100, score)))
        label = ("Great" if score >= 80 else "Good" if score >= 60
                 else "Fair" if score >= 40 else "High")
        out[itin] = {"score": score, "label": label, "price": round(cur),
                     "vs_median_pct": round((cur - med) / max(med, 1.0) * 100),
                     "vs_min_pct": round((cur - lo) / max(lo, 1.0) * 100)}
    return out


def anomalies(df, z=2.5):
    """Robust-z (MAD) drops/spikes vs each route's recent history. Drives alerts."""
    daily = predict.daily_min(df)
    out = []
    for itin, h in daily.groupby("itin"):
        p = h.sort_values("scan_date")["price"].astype(float).to_numpy()
        if len(p) < 6:
            continue
        cur, hist = p[-1], p[:-1]
        med = float(np.median(hist))
        mad = float(np.median(np.abs(hist - med))) or 1.0
        score = (cur - med) / (1.4826 * mad)
        if score <= -z or score >= z:
            out.append({"itin": itin, "kind": "drop" if score < 0 else "spike",
                        "price": round(float(cur)), "baseline": round(med),
                        "z": round(float(score), 1),
                        "pct": round((cur - med) / max(med, 1.0) * 100)})
    out.sort(key=lambda a: a["z"])
    return out


def cheapest_day(df):
    """Cheapest departure/return weekday and date-pair from the latest scan."""
    ok = _ok_with_itin(df)
    if ok.empty:
        return {}
    day = ok[ok["scan_date"] == ok["scan_date"].max()].copy()
    day["dep_dow"] = pd.to_datetime(day["depart_date"]).dt.dayofweek
    day["ret_dow"] = pd.to_datetime(day["return_date"]).dt.dayofweek

    def by(col):
        g = day.groupby(col)["price"].min()
        return [{"dow": int(k), "label": DOW[int(k)], "min": round(float(v))}
                for k, v in g.sort_values().items()]

    pairs = (day.groupby(["depart_date", "return_date"])["price"].min()
                .reset_index().sort_values("price"))
    most = float(pairs["price"].max()) if len(pairs) else 0.0
    date_pairs = [{"depart": r.depart_date, "return": r.return_date,
                   "min": round(float(r.price)),
                   "save_vs_dearest": round(most - float(r.price))}
                  for r in pairs.head(20).itertuples()]
    dep, ret = by("dep_dow"), by("ret_dow")
    return {"dep_dow": dep, "ret_dow": ret, "date_pairs": date_pairs,
            "best_dep": dep[0] if dep else None, "best_ret": ret[0] if ret else None}


def best_time_to_book(recs):
    """From the model's forward curve: when to book + the expected saving."""
    out = []
    for r in recs:
        if r.get("method") != "model" or not r.get("curve"):
            continue
        dtd_now, best_dtd = r["days_to_departure"], r["best_dtd"]
        save = max(0, round(r["price"] - r["predicted_low"]))
        out.append({"itin": r["itinerary"], "best_dtd": best_dtd,
                    "days_from_now": max(0, dtd_now - best_dtd),
                    "book_now": best_dtd >= dtd_now - 1,
                    "predicted_low": r["predicted_low"], "current": round(r["price"]),
                    "save": save})
    out.sort(key=lambda x: -x["save"])
    return out


def what_changed(df):
    """Diff the two most recent scan slots per itinerary: movers + new lows."""
    ok = _ok_with_itin(df)
    if ok.empty:
        return {}
    cheapest = ok.sort_values("price").drop_duplicates(["itin", "scan_slot"])
    daily = predict.daily_min(df)
    counts = daily.groupby("itin").size().to_dict()
    prior_min = {}
    for itin, h in daily.groupby("itin"):
        p = h.sort_values("scan_date")["price"].astype(float).to_numpy()
        prior_min[itin] = float(p[:-1].min()) if len(p) >= 2 else None

    movers, new_lows, since = [], [], None
    for itin, h in cheapest.groupby("itin"):
        h = h.sort_values("scan_slot")
        cur = float(h["price"].iloc[-1])
        since = since or h["scan_slot"].iloc[-1]
        pm = prior_min.get(itin)
        if counts.get(itin, 0) >= 3 and pm is not None and cur <= pm:
            new_lows.append({"itin": itin, "price": round(cur)})
        if len(h) < 2:
            continue
        prev = float(h["price"].iloc[-2])
        delta = cur - prev
        if abs(delta) >= 1:
            movers.append({"itin": itin, "from": round(prev), "to": round(cur),
                           "delta": round(delta), "pct": round(delta / max(prev, 1.0) * 100, 1)})
    movers.sort(key=lambda m: m["delta"])
    return {"since": since, "movers": movers, "new_lows": new_lows}


def airline_intel(df):
    """Per route: cheapest carrier, nonstop availability and the nonstop premium."""
    ok = _ok_with_itin(df)
    if ok.empty:
        return []
    day = ok[ok["scan_date"] == ok["scan_date"].max()]
    out = []
    for route, g in day.groupby("route"):
        g = g.copy()
        g["clean"] = g["airline"].map(_clean)
        priced = g[g["clean"] != ""]
        cheapest_airline = (priced.sort_values("price")["clean"].iloc[0]
                            if not priced.empty else "")
        overall_min = float(g["price"].min())
        ns = g[g["stops"] == 0]["price"]
        nonstop_min = float(ns.min()) if not ns.empty else None
        out.append({
            "route": route,
            "cheapest_airline": cheapest_airline,
            "cheapest_iata": _iata(cheapest_airline),
            "nonstop_available": nonstop_min is not None,
            "nonstop_premium": (round(nonstop_min - overall_min)
                                if nonstop_min is not None else None),
            "carriers": int(priced["clean"].nunique()),
        })
    return out


# --------------------------------------------------------------------------- #
# Market-wide analytics -- aggregates over the WHOLE rolling grid (every
# departure date x trip length we scrape), not just the dense fixed itineraries.
# Everything here is derived purely from the data we already hold, so it stays
# free and offline, and powers the dashboard's "Market analytics" widgets.
# --------------------------------------------------------------------------- #
def _cheapest_per_itin(ok):
    """One row per itinerary -- its cheapest offer in the most recent scan."""
    latest = ok[ok["scan_date"] == ok["scan_date"].max()]
    return latest.sort_values("price").drop_duplicates("itin", keep="first").copy()


def market_pulse(df, recs=None):
    """A single 0-100 "is now a good time to buy?" index for the whole market.

    Blends three honest, offline signals:
      * value      -- the average deal score (how cheap fares sit within their
                      own recent range); higher = cheaper right now.
      * momentum   -- the day-over-day move in the grid's median cheapest fare;
                      falling prices nudge the index up (good for buyers).
      * signal mix -- the share of itineraries the engine currently calls BUY.
    """
    ok = _ok_with_itin(df)
    if ok.empty:
        return None
    deals = deal_scores(df)
    value = float(np.mean([d["score"] for d in deals.values()])) if deals else 50.0

    # Grid-wide momentum: the median per-itinerary fare change between the two
    # most recent scans, measured on the SAME itineraries in both (matched
    # pairs) so a shifting grid composition can't masquerade as a price move.
    per = ok.sort_values("price").drop_duplicates(["itin", "scan_date"])
    scans = sorted(per["scan_date"].unique())
    momentum_pct = None
    if len(scans) >= 2:
        prev = per[per["scan_date"] == scans[-2]].set_index("itin")["price"]
        cur = per[per["scan_date"] == scans[-1]].set_index("itin")["price"]
        common = prev.index.intersection(cur.index)
        if len(common) >= 5:
            pct = (cur.loc[common].astype(float) - prev.loc[common].astype(float)) \
                  / prev.loc[common].astype(float).clip(lower=1.0) * 100
            momentum_pct = round(float(pct.median()), 1)

    buy = wait = watch = 0
    for r in (recs or []):
        s = r.get("signal")
        buy += s == "BUY"; wait += s == "WAIT"; watch += s == "WATCH"
    total = buy + wait + watch
    buy_share = (buy / total) if total else 0.0

    # Falling fares (negative momentum) help buyers; cap the contribution at +-8%.
    mom_term = 50.0
    if momentum_pct is not None:
        mom_term = max(0.0, min(100.0, 50.0 - momentum_pct * 6.0))

    score = int(round(0.55 * value + 0.25 * mom_term + 0.20 * 100 * buy_share))
    score = max(0, min(100, score))
    label = ("Great time to buy" if score >= 70 else "Leaning buy" if score >= 56
             else "Balanced market" if score >= 44 else "Better to wait"
             if score >= 30 else "Fares running high")
    if momentum_pct is None:
        note = "Across every tracked departure right now."
    elif momentum_pct <= -1:
        note = f"The typical fare fell {abs(momentum_pct)}% since the last scan."
    elif momentum_pct >= 1:
        note = f"The typical fare rose {momentum_pct}% since the last scan."
    else:
        note = "The typical fare is holding steady since the last scan."
    return {"score": score, "label": label, "value": int(round(value)),
            "momentum_pct": momentum_pct, "buy": buy, "wait": wait, "watch": watch,
            "note": note}


def advance_curve(df, min_n=3):
    """Empirical 'how far ahead should I book?' curve.

    Cross-section of the latest scan: the median cheapest fare grouped by how
    many days out the departure is. Distinct from the per-itinerary model
    forecast -- this is what the open dataset actually shows across the grid.
    """
    ok = _ok_with_itin(df)
    if ok.empty:
        return None
    cheap = _cheapest_per_itin(ok)
    cheap["dtd"] = pd.to_numeric(cheap["days_to_departure"], errors="coerce")
    cheap = cheap.dropna(subset=["dtd"])
    if cheap.empty:
        return None
    g = cheap.groupby(cheap["dtd"].astype(int))["price"]
    pts = [{"dtd": int(k), "p": int(round(float(v.median()))), "n": int(v.size)}
           for k, v in g if v.size >= min_n]
    pts.sort(key=lambda x: x["dtd"])
    if len(pts) < 3:
        return None

    # Smooth (centred rolling mean) to find the genuinely cheapest lead window.
    dtds = [p["dtd"] for p in pts]
    prices = np.array([p["p"] for p in pts], dtype=float)
    k = min(7, len(prices))
    kern = np.ones(k) / k
    smooth = np.convolve(prices, kern, mode="same")
    j = int(np.argmin(smooth))
    best_dtd = int(dtds[j])
    best_price = int(round(float(prices[j])))
    worst_price = int(round(float(prices.max())))
    return {"points": pts, "best_dtd": best_dtd, "best_price": best_price,
            "save_vs_worst": max(0, worst_price - best_price)}


def length_curve(df):
    """Cheapest bookable fare at each trip length (nights) -- the value sweet spot."""
    ok = _ok_with_itin(df)
    if ok.empty:
        return None
    latest = ok[ok["scan_date"] == ok["scan_date"].max()].copy()
    latest["len"] = pd.to_numeric(latest["trip_length"], errors="coerce")
    latest = latest.dropna(subset=["len"])
    if latest.empty:
        return None
    g = latest.groupby(latest["len"].astype(int))["price"]
    pts = [{"len": int(k), "min": int(round(float(v.min()))), "n": int(v.size)}
           for k, v in g]
    pts.sort(key=lambda x: x["len"])
    if len(pts) < 2:
        return None
    best = min(pts, key=lambda x: x["min"])
    return {"points": pts, "best_len": best["len"], "best_price": best["min"]}


def price_distribution(df, bins=12):
    """Histogram of the cheapest fare per itinerary in the latest scan."""
    ok = _ok_with_itin(df)
    if ok.empty:
        return None
    prices = _cheapest_per_itin(ok)["price"].astype(float).to_numpy()
    prices = prices[np.isfinite(prices)]
    if prices.size < 5:
        return None
    counts, edges = np.histogram(prices, bins=min(bins, max(4, prices.size // 6)))
    out = [{"lo": int(round(edges[i])), "hi": int(round(edges[i + 1])),
            "count": int(counts[i])} for i in range(len(counts))]
    return {"bins": out, "n": int(prices.size),
            "min": int(round(float(prices.min()))),
            "p25": int(round(float(np.percentile(prices, 25)))),
            "median": int(round(float(np.median(prices)))),
            "p75": int(round(float(np.percentile(prices, 75)))),
            "max": int(round(float(prices.max())))}


def savings_leaderboard(df, top=8):
    """Across the whole grid: the trips priced furthest below the typical fare
    for their trip length right now -- the standout deals a visitor can book."""
    ok = _ok_with_itin(df)
    if ok.empty:
        return []
    cheap = _cheapest_per_itin(ok)
    cheap["len"] = pd.to_numeric(cheap["trip_length"], errors="coerce")
    typical = cheap.groupby("len")["price"].median()
    out = []
    for r in cheap.itertuples():
        ln = getattr(r, "len")
        if pd.isna(ln):
            continue
        base = float(typical.get(ln, np.nan))
        if not np.isfinite(base):
            continue
        save = base - float(r.price)
        if save <= 0:
            continue
        air = _clean(getattr(r, "airline", ""))
        out.append({
            "itin": r.itin, "o": str(r.origin), "d": str(r.destination),
            "dep": str(r.depart_date), "ret": str(r.return_date),
            "len": int(ln), "price": int(round(float(r.price))),
            "typical": int(round(base)), "save": int(round(save)),
            "pct": int(round(save / max(base, 1.0) * 100)),
            "airline": air, "iata": _iata(air),
            "stops": (int(r.stops) if pd.notna(getattr(r, "stops", None)) else None),
            "via": _layover(getattr(r, "layover", "")),
            "dtd": (int(r.days_to_departure) if pd.notna(r.days_to_departure) else None),
        })
    out.sort(key=lambda x: -x["save"])
    return out[:top]


def market_analytics(df, recs=None):
    """Bundle the market-wide widgets into one JSON-serialisable section."""
    return {
        "pulse": market_pulse(df, recs),
        "advance_curve": advance_curve(df),
        "length_curve": length_curve(df),
        "price_distribution": price_distribution(df),
        "savings": savings_leaderboard(df),
    }


# --------------------------------------------------------------------------- #
def _title(itin):
    """'CHC-CMB 2026-09-01 -> 2026-09-22' -> 'CHC -> CMB, 1 Sep – 22 Sep'-ish id."""
    return itin.split(" ")[0]


def narrative(rec, deal, cur="NZD"):
    """A crisp, analyst-style paragraph for one itinerary -- pure templating."""
    money = lambda v: f"{cur} {round(v):,}"
    route = _title(rec["itinerary"])
    bits = [f"{route}: cheapest is {money(rec['price'])}"]
    if deal:
        vm = deal["vs_median_pct"]
        if vm <= -3:
            bits.append(f"{abs(vm)}% below its 30-day typical")
        elif vm >= 3:
            bits.append(f"{vm}% above its typical")
        else:
            bits.append("right around its typical price")
        if deal["score"] >= 80:
            bits.append("and near the lowest we've seen")
    if rec.get("prob_drop") is not None and rec.get("method") == "model":
        bits.append(f"the model puts a ~{rec['prob_drop']}% chance of a further drop")
        if rec["signal"] == "WAIT" and rec.get("expected_savings"):
            bits.append(f"with an expected low near {money(rec['predicted_low'])}")
    verdict = {"BUY": "Book now", "WAIT": "Hold and watch", "WATCH": "Keep watching"}[rec["signal"]]
    tail = f"{verdict} ({rec['confidence']}% confidence)."
    return ". ".join([", ".join(bits)]) + ". " + tail


def build(df, recs, bundle=None):
    """Aggregate every analytic into one JSON-serialisable payload section."""
    cur = (df[df["status"] == "ok"]["currency"].iloc[0]
           if (df["status"] == "ok").any() else "NZD")
    deals = deal_scores(df)
    anoms = anomalies(df)
    narr = {r["itinerary"]: narrative(r, deals.get(r["itinerary"]), cur) for r in recs}
    return {
        "deals": deals,
        "anomalies": anoms,
        "cheapest_day": cheapest_day(df),
        "best_time_to_book": best_time_to_book(recs),
        "what_changed": what_changed(df),
        "airline_intel": airline_intel(df),
        "narratives": narr,
        "backtest": predict.backtest(df),
        "market": market_analytics(df, recs),
    }
