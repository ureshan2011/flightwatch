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
         "what_changed", "airline_intel", "narratives", "backtest"})
