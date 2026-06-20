"""
Turns the accumulated fare history into per-itinerary forecasts and a
go / no-go (BUY / WAIT) decision with a confidence rate.

Design goals (this is meant to be a production, open-source decision engine):

  * Honest from day one. With little history it falls back to an interpretable
    heuristic and reports LOW confidence -- it never fakes certainty.
  * Calibrated as data grows. Once enough observations exist it fits a
    *quantile* gradient-boosting model (10th / 50th / 90th percentile) over the
    booking curve, so every prediction carries an uncertainty band, not just a
    point estimate.
  * Every recommendation exposes its reasoning: predicted future low, expected
    savings from waiting, the probability the fare will drop, and a 0-100%
    confidence that combines data sufficiency with the strength of the signal.

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


# --------------------------------------------------------------------------- #
# Data shaping
# --------------------------------------------------------------------------- #
def daily_min(df):
    """
    Collapse raw fares (many offers per itinerary per day) to ONE point per
    itinerary per scan_date: the cheapest fare that day -- what a traveller pays.
    """
    df = df[df["status"] == "ok"].copy()
    if df.empty:
        return df
    df["itin"] = df["origin"] + "-" + df["destination"] + " " + \
                 df["depart_date"].astype(str) + " -> " + df["return_date"].astype(str)
    df = df.sort_values("price").drop_duplicates(["itin", "scan_date"], keep="first")
    return df.sort_values("scan_date")


def _latest_per_itinerary(df):
    return daily_min(df)


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def _features(df):
    df = df.copy()
    df["dep_dow"] = pd.to_datetime(df["depart_date"]).dt.dayofweek
    df["ret_dow"] = pd.to_datetime(df["return_date"]).dt.dayofweek
    return df


_FEATS = ["days_to_departure", "trip_length", "dep_dow", "ret_dow", "stops"]


def train_model(df: pd.DataFrame):
    """
    Fit quantile gradient-boosting models over the booking curve.

    Returns a bundle {models: {q: regressor}, mae, n, features} or None if there
    is not yet enough history. `mae` is honest cross-validated error of the
    median model, surfaced on the dashboard as the model's accuracy.
    """
    daily = daily_min(df)
    if len(daily) < MIN_OBS_FOR_MODEL:
        return None

    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score, KFold

    d = _features(daily).dropna(subset=["price"])
    X, y = d[_FEATS].fillna(0), d["price"].astype(float)

    median = GradientBoostingRegressor(loss="quantile", alpha=0.5,
                                       n_estimators=300, max_depth=3,
                                       learning_rate=0.05, random_state=42)
    mae = -cross_val_score(median, X, y,
                           cv=KFold(5, shuffle=True, random_state=42),
                           scoring="neg_mean_absolute_error").mean()

    models = {}
    for q in QUANTILES:
        m = GradientBoostingRegressor(loss="quantile", alpha=q,
                                      n_estimators=300, max_depth=3,
                                      learning_rate=0.05, random_state=42)
        m.fit(X, y)
        models[q] = m
    return {"models": models, "mae": float(mae), "n": int(len(d)), "features": _FEATS}


def _forecast_curve(bundle, latest_row):
    """
    Predict the booking curve forward for one itinerary and return the expected
    future low plus its 10/90 band. We sweep days_to_departure from now down to
    the floor and take the cheapest predicted median fare.
    """
    base = _features(pd.DataFrame([latest_row])).iloc[0]
    dtd_now = int(latest_row["days_to_departure"])
    grid = list(range(dtd_now, FLOOR_DTD - 1, -1)) or [dtd_now]

    rows = []
    for dtd in grid:
        r = {f: base.get(f, 0) for f in _FEATS}
        r["days_to_departure"] = dtd
        rows.append(r)
    X = pd.DataFrame(rows)[_FEATS].fillna(0)

    med = bundle["models"][0.5].predict(X)
    j = int(np.argmin(med))                 # cheapest expected day ahead
    lo = float(bundle["models"][0.1].predict(X.iloc[[j]])[0])
    hi = float(bundle["models"][0.9].predict(X.iloc[[j]])[0])
    return {"predicted_low": float(med[j]),
            "low_band": (min(lo, hi), max(lo, hi)),
            "best_dtd": int(grid[j])}


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
# Decision engine
# --------------------------------------------------------------------------- #
def _heuristic(hist):
    """Interpretable signal from an itinerary's own history. Low-ish confidence."""
    latest = hist.iloc[-1]
    price, dtd = float(latest["price"]), int(latest["days_to_departure"])
    lo, med = float(hist["price"].min()), float(hist["price"].median())
    pct = float((hist["price"] < price).mean() * 100)
    n = len(hist)

    if n < MIN_HISTORY_PER_ITIN:
        return {"signal": "WATCH", "confidence": 25,
                "reason": "Still collecting history for this route.",
                "price": price, "predicted_low": lo, "expected_savings": 0.0,
                "prob_drop": None, "trailing_min": lo, "percentile": round(pct),
                "days_to_departure": dtd}

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
            "days_to_departure": dtd}


def _model_decision(bundle, hist):
    """Forecast-driven decision with a calibrated confidence rate."""
    latest = hist.iloc[-1]
    price, dtd = float(latest["price"]), int(latest["days_to_departure"])
    fc = _forecast_curve(bundle, latest)
    lo_band = fc["low_band"]
    predicted_low = fc["predicted_low"]

    prob_drop = _prob_below(lo_band[0], predicted_low, lo_band[1], price) * 100
    expected_savings = max(0.0, price - predicted_low)
    band_width = max(lo_band[1] - lo_band[0], 1.0)

    # Confidence: tighter band relative to price + more data => more confident.
    tightness = max(0.0, 1.0 - band_width / max(price, 1.0))
    data_factor = min(1.0, bundle["n"] / (MIN_OBS_FOR_MODEL * 3))
    base_conf = 45 + 45 * (0.6 * tightness + 0.4 * data_factor)

    materiality = expected_savings / max(price, 1.0)   # is waiting worth it?
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
            "expected_savings": round(expected_savings if sig == "WAIT" else 0),
            "prob_drop": round(prob_drop),
            "trailing_min": round(float(hist["price"].min())),
            "percentile": round(float((hist["price"] < price).mean() * 100)),
            "days_to_departure": dtd}


def recommendations(df: pd.DataFrame, bundle=None) -> list:
    """
    One recommendation per itinerary. Uses the trained model when available
    (and trustworthy for that route), otherwise the heuristic. `bundle` may be
    passed in to avoid retraining; if omitted and there is enough data, we train.
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
