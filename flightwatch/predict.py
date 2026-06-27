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
  * Seasonality-aware AND market-aware. Features include cyclical day-of-week /
    month / week-of-year and booking-day encodings; PER-ROUTE peak-season flags
    (each route's NZ-origin seasons unioned with its own destination's festivals,
    kept separate, not one blurred global set); a hierarchical (route x departure-
    month) price level so thin nearby dates borrow strength from each other;
    live competition/supply (carriers, offers, nonstop availability + premium);
    and offline exogenous hooks (jet-fuel index, destination-currency FX) that are
    neutral until a data file is present. So the model learns the shape of the
    booking curve and the market around it, not just a flat average.
  * A direct "will it drop?" classifier estimates P(the fare falls meaningfully
    before departure) instead of only inferring it from the quantile spread.
  * Every recommendation exposes its reasoning: the full predicted forward curve,
    the expected future low, expected savings from waiting, the probability the
    fare drops, and a 0-100% confidence combining data sufficiency with signal
    strength.

NB: the traveller always departs New Zealand and pays in NZD, so the booking
currency is fixed; what varies by route is the foreign end, which is why FX and
the event calendar are keyed on each route's DESTINATION (see `exogenous`).

The raw CSV stores every offer; here we collapse to one cheapest fare per
itinerary per day (what a traveller would actually pay) before modelling.
"""

import numpy as np
import pandas as pd

from . import exogenous

# Total cheapest-per-day observations before the ML model is trusted over the
# heuristic. Below this we still show a heuristic signal, flagged low-confidence.
MIN_OBS_FOR_MODEL = 120
MIN_HISTORY_PER_ITIN = 4         # daily points before an itinerary gets a firm signal
QUANTILES = (0.1, 0.5, 0.9)
FLOOR_DTD = 7                    # we never advise waiting past ~a week out
CONFORMAL_COVERAGE = 0.80        # target coverage of the calibrated price band

# Meaningful-drop threshold for the direct "will it drop?" classifier and the
# decision layer: a fall of more than this fraction is what counts as worth
# waiting for (smaller moves are noise a traveller shouldn't chase).
DROP_TOL = 0.04
MIN_OBS_FOR_CLF = 80             # labelled decision-points before we trust the classifier

# Coarse GLOBAL fallback demand months (NZ summer/festive, Apr, NZ winter school
# holidays). Kept for back-compat and as a default when a route has no mapped
# destination; the per-route calendar in `exogenous` is the sharper signal and is
# what the `route_peak` feature uses.
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
    what a traveller pays. Adds `itin` (full date-pair id) and `route` (O-D), plus
    that day's COMPETITION/SUPPLY snapshot (how many carriers and offers were on
    sale, whether a nonstop existed and its premium) -- computed BEFORE we collapse
    to the cheapest, since the collapse would otherwise throw that context away.
    """
    df = df[df["status"] == "ok"].copy()
    if df.empty:
        return df
    df["route"] = df["origin"] + "-" + df["destination"]
    df["itin"] = df["route"] + " " + df["depart_date"].astype(str) + \
                 " -> " + df["return_date"].astype(str)
    comp = _competition(df)
    daily = df.sort_values("price").drop_duplicates(["itin", "scan_date"], keep="first")
    if not comp.empty:
        daily = daily.merge(comp, on=["itin", "scan_date"], how="left")
    return daily.sort_values("scan_date")


