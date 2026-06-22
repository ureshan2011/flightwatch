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
    }
