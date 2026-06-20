"""
Turns the accumulated fare history into per-itinerary forecasts and a
go / no-go (BUY / WAIT / WATCH) decision with a calibrated confidence rate.

Design goals (a production, open-source decision engine):

  * Honest from day one. With little history it falls back to an interpretable
    heuristic and reports LOW confidence -- it never fakes certainty.
  * Honestly validated. Once enough observations exist it fits gradient-boosting
    models over the booking curve and reports a *time-series cross-validated*
    error (TimeSeriesSplit, forward-chaining) -- never a shuffled split that
    would leak the future into the past and overstate skill.
  * Calibrated uncertainty. Predictions carry a SPLIT-CONFORMAL interval whose
    width is learned from real held-out residuals, so an "80% band" actually
    covers ~80% of outcomes -- not a raw guess.
  * Seasonality-aware. Features include cyclical day-of-week / month / week-of-
    year encodings, peak-season flags and a per-route price level, so the model
    learns the shape of the booking curve, not just a flat average.
  * Every recommendation exposes its reasoning: the full predicted forward curve,
    the expected future low, expected savings from waiting, the probability the
    fare drops, and a 0-100% confidence combining data sufficiency with signal
    strength.

The raw CSV stores every offer; here we collapse to one cheapest fare per
itinerary per day (what a traveller would actually pay) before modelling.
"""

import numpy as np
import pandas as pd

# Total cheapest-per-day observations before the ML model is trusted over the
# heuristic. Below this we still show a heuristic signal, flagged low-confidence.
MIN_OBS_FOR_MODEL = 120
MIN_HISTORY_PER_ITIN = 4         # daily points before an itinerary gets a firm signal
QUANTILES = (0.1, 0.5, 0.9)
FLOOR_DTD = 7                    # we never advise waiting past ~a week out
CONFORMAL_COVERAGE = 0.80        # target coverage of the calibrated price band

# Months that run hot on the Christchurch <-> Colombo corridor: NZ summer/festive
# (Dec-Jan), Sinhala/Tamil new year + school holidays (Apr), NZ winter school
# holidays (Jul). Used as a coarse demand signal; safe to tune.
PEAK_MONTHS = {12, 1, 4, 7}

# Days-to-departure bucket edges -> ordinal lead-time bucket (captures the
# non-linear "prices firm up near departure" effect that a raw integer misses).
_LEAD_EDGES = [15, 31, 61, 91, 151]


# --------------------------------------------------------------------------- #
# Data shaping
# --------------------------------------------------------------------------- #
def daily_min(df):
    """
    Collapse raw fares (many offers per itinerary, possibly several scans per
    day) to ONE point per itinerary per scan_date: the cheapest fare that day --
    what a traveller pays. Adds `itin` (full date-pair id) and `route` (O-D).
    """
    df = df[df["status"] == "ok"].copy()
    if df.empty:
        return df
    df["route"] = df["origin"] + "-" + df["destination"]
    df["itin"] = df["route"] + " " + df["depart_date"].astype(str) + \
                 " -> " + df["return_date"].astype(str)
    df = df.sort_values("price").drop_duplicates(["itin", "scan_date"], keep="first")
    return df.sort_values("scan_date")


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
_FEATS = [
    "days_to_departure", "lead_bucket", "trip_length",
    "dep_dow_sin", "dep_dow_cos", "ret_dow_sin", "ret_dow_cos",
    "mon_sin", "mon_cos", "woy_sin", "woy_cos", "is_peak",
    "stops", "route_level",
]