def _competition(df):
    """Per (itinerary, scan_date) market-structure snapshot from the full offer
    list: number of distinct carriers + offers, nonstop availability and the
    nonstop premium (cheapest nonstop minus the cheapest fare overall). These are
    a real, model-relevant driver of price (more competition / more seats on sale
    tends to mean softer fares) that the per-day cheapest point alone can't see.
    """
    d = df.copy()
    d["price"] = pd.to_numeric(d["price"], errors="coerce")
    d["stops"] = pd.to_numeric(d.get("stops", 0), errors="coerce").fillna(0)
    d["_air"] = d.get("airline", "").astype(str).str.strip()
    base = d.groupby(["itin", "scan_date"]).agg(
        n_offers=("price", "size"), overall_min=("price", "min")).reset_index()
    carriers = (d[d["_air"] != ""].groupby(["itin", "scan_date"])["_air"]
                .nunique().rename("n_carriers").reset_index())
    nonstop = (d[d["stops"] == 0].groupby(["itin", "scan_date"])["price"]
               .min().rename("nonstop_min").reset_index())
    out = base.merge(carriers, on=["itin", "scan_date"], how="left") \
              .merge(nonstop, on=["itin", "scan_date"], how="left")
    out["n_carriers"] = out["n_carriers"].fillna(1).astype(int)
    out["nonstop_avail"] = out["nonstop_min"].notna().astype(int)
    out["nonstop_premium"] = (out["nonstop_min"] - out["overall_min"]).clip(lower=0).fillna(0.0)
    return out[["itin", "scan_date", "n_offers", "n_carriers",
                "nonstop_avail", "nonstop_premium"]]


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
_FEATS = [
    "days_to_departure", "lead_bucket", "trip_length",
    "dep_dow_sin", "dep_dow_cos", "ret_dow_sin", "ret_dow_cos",
    "scan_dow_sin", "scan_dow_cos",
    "mon_sin", "mon_cos", "woy_sin", "woy_cos",
    "is_peak", "route_peak",
    "stops", "route_level", "route_dep_level",
    "n_carriers", "nonstop_avail", "nonstop_premium", "log_offers",
    "fuel_z", "fx_z",
]


