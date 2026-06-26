"""Model: honest validation, calibrated bands, sane signals, working backtest."""
import numpy as np
import pandas as pd

import flightwatch.predict as P
from flightwatch.predict import _features, _FEATS


def test_model_trains_and_validates(synth):
    b = P.train_model(synth(days=150, seed=1))
    assert b is not None
    assert np.isfinite(b["mae"]) and b["mae"] > 0
    assert {0.1, 0.5, 0.9}.issubset(b["models"].keys())
    assert b["conformal"] is None or b["conformal"] > 0


def test_conformal_band_covers(synth):
    df = synth(days=170, seed=2)
    b = P.train_model(df)
    if not b["conformal"]:
        return
    d = _features(P.daily_min(df).sort_values("scan_date"),
                  b["route_levels"], b["global_level"])
    cal = d.iloc[int(len(d) * 0.8):]
    pred = b["models"][0.5].predict(cal[_FEATS].fillna(0))
    cov = float(np.mean(np.abs(cal["price"].values - pred) <= b["conformal"]))
    assert cov >= 0.6                      # targets 0.80; allow holdout slack


def test_forecast_curve_shape(synth):
    df = synth(days=150, seed=3)
    b = P.train_model(df)
    daily = P.daily_min(df)
    hist = daily[daily["itin"] == daily["itin"].iloc[0]].sort_values("scan_date")
    fc = P.forecast_curve(b, hist.iloc[-1])
    assert fc["curve"] and all("lo" in c and "p" in c and "hi" in c for c in fc["curve"])
    assert fc["low_band"][0] <= fc["predicted_low"] <= fc["low_band"][1] + 1e-6


def test_recommendations_shape(synth):
    recs = P.recommendations(synth(days=150, seed=4))
    assert recs
    for r in recs:
        assert r["signal"] in ("BUY", "WAIT", "WATCH")
        assert 0 <= r["confidence"] <= 100
        assert "curve" in r and "predicted_low" in r


def test_heuristic_buys_near_min_close_in():
    rows = []
    for i, (dtd, price) in enumerate(zip([70, 60, 50, 40, 30], [1500, 1400, 1300, 1250, 1100])):
        rows.append(dict(scan_date=pd.Timestamp("2026-06-01") + pd.Timedelta(days=i),
                         days_to_departure=dtd, price=float(price)))
    rec = P._heuristic(pd.DataFrame(rows))
    assert rec["signal"] == "BUY"          # latest is the min and inside the window


def test_backtest_runs(synth):
    bt = P.backtest(synth(days=150, seed=5))
    assert bt and bt["n"] > 0 and 0 <= bt["hit_rate"] <= 100
    assert "series" in bt


def test_backtests_by_route_shape(synth):
    # Two corridors so the per-route split is exercised.
    df = synth(itins=[("CHC", "CMB", "2026-09-01", "2026-09-22", 1200, 1),
                      ("AKL", "DEL", "2026-10-03", "2026-10-24", 1600, 1)],
               days=150, seed=7)
    by = P.backtests_by_route(df)
    assert "CHC-CMB" in by and "AKL-DEL" in by
    for route, s in by.items():
        assert set(s).issuperset(
            {"calls", "right", "hit_rate", "saved_vs_searchday", "missed_cost"})
        assert s["calls"] >= 0 and 0 <= s["right"] <= s["calls"]
        # Accuracy is published only once the route has enough graded calls --
        # never a fabricated percentage off a handful of decisions.
        if s["calls"] >= P.MIN_CALLS_FOR_HITRATE:
            assert s["hit_rate"] is None or 0 <= s["hit_rate"] <= 100
        else:
            assert s["hit_rate"] is None
        assert isinstance(s["saved_vs_searchday"], int)
    # The whole section must be JSON-serialisable for embedding in the payload.
    import json
    json.dumps(by)


def test_backtests_by_route_empty():
    assert P.backtests_by_route(pd.DataFrame()) == {}