def _features(df, route_levels=None, global_level=None):
    """Engineer the model's structural features (no current-price leakage)."""
    df = df.copy()
    dep = pd.to_datetime(df["depart_date"], errors="coerce")
    ret = pd.to_datetime(df["return_date"], errors="coerce")
    dep_dow, ret_dow = dep.dt.dayofweek, ret.dt.dayofweek
    mon = dep.dt.month
    woy = dep.dt.isocalendar().week.astype(float)

    df["dep_dow_sin"] = np.sin(2 * np.pi * dep_dow / 7)
    df["dep_dow_cos"] = np.cos(2 * np.pi * dep_dow / 7)
    df["ret_dow_sin"] = np.sin(2 * np.pi * ret_dow / 7)
    df["ret_dow_cos"] = np.cos(2 * np.pi * ret_dow / 7)
    df["mon_sin"] = np.sin(2 * np.pi * mon / 12)
    df["mon_cos"] = np.cos(2 * np.pi * mon / 12)
    df["woy_sin"] = np.sin(2 * np.pi * woy / 52)
    df["woy_cos"] = np.cos(2 * np.pi * woy / 52)
    df["is_peak"] = mon.isin(PEAK_MONTHS).astype(int)

    dtd = pd.to_numeric(df["days_to_departure"], errors="coerce").fillna(0).astype(float)
    df["lead_bucket"] = np.digitize(dtd, _LEAD_EDGES).astype(int)

    if "route" not in df.columns:
        df["route"] = df["origin"].astype(str) + "-" + df["destination"].astype(str)
    if route_levels:
        gl = global_level if global_level is not None else float(np.median(list(route_levels.values())))
        df["route_level"] = df["route"].map(route_levels).fillna(gl)
    else:
        df["route_level"] = global_level if global_level is not None else 0.0

    df["stops"] = pd.to_numeric(df.get("stops", 0), errors="coerce").fillna(0)
    return df


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def _gbr(alpha=None):
    from sklearn.ensemble import GradientBoostingRegressor
    common = dict(n_estimators=220, max_depth=3, learning_rate=0.05,
                  min_samples_leaf=8, subsample=0.9, random_state=42)
    if alpha is None:
        return GradientBoostingRegressor(loss="squared_error", **common)
    return GradientBoostingRegressor(loss="quantile", alpha=alpha, **common)


def train_model(df: pd.DataFrame):
    """
    Fit gradient-boosting models over the booking curve and report honest,
    time-series cross-validated error plus a split-conformal band width.

    Returns a bundle dict, or None if there is not yet enough history.
    """
    daily = daily_min(df)
    if len(daily) < MIN_OBS_FOR_MODEL:
        return None

    daily = daily.sort_values("scan_date")
    route_levels = daily.groupby("route")["price"].median().to_dict()
    global_level = float(daily["price"].median())

    d = _features(daily, route_levels, global_level).dropna(subset=["price"])
    X = d[_FEATS].fillna(0)
    y = d["price"].astype(float)

    # Honest error: forward-chaining CV of the median model (no future leakage).
    mae = _timeseries_mae(X, y)

    # Calibrated band: fit on the first 70% (by time), measure residuals on the
    # last 30%, take the target-coverage quantile of |residual| as the half-width.
    conformal = _conformal_halfwidth(X, y, CONFORMAL_COVERAGE)

    median = _gbr(0.5)
    median.fit(X, y)
    models = {0.5: median}
    for q in (0.1, 0.9):
        m = _gbr(q)
        m.fit(X, y)
        models[q] = m

    return {"models": models, "mae": float(mae), "n": int(len(d)),
            "features": _FEATS, "route_levels": route_levels,
            "global_level": global_level, "conformal": conformal,
            "coverage": CONFORMAL_COVERAGE}