def _features(df, route_levels=None, global_level=None, route_dep_levels=None):
    """Engineer the model's structural features (no current-price leakage).

    Beyond the calendar/lead-time shape, this layer adds, per ROUTE separately:
      * `route_peak`  -- demand months for THIS route's NZ-origin + destination
        festivals/holidays (e.g. Sri Lankan New Year for CHC->CMB, Diwali for
        AKL->DEL), not one blurred global set;
      * `route_dep_level` -- a date-neighbourhood price level (route x departure
        month) shrunk toward the route's overall level, so thin nearby dates
        borrow strength from each other (the hierarchical-pooling feature);
      * competition/supply (`n_carriers`, `nonstop_avail`, `nonstop_premium`,
        `log_offers`); and
      * exogenous `fuel_z` / `fx_z` -- jet-fuel and the destination-currency-per-
        NZD level, as of the pricing day (neutral 0 until a data file is present).
    The traveller always departs NZ and pays in NZD, so FX is keyed on the route's
    DESTINATION currency, never the booking currency.
    """
    df = df.copy()
    dep = pd.to_datetime(df["depart_date"], errors="coerce")
    ret = pd.to_datetime(df["return_date"], errors="coerce")
    dep_dow, ret_dow = dep.dt.dayofweek, ret.dt.dayofweek
    mon = dep.dt.month
    mon_i = mon.fillna(0).astype(int)
    woy = dep.dt.isocalendar().week.astype(float)

    dtd = pd.to_numeric(df["days_to_departure"], errors="coerce").fillna(0).astype(float)
    # The pricing day = departure minus days-to-departure. Derived (not the raw
    # scan_date column) so it's identical when training and when sweeping the
    # forward curve, and it captures any "book on a weekend" effect.
    scan = dep - pd.to_timedelta(dtd, unit="D")
    scan_dow = scan.dt.dayofweek.fillna(0)

    df["dep_dow_sin"] = np.sin(2 * np.pi * dep_dow / 7)
    df["dep_dow_cos"] = np.cos(2 * np.pi * dep_dow / 7)
    df["ret_dow_sin"] = np.sin(2 * np.pi * ret_dow / 7)
    df["ret_dow_cos"] = np.cos(2 * np.pi * ret_dow / 7)
    df["scan_dow_sin"] = np.sin(2 * np.pi * scan_dow / 7)
    df["scan_dow_cos"] = np.cos(2 * np.pi * scan_dow / 7)
    df["mon_sin"] = np.sin(2 * np.pi * mon / 12)
    df["mon_cos"] = np.cos(2 * np.pi * mon / 12)
    df["woy_sin"] = np.sin(2 * np.pi * woy / 52)
    df["woy_cos"] = np.cos(2 * np.pi * woy / 52)
    df["is_peak"] = mon.isin(PEAK_MONTHS).astype(int)

    df["lead_bucket"] = np.digitize(dtd, _LEAD_EDGES).astype(int)

    if "route" not in df.columns:
        df["route"] = df["origin"].astype(str) + "-" + df["destination"].astype(str)
    routes = df["route"].astype(str)

    # Route-specific peak months (NZ origin season U destination season).
    peak_map = exogenous.route_peak_map(routes.unique())
    df["route_peak"] = [int(m in peak_map.get(r, set()))
                        for r, m in zip(routes, mon_i)]

    gl = global_level
    if gl is None and route_levels:
        gl = float(np.median(list(route_levels.values())))
    if gl is None:
        gl = 0.0
    if route_levels:
        df["route_level"] = routes.map(route_levels).fillna(gl)
    else:
        df["route_level"] = gl

    # Hierarchical (route x departure-month) level, falling back to the route
    # level wherever a neighbourhood wasn't seen in training.
    if route_dep_levels:
        df["route_dep_level"] = [route_dep_levels.get((r, m), np.nan)
                                 for r, m in zip(routes, mon_i)]
        df["route_dep_level"] = pd.to_numeric(df["route_dep_level"], errors="coerce") \
            .fillna(df["route_level"])
    else:
        df["route_dep_level"] = df["route_level"]

    # Competition / supply -- default to a lone-carrier, nonstop-less snapshot when
    # the frame predates these columns (e.g. a bare forecast row).
    df["n_carriers"] = pd.to_numeric(df.get("n_carriers", 1), errors="coerce").fillna(1)
    df["nonstop_avail"] = pd.to_numeric(df.get("nonstop_avail", 0), errors="coerce").fillna(0)
    df["nonstop_premium"] = pd.to_numeric(df.get("nonstop_premium", 0), errors="coerce").fillna(0.0)
    df["log_offers"] = np.log1p(pd.to_numeric(df.get("n_offers", 0), errors="coerce").fillna(0))

    # Exogenous signals as of the pricing day (neutral 0 unless a CSV is present).
    scan_arr = scan.to_numpy()
    df["fuel_z"] = exogenous.fuel_z(scan_arr)
    row_cur = routes.map(lambda r: exogenous.dest_currency(*r.split("-", 1))
                         if "-" in r else "")
    fx = np.zeros(len(df), dtype=float)
    for cur in pd.unique(row_cur.dropna()):
        if not cur:
            continue
        m = (row_cur == cur).to_numpy()
        fx[m] = exogenous.fx_z(cur, scan_arr[m])
    df["fx_z"] = fx

    df["stops"] = pd.to_numeric(df.get("stops", 0), errors="coerce").fillna(0)
    return df


def _route_dep_levels(daily, route_levels, global_level, k=8):
    """Empirical-Bayes (route x departure-month) median price, each shrunk toward
    its route level by a pseudo-count `k`. This is the hierarchical-pooling step:
    a thinly observed departure month borrows from the whole route instead of
    overfitting its handful of points.
    """
    d = daily.copy()
    d["_m"] = pd.to_datetime(d["depart_date"], errors="coerce").dt.month
    d = d.dropna(subset=["_m"])
    out = {}
    for (route, m), g in d.groupby(["route", "_m"]):
        n = len(g)
        grp_med = float(g["price"].astype(float).median())
        base = float(route_levels.get(route, global_level))
        out[(route, int(m))] = (n * grp_med + k * base) / (n + k)
    return out


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


