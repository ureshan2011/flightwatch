"""
Turns the accumulated price history into a per-itinerary recommendation.

Two modes, chosen automatically by how much data exists:

  HEURISTIC (always available): compares today's price against that itinerary's
  own observed history (min / median / percentile) and the time left before
  departure. Honest and interpretable from day one.

  MODEL (kicks in once there are enough observations): a gradient-boosting
  regressor predicts the expected minimum price over the remaining booking
  window, so "wait" carries an expected-savings estimate.

Both return: signal in {"BUY", "WAIT", "WATCH"}, a short reason, and numbers.
"""

import pandas as pd
import numpy as np

MIN_OBS_FOR_MODEL = 400          # total observations before training the ML model
MIN_HISTORY_PER_ITIN = 4         # daily points before an itinerary gets a signal


def _latest_per_itinerary(df):
    df = df[df["status"] == "ok"].copy()
    if df.empty:
        return df
    df["itin"] = df["origin"] + "-" + df["destination"] + " " + \
                 df["depart_date"].astype(str) + " -> " + df["return_date"].astype(str)
    df = df.sort_values("scan_date")
    return df


def heuristic_signal(hist: pd.DataFrame) -> dict:
    """hist = all observations for ONE itinerary, chronologically sorted."""
    latest = hist.iloc[-1]
    price = latest["price"]
    dtd = int(latest["days_to_departure"])
    lo, med = hist["price"].min(), hist["price"].median()
    pct = (hist["price"] < price).mean() * 100  # where today's price sits in its history

    if len(hist) < MIN_HISTORY_PER_ITIN:
        return {"signal": "WATCH", "reason": "Still collecting history for this route.",
                "price": price, "trailing_min": lo, "percentile": round(pct)}

    near_min = price <= lo * 1.03
    if near_min and dtd <= 75:
        sig, reason = "BUY", "Near its lowest observed price and inside the booking window."
    elif price <= med and dtd <= 90:
        sig, reason = "BUY", "Below its typical price with departure approaching."
    elif dtd > 90 and price >= med:
        sig, reason = "WAIT", "Priced at/above typical and still far out -- history suggests room to fall."
    else:
        sig, reason = "WATCH", "No strong edge either way right now."

    return {"signal": sig, "reason": reason, "price": price,
            "trailing_min": lo, "trailing_median": med, "percentile": round(pct),
            "days_to_departure": dtd}


def recommendations(df: pd.DataFrame) -> list:
    df = _latest_per_itinerary(df)
    if df.empty:
        return []
    out = []
    for itin, hist in df.groupby("itin"):
        rec = heuristic_signal(hist.sort_values("scan_date"))
        rec["itinerary"] = itin
        rec["points"] = len(hist)
        out.append(rec)
    # BUY first, then WATCH, then WAIT; cheapest within group
    order = {"BUY": 0, "WATCH": 1, "WAIT": 2}
    out.sort(key=lambda r: (order.get(r["signal"], 9), r["price"]))
    return out


def train_model(df: pd.DataFrame):
    """
    Optional ML layer. Returns a fitted model + CV error, or None if not enough data.
    Predicts price from booking-curve features; you can extend the target to
    'minimum price over the remaining window' once you have departures that elapsed.
    """
    ok = df[df["status"] == "ok"].dropna(subset=["price"])
    if len(ok) < MIN_OBS_FOR_MODEL:
        return None

    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score, KFold

    ok = ok.copy()
    ok["dep_dow"] = pd.to_datetime(ok["depart_date"]).dt.dayofweek
    ok["ret_dow"] = pd.to_datetime(ok["return_date"]).dt.dayofweek
    feats = ["days_to_departure", "trip_length", "dep_dow", "ret_dow", "stops"]
    X, y = ok[feats].fillna(0), ok["price"]
    model = GradientBoostingRegressor(n_estimators=300, max_depth=3,
                                      learning_rate=0.05, random_state=42)
    mae = -cross_val_score(model, X, y, cv=KFold(5, shuffle=True, random_state=42),
                           scoring="neg_mean_absolute_error").mean()
    model.fit(X, y)
    return {"model": model, "mae": mae, "features": feats, "n": len(ok)}