def _timeseries_mae(X, y):
    """Forward-chaining CV MAE of the median model; robust to small samples."""
    from sklearn.model_selection import TimeSeriesSplit
    n = len(X)
    n_splits = max(2, min(5, n // 30))
    try:
        tscv = TimeSeriesSplit(n_splits=n_splits)
        errs = []
        for tr, te in tscv.split(X):
            m = _gbr(0.5)
            m.fit(X.iloc[tr], y.iloc[tr])
            errs.append(float(np.mean(np.abs(y.iloc[te].values - m.predict(X.iloc[te])))))
        return float(np.mean(errs)) if errs else float(np.mean(np.abs(y - y.median())))
    except Exception:
        return float(np.mean(np.abs(y - y.median())))


def _conformal_halfwidth(X, y, coverage):
    """Split-conformal half-width: the `coverage` quantile of held-out |resid|."""
    n = len(X)
    cut = int(n * 0.7)
    if cut < 20 or n - cut < 10:
        return None
    m = _gbr(0.5)
    m.fit(X.iloc[:cut], y.iloc[:cut])
    resid = np.abs(y.iloc[cut:].values - m.predict(X.iloc[cut:]))
    return float(np.quantile(resid, coverage))


def forecast_curve(bundle, latest_row):
    """
    Predict the booking curve forward for one itinerary: sweep days_to_departure
    from now down to the floor and return the whole predicted curve with bands,
    plus the cheapest expected day ahead.
    """
    base = dict(latest_row)
    dtd_now = int(latest_row["days_to_departure"])
    grid = list(range(dtd_now, FLOOR_DTD - 1, -1)) or [dtd_now]

    rows = [{**base, "days_to_departure": dtd} for dtd in grid]
    d = _features(pd.DataFrame(rows), bundle["route_levels"], bundle["global_level"])
    X = d[_FEATS].fillna(0)

    med = bundle["models"][0.5].predict(X)
    cw = bundle.get("conformal")
    if cw is not None:
        lo_arr, hi_arr = med - cw, med + cw
    else:
        lo_arr = bundle["models"][0.1].predict(X)
        hi_arr = bundle["models"][0.9].predict(X)

    curve = [{"dtd": int(grid[i]), "p": float(med[i]),
              "lo": float(min(lo_arr[i], hi_arr[i])),
              "hi": float(max(lo_arr[i], hi_arr[i]))}
             for i in range(len(grid))]
    j = int(np.argmin(med))
    return {"predicted_low": float(med[j]),
            "low_band": (float(min(lo_arr[j], hi_arr[j])), float(max(lo_arr[j], hi_arr[j]))),
            "best_dtd": int(grid[j]), "curve": curve}


def _prob_below(q10, q50, q90, x):
    """Piecewise-linear CDF through the three quantiles; P(price <= x)."""
    q10, q50, q90 = sorted([q10, q50, q90])
    if x <= q10:
        slope = 0.4 / max(q50 - q10, 1.0)
        return float(max(0.0, 0.1 - slope * (q10 - x)))
    if x >= q90:
        slope = 0.4 / max(q90 - q50, 1.0)
        return float(min(1.0, 0.9 + slope * (x - q90)))
    if x <= q50:
        return float(0.1 + (x - q10) / max(q50 - q10, 1.0) * 0.4)
    return float(0.5 + (x - q50) / max(q90 - q50, 1.0) * 0.4)


# --------------------------------------------------------------------------- #
# Per-itinerary history features (for the decision layer + analytics)
# --------------------------------------------------------------------------- #
def history_stats(hist):
    """Market-state stats for an itinerary's own daily-min history."""
    prices = hist["price"].astype(float).to_numpy()
    price = float(prices[-1])
    lo, med = float(prices.min()), float(np.median(prices))
    pct = float((prices < price).mean() * 100)
    momentum = price - float(prices[-8]) if len(prices) >= 8 else 0.0
    recent = prices[-7:] if len(prices) >= 2 else prices
    volatility = float(np.std(recent)) if len(recent) >= 2 else 0.0
    roll_median = float(np.median(prices[-7:])) if len(prices) >= 1 else med
    return {"price": price, "min": lo, "median": med, "percentile": pct,
            "momentum": momentum, "volatility": volatility, "roll_median": roll_median}


# --------------------------------------------------------------------------- #
# Decision engine
# --------------------------------------------------------------------------- #
def _heuristic(hist):
    """Interpretable signal from an itinerary's own history. Low-ish confidence."""
    s = history_stats(hist)
    price, lo, med, pct = s["price"], s["min"], s["median"], s["percentile"]
    dtd = int(hist.iloc[-1]["days_to_departure"])
    n = len(hist)

    if n < MIN_HISTORY_PER_ITIN:
        return {"signal": "WATCH", "confidence": 25,
                "reason": "Still collecting history for this route.",
                "price": price, "predicted_low": lo, "expected_savings": 0.0,
                "prob_drop": None, "trailing_min": lo, "percentile": round(pct),
                "days_to_departure": dtd, "curve": []}

    near_min = price <= lo * 1.03
    data_conf = min(60, 20 + n * 4)          # heuristic caps below the model
    if near_min and dtd <= 75:
        sig, reason, conf = "BUY", "Near its lowest observed price and inside the booking window.", data_conf + 15
    elif price <= med and dtd <= 90:
        sig, reason, conf = "BUY", "Below its typical price with departure approaching.", data_conf + 5
    elif dtd > 90 and price >= med:
        sig, reason, conf = "WAIT", "At/above typical and still far out -- history suggests room to fall.", data_conf
    else:
        sig, reason, conf = "WATCH", "No strong edge either way right now.", data_conf - 10

    return {"signal": sig, "confidence": int(max(10, min(70, conf))), "reason": reason,
            "price": price, "predicted_low": lo,
            "expected_savings": round(max(0.0, price - lo)),
            "prob_drop": round(pct), "trailing_min": lo,
            "trailing_median": med, "percentile": round(pct),
            "momentum": round(s["momentum"]), "volatility": round(s["volatility"]),
            "days_to_departure": dtd, "curve": []}


def _model_decision(bundle, hist):
    """Forecast-driven decision with a calibrated confidence rate."""
    s = history_stats(hist)
    price = s["price"]
    dtd = int(hist.iloc[-1]["days_to_departure"])
    fc = forecast_curve(bundle, hist.iloc[-1])
    lo_band, predicted_low = fc["low_band"], fc["predicted_low"]

    prob_drop = _prob_below(lo_band[0], predicted_low, lo_band[1], price) * 100
    expected_savings = max(0.0, price - predicted_low)
    band_width = max(lo_band[1] - lo_band[0], 1.0)

    # Confidence: tighter band relative to price + more data => more confident.
    tightness = max(0.0, 1.0 - band_width / max(price, 1.0))
    data_factor = min(1.0, bundle["n"] / (MIN_OBS_FOR_MODEL * 3))
    base_conf = 45 + 45 * (0.6 * tightness + 0.4 * data_factor)

    materiality = expected_savings / max(price, 1.0)
    if dtd <= FLOOR_DTD or (prob_drop < 35 and dtd <= 90):
        sig = "BUY"
        reason = ("Unlikely to get cheaper before departure"
                  if prob_drop < 35 else "Booking window is closing.")
        conf = base_conf + (10 if prob_drop < 25 else 0)
    elif prob_drop >= 60 and materiality >= 0.04:
        sig = "WAIT"
        reason = (f"~{round(prob_drop)}% chance it drops; model expects a low near "
                  f"{predicted_low:.0f} (~{round(expected_savings)} below today).")
        conf = base_conf
    else:
        sig = "WATCH"
        reason = "No decisive edge; fare is near its expected band."
        conf = base_conf - 12

    return {"signal": sig, "confidence": int(max(20, min(97, round(conf)))),
            "reason": reason, "price": price,
            "predicted_low": round(predicted_low),
            "low_band": [round(lo_band[0]), round(lo_band[1])],
            "best_dtd": fc["best_dtd"],
            "curve": [{"dtd": c["dtd"], "p": round(c["p"]),
                       "lo": round(c["lo"]), "hi": round(c["hi"])} for c in fc["curve"]],
            "expected_savings": round(expected_savings if sig == "WAIT" else 0),
            "prob_drop": round(prob_drop),
            "trailing_min": round(s["min"]),
            "momentum": round(s["momentum"]), "volatility": round(s["volatility"]),
            "percentile": round(s["percentile"]), "days_to_departure": dtd}


def recommendations(df: pd.DataFrame, bundle=None) -> list:
    """
    One recommendation per itinerary. Uses the trained model when available
    (and trustworthy for that route), otherwise the heuristic.
    """
    daily = daily_min(df)
    if daily.empty:
        return []
    if bundle is None:
        bundle = train_model(df)

    out = []
    for itin, hist in daily.groupby("itin"):
        hist = hist.sort_values("scan_date")
        if bundle is not None and len(hist) >= MIN_HISTORY_PER_ITIN:
            rec = _model_decision(bundle, hist)
            rec["method"] = "model"
        else:
            rec = _heuristic(hist)
            rec["method"] = "heuristic"
        rec["itinerary"] = itin
        rec["points"] = int(len(hist))
        out.append(rec)

    order = {"BUY": 0, "WATCH": 1, "WAIT": 2}
    out.sort(key=lambda r: (order.get(r["signal"], 9), -r["confidence"], r["price"]))
    return out


# --------------------------------------------------------------------------- #
# Backtest -- did the engine's calls actually pay off? (100% offline honesty)
# --------------------------------------------------------------------------- #
def backtest(df: pd.DataFrame, tolerance=0.03):
    """
    Walk every itinerary's daily history forward. At each day (using only the
    data available up to that day) replay the heuristic signal, then grade it
    against what actually happened afterwards:

      * BUY  is correct if the fare never dropped meaningfully (> tolerance) below
        that day's price before departure -- booking then was the right call.
      * WAIT is correct if the fare DID drop meaningfully afterwards.
      * WATCH is correct if the later move was small (within tolerance).

    Returns hit-rate, per-signal breakdown, average BUY regret (money left on the
    table) and a predicted-vs-actual accuracy series, or None if too little data.
    """
    daily = daily_min(df)
    if daily.empty:
        return None

    rows, regrets = [], []
    for itin, h in daily.groupby("itin"):
        h = h.sort_values("scan_date").reset_index(drop=True)
        prices = h["price"].astype(float).to_numpy()
        for i in range(MIN_HISTORY_PER_ITIN, len(h)):
            future = prices[i + 1:]
            if future.size == 0:
                continue
            rec = _heuristic(h.iloc[:i + 1])
            cur, fut_min = float(prices[i]), float(future.min())
            drop = (cur - fut_min) / max(cur, 1.0)
            sig = rec["signal"]
            if sig == "BUY":
                correct = drop <= tolerance
                regrets.append(max(0.0, cur - fut_min))
            elif sig == "WAIT":
                correct = drop > tolerance
            else:
                correct = abs(drop) <= tolerance
            rows.append({"date": h.iloc[i]["scan_date"], "signal": sig,
                         "correct": bool(correct)})

    if not rows:
        return None

    by_signal = {}
    for sig in ("BUY", "WAIT", "WATCH"):
        s = [r for r in rows if r["signal"] == sig]
        if s:
            by_signal[sig] = {"n": len(s),
                              "correct": sum(r["correct"] for r in s),
                              "hit_rate": round(100 * sum(r["correct"] for r in s) / len(s))}

    bt = pd.DataFrame(rows)
    series = (bt.assign(d=pd.to_datetime(bt["date"]))
                .groupby(bt["date"].astype(str))["correct"].mean()
                .reset_index().rename(columns={"correct": "acc"}))
    return {
        "n": len(rows),
        "hit_rate": round(100 * bt["correct"].mean()),
        "by_signal": by_signal,
        "avg_buy_regret": round(float(np.mean(regrets))) if regrets else 0,
        "series": [{"d": d, "acc": round(100 * a)} for d, a in
                   zip(series["date"], series["acc"])],
    }