def _gbc():
    """Gradient-boosting classifier for the direct 'will it drop?' target."""
    from sklearn.ensemble import GradientBoostingClassifier
    return GradientBoostingClassifier(n_estimators=200, max_depth=3,
                                      learning_rate=0.05, min_samples_leaf=10,
                                      subsample=0.9, random_state=42)


def train_model(df: pd.DataFrame):
    """
    Fit gradient-boosting models over the booking curve and report honest,
    time-series cross-validated error plus calibrated prediction bands.

    On top of the median + quantile regressors it now also fits:
      * a hierarchical (route x departure-month) price level used as a feature;
      * a PER-ROUTE split-conformal band, with the global band as the fallback,
        and an automatic choice between the conformal band and the quantile band
        based on which actually achieves the target coverage on held-out data;
      * a direct "fare drops > DROP_TOL before departure" classifier, so the
        decision layer can read a real probability instead of inferring one from
        the quantile spread.

    Returns a bundle dict, or None if there is not yet enough history.
    """
    daily = daily_min(df)
    if len(daily) < MIN_OBS_FOR_MODEL:
        return None

    daily = daily.sort_values("scan_date")
    route_levels = daily.groupby("route")["price"].median().to_dict()
    global_level = float(daily["price"].median())
    route_dep_levels = _route_dep_levels(daily, route_levels, global_level)

    d = _features(daily, route_levels, global_level, route_dep_levels) \
        .dropna(subset=["price"])
    X = d[_FEATS].fillna(0)
    y = d["price"].astype(float)
    routes = d["route"].astype(str)

    # Honest error: forward-chaining CV of the median model (no future leakage).
    mae = _timeseries_mae(X, y)

    # Calibration: per-route conformal half-widths + a global fallback, and a
    # data-driven choice of which band (conformal vs quantile) to actually use.
    calib = _calibrate(X, y, routes, CONFORMAL_COVERAGE)

    median = _gbr(0.5)
    median.fit(X, y)
    models = {0.5: median}
    for q in (0.1, 0.9):
        m = _gbr(q)
        m.fit(X, y)
        models[q] = m

    drop_clf = _train_drop_classifier(daily, route_levels, global_level,
                                       route_dep_levels)

    return {"models": models, "mae": float(mae), "n": int(len(d)),
            "features": _FEATS, "route_levels": route_levels,
            "global_level": global_level, "route_dep_levels": route_dep_levels,
            "conformal": (calib["global"] if calib else None),
            "route_conformal": (calib["by_route"] if calib else {}),
            "band_method": (calib["method"] if calib else "conformal"),
            "empirical_coverage": (calib["coverage"] if calib else None),
            "drop_clf": drop_clf, "drop_tol": DROP_TOL,
            "coverage": CONFORMAL_COVERAGE,
            "exogenous": exogenous.available()}


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
    """Split-conformal half-width: the `coverage` quantile of held-out |resid|.

    Kept as a thin wrapper over `_calibrate` for callers/tests that just want the
    one global number.
    """
    calib = _calibrate(X, y, None, coverage)
    return calib["global"] if calib else None


def _calibrate(X, y, routes, coverage):
    """Split calibration: fit on the first 70% by time, then on the last 30%
    measure (a) per-route + global conformal half-widths and (b) whether the
    conformal band or the quantile band better hits the target coverage.

    Returns {global, by_route, method, coverage} or None if too little holdout.
    """
    n = len(X)
    cut = int(n * 0.7)
    if cut < 20 or n - cut < 10:
        return None
    Xtr, ytr = X.iloc[:cut], y.iloc[:cut]
    Xte, yte = X.iloc[cut:], y.iloc[cut:].to_numpy()

    med = _gbr(0.5)
    med.fit(Xtr, ytr)
    resid = np.abs(yte - med.predict(Xte))
    g_width = float(np.quantile(resid, coverage))

    by_route = {}
    if routes is not None:
        rte = routes.iloc[cut:].to_numpy()
        for r in pd.unique(rte):
            m = rte == r
            if m.sum() >= 8:
                by_route[str(r)] = float(np.quantile(resid[m], coverage))

    conf_cov = float(np.mean(resid <= g_width))
    # Quantile-band empirical coverage on the same holdout.
    lo_m, hi_m = _gbr(0.1), _gbr(0.9)
    lo_m.fit(Xtr, ytr)
    hi_m.fit(Xtr, ytr)
    lo_p, hi_p = lo_m.predict(Xte), hi_m.predict(Xte)
    q_cov = float(np.mean((yte >= np.minimum(lo_p, hi_p)) &
                          (yte <= np.maximum(lo_p, hi_p))))

    method = ("conformal" if abs(conf_cov - coverage) <= abs(q_cov - coverage)
              else "quantile")
    return {"global": g_width, "by_route": by_route, "method": method,
            "coverage": (conf_cov if method == "conformal" else q_cov)}


