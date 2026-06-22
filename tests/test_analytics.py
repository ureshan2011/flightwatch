"""Analytics: deal scores, anomaly detection, heatmap, digest, narratives."""
import flightwatch.analytics as A
import flightwatch.predict as P


def test_deal_scores_bounded(synth):
    ds = A.deal_scores(synth(days=120, seed=5))
    assert ds
    for v in ds.values():
        assert 0 <= v["score"] <= 100
        assert v["label"] in ("Great", "Good", "Fair", "High")


def test_anomaly_detects_forced_drop(synth):
    df = synth(days=40, seed=6)
    df.loc[df["scan_date"] == df["scan_date"].max(), "price"] = 500.0
    drops = [a for a in A.anomalies(df) if a["kind"] == "drop"]
    assert drops and drops[0]["pct"] < 0


def test_cheapest_day_and_airline_intel(synth):
    df = synth(days=12, seed=7)
    cd = A.cheapest_day(df)
    assert cd["best_dep"] and isinstance(cd["date_pairs"], list)
    intel = A.airline_intel(df)
    assert intel and "cheapest_airline" in intel[0]


def test_what_changed_sees_intraday_move(synth):
    wc = A.what_changed(synth(days=20, seed=8, slots=2))
    assert "movers" in wc and isinstance(wc["movers"], list)


def test_narrative_is_text(synth):
    df = synth(days=120, seed=9)
    recs = P.recommendations(df)
    deals = A.deal_scores(df)
    text = A.narrative(recs[0], deals.get(recs[0]["itinerary"]))
    assert isinstance(text, str) and "confidence" in text.lower()


def test_build_payload_serializable(synth):
    import json
    df = synth(days=130, seed=10)
    recs = P.recommendations(df)
    payload = A.build(df, recs)
    json.dumps(payload, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    assert set(payload).issuperset(
        {"deals", "anomalies", "cheapest_day", "best_time_to_book",
         "what_changed", "airline_intel", "narratives", "backtest", "market"})


def _grid(make_df, days=6, seed=11):
    """A multi-departure x multi-length grid so the market widgets have a real
    cross-section to aggregate (advance curve, trip-length value, distribution)."""
    from datetime import date, timedelta
    base = date(2026, 9, 1)
    itins = []
    for i in range(12):
        dep = base + timedelta(days=i * 2)
        for ln in (20, 25, 30):
            ret = dep + timedelta(days=ln)
            itins.append(("CHC", "CMB", dep.isoformat(), ret.isoformat(), 1200 + ln * 10, 1))
    return make_df(itins=itins, days=days, seed=seed)


def test_market_analytics_shapes_and_serializable(synth):
    import json
    df = _grid(synth)
    m = A.market_analytics(df, P.recommendations(df))
    assert set(m).issuperset(
        {"pulse", "advance_curve", "length_curve", "price_distribution", "savings"})
    p = m["pulse"]
    assert 0 <= p["score"] <= 100 and p["label"]
    assert p["buy"] + p["wait"] + p["watch"] >= 1
    ac = m["advance_curve"]
    assert ac and len(ac["points"]) >= 3 and ac["save_vs_worst"] >= 0
    lc = m["length_curve"]
    assert lc and lc["best_len"] in {20, 25, 30}
    pd_ = m["price_distribution"]
    assert pd_ and pd_["min"] <= pd_["median"] <= pd_["max"]
    assert isinstance(m["savings"], list)
    json.dumps(m, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def test_market_pulse_momentum_matches_on_common_itins(synth):
    # A clean intraday/day-over-day drop on the SAME itineraries should read as
    # negative momentum (good for buyers), not be confused by grid composition.
    df = _grid(synth, days=8, seed=12)
    last_two = sorted(df["scan_date"].unique())[-2:]
    df.loc[df["scan_date"] == last_two[-1], "price"] *= 0.9
    p = A.market_pulse(df, P.recommendations(df))
    assert p["momentum_pct"] is not None and p["momentum_pct"] < 0