def _train_drop_classifier(daily, route_levels, global_level, route_dep_levels):
    """Direct P(fare drops > DROP_TOL before departure) model.

    Walk every itinerary forward; at each day with a future, label whether the
    fare went on to fall by more than DROP_TOL, and pair that with the same
    structural features the regressor sees. Returns a fitted classifier, or None
    if there aren't yet enough labelled points (or only one class).
    """
    feats, labels = [], []
    for _, h in daily.groupby("itin"):
        h = h.sort_values("scan_date").reset_index(drop=True)
        prices = h["price"].astype(float).to_numpy()
        for i in range(len(h) - 1):
            future = prices[i + 1:]
            if future.size == 0:
                continue
            cur = float(prices[i])
            drop = (cur - float(future.min())) / max(cur, 1.0)
            feats.append(h.iloc[i])
            labels.append(int(drop > DROP_TOL))
    if len(labels) < MIN_OBS_FOR_CLF or len(set(labels)) < 2:
        return None
    fdf = pd.DataFrame(feats)
    d = _features(fdf, route_levels, global_level, route_dep_levels)
    X = d[_FEATS].fillna(0)
    try:
        clf = _gbc()
        clf.fit(X, np.asarray(labels))
        return clf
    except Exception:
        return None


def _drop_probability(bundle, latest_row):
    """Classifier P(meaningful drop) for one row, as a 0-100 number, or None."""
    clf = bundle.get("drop_clf")
    if clf is None:
        return None
    d = _features(pd.DataFrame([dict(latest_row)]), bundle["route_levels"],
                  bundle["global_level"], bundle.get("route_dep_levels"))
    try:
        return float(clf.predict_proba(d[_FEATS].fillna(0))[0, 1]) * 100
    except Exception:
        return None


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
    d = _features(pd.DataFrame(rows), bundle["route_levels"], bundle["global_level"],
                  bundle.get("route_dep_levels"))
    X = d[_FEATS].fillna(0)

    med = bundle["models"][0.5].predict(X)
    method = bundle.get("band_method", "conformal")
    if method == "quantile" and 0.1 in bundle["models"] and 0.9 in bundle["models"]:
        lo_arr = bundle["models"][0.1].predict(X)
        hi_arr = bundle["models"][0.9].predict(X)
    else:
        # Per-route conformal half-width, falling back to the global one, then to
        # the quantile band if calibration produced no width at all.
        route = str(base.get("route", ""))
        cw = (bundle.get("route_conformal", {}).get(route)
              if bundle.get("route_conformal") else None)
        if cw is None:
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

    # Prefer the direct "will it drop?" classifier; fall back to inferring the
    # probability from the forecast band's implied CDF when it isn't trained yet.
    prob_drop = _drop_probability(bundle, hist.iloc[-1])
    prob_source = "classifier"
    if prob_drop is None:
        prob_drop = _prob_below(lo_band[0], predicted_low, lo_band[1], price) * 100
        prob_source = "band"
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
            "prob_drop": round(prob_drop), "prob_source": prob_source,
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


# Routes thinner than this many graded calls don't get a headline accuracy figure
# -- we refuse to publish an honest-looking percentage off a handful of decisions.
MIN_CALLS_FOR_HITRATE = 8


def backtests_by_route(df: pd.DataFrame, tolerance=0.03):
    """Per-corridor walk-forward backtest summary for the Lab scorecard.

    Same forward-chaining grading as `backtest()` (replay the heuristic at each
    day using only data available then, grade it against what actually happened),
    but aggregated per route and reporting, for each corridor::

        {calls, right, hit_rate, saved_vs_searchday, missed_cost}

    * ``calls``/``right`` -- graded BUY/WAIT/WATCH decision-points and how many
      paid off; ``hit_rate`` is right/calls as a percent, or ``None`` when the
      route is too thin (< ``MIN_CALLS_FOR_HITRATE`` calls) to headline honestly.
    * ``saved_vs_searchday`` -- summed over every tracked itinerary: the price you
      would have paid booking on the day you first searched (the itinerary's first
      observation) minus the price you'd pay following Faro (book at its first BUY,
      otherwise forced to book at the latest fare we have). Negative contributions
      (where waiting cost money) are kept, so the figure is honest, not cherry-
      picked.
    * ``missed_cost`` -- summed regret on BUY calls that turned out wrong (the fare
      later dropped further), counted against the total.

    Returns ``{route: summary}``. An itinerary with too little history is simply
    skipped; a route only appears once it has at least one graded call.
    """
    if df is None or df.empty or "status" not in df.columns:
        return {}
    daily = daily_min(df)
    if daily.empty:
        return {}

    acc = {}  # route -> running tallies

    for itin, h in daily.groupby("itin"):
        route = str(itin).split(" ", 1)[0]            # "CHC-CMB 2026-.. -> .." -> "CHC-CMB"
        h = h.sort_values("scan_date").reset_index(drop=True)
        prices = h["price"].astype(float).to_numpy()
        if prices.size == 0:
            continue
        a = acc.setdefault(route, {"calls": 0, "right": 0, "missed": 0.0,
                                   "saved": 0.0, "itins": 0})

        first_buy_price = None
        first_graded_price = None
        for i in range(MIN_HISTORY_PER_ITIN, len(h)):
            future = prices[i + 1:]
            if future.size == 0:
                continue
            rec = _heuristic(h.iloc[:i + 1])
            cur, fut_min = float(prices[i]), float(future.min())
            drop = (cur - fut_min) / max(cur, 1.0)
            sig = rec["signal"]
            if first_graded_price is None:
                first_graded_price = cur
            if sig == "BUY":
                correct = drop <= tolerance
                if drop > tolerance:
                    a["missed"] += max(0.0, cur - fut_min)
                if first_buy_price is None:
                    first_buy_price = cur
            elif sig == "WAIT":
                correct = drop > tolerance
            else:
                correct = abs(drop) <= tolerance
            a["calls"] += 1
            a["right"] += int(bool(correct))

        # Money saved by following Faro vs booking the day Faro first had an opinion
        # on this itinerary -- only counted for itineraries Faro actually reasoned
        # about (>= 1 graded call), so the figure isn't swamped by the long tail of
        # barely-observed dates. Faro books at its first BUY; if it never said BUY
        # you're forced to book at the latest fare (which may have saved or cost you,
        # both counted honestly).
        if first_graded_price is not None:
            faro_price = (first_buy_price if first_buy_price is not None
                          else float(prices[-1]))
            a["saved"] += first_graded_price - faro_price
            a["itins"] += 1

    out = {}
    for route, a in acc.items():
        calls = a["calls"]
        if calls == 0 and a["itins"] == 0:
            continue
        out[route] = {
            "calls": calls,
            "right": a["right"],
            "hit_rate": (round(100 * a["right"] / calls)
                         if calls >= MIN_CALLS_FOR_HITRATE else None),
            "saved_vs_searchday": round(a["saved"]),
            "missed_cost": round(a["missed"]),
            "itineraries": a["itins"],
        }
    return out
