"""Dashboard data layer: the trip-finder explore grid + filter metadata."""
import json

import flightwatch.dashboard as DB


def test_explore_one_row_per_itinerary(synth):
    df = synth(days=30, seed=3)
    ok = DB._with_itin(df[df["status"] == "ok"])
    explore = DB._explore(ok)
    # One bookable option per distinct itinerary.
    assert len(explore) == ok["itin"].nunique()
    e = explore[0]
    assert set(e).issuperset({"o", "d", "dep", "ret", "len", "min", "airline",
                              "iata", "stops", "nonstop", "fastest", "offers"})
    assert e["min"] > 0 and e["len"] > 0


def test_explore_uses_freshest_scan(synth):
    # Two scan dates: the explore price must come from the latest one.
    df = synth(days=20, seed=4)
    ok = DB._with_itin(df[df["status"] == "ok"])
    explore = DB._explore(ok)
    latest = ok["scan_date"].max()
    for e in explore:
        itin = f'{e["o"]}-{e["d"]} {e["dep"]} -> {e["ret"]}'
        day = ok[(ok["itin"] == itin) & (ok["scan_date"] == latest)]
        if not day.empty:
            assert e["min"] == round(float(day["price"].min()))


def test_explore_meta_bounds_and_serializable(synth):
    df = synth(days=25, seed=5)
    ok = DB._with_itin(df[df["status"] == "ok"])
    explore = DB._explore(ok)
    meta = DB._explore_meta(explore)
    assert meta["count"] == len(explore)
    assert meta["price_min"] <= meta["price_max"]
    assert meta["dep_min"] <= meta["dep_max"]
    assert meta["len_min"] <= meta["len_max"]
    # The whole payload slice must be JSON-serialisable for embedding.
    json.dumps({"explore": explore, "explore_meta": meta})


def test_explore_cap_is_respected(synth):
    df = synth(days=15, seed=6)
    ok = DB._with_itin(df[df["status"] == "ok"])
    assert len(DB._explore(ok, cap=1)) == 1


def test_empty_explore_meta():
    assert DB._explore_meta([]) == {}


def test_clean_airline_rejects_noise():
    assert DB.clean_airline("706 kg CO2e") == ""
    assert DB.clean_airline("751 kg CO2e") == ""
    assert DB.clean_airline("CHC–CMB") == ""
    assert DB.clean_airline("12,345") == ""
    # legitimate carriers (incl. combined) survive
    assert DB.clean_airline("SriLankan") == "SriLankan"
    assert DB.clean_airline("Air New Zealand, Singapore Airlines") == \
        "Air New Zealand + Singapore Airlines"


def test_carriers_split_into_individuals():
    import pandas as pd
    day = pd.DataFrame({"price": [100, 200],
                        "airline": ["Air New Zealand, Singapore Airlines", "SriLankan"]})
    carriers = DB._carriers_in(day)
    # Singapore Airlines is findable even though it only flies a leg of a combo.
    assert {"Air New Zealand", "Singapore Airlines", "SriLankan"} <= set(carriers)


def test_explore_exposes_carrier_list_and_filter_options():
    import pandas as pd
    df = pd.DataFrame([
        dict(scan_datetime="2026-06-20T09:00:00Z", scan_date=pd.Timestamp("2026-06-20"),
             scan_slot="2026-06-20T09:00Z", origin="CHC", destination="CMB",
             depart_date="2026-09-10", return_date="2026-10-01", trip_length=21,
             days_to_departure=80, offer_index=0, price=p, currency="NZD",
             airline=a, stops=1, duration_minutes=900, status="ok", source="x")
        for p, a in [(1500, "Air New Zealand, Singapore Airlines"), (1600, "SriLankan")]
    ])
    ok = DB._with_itin(df[df["status"] == "ok"])
    explore = DB._explore(ok)
    assert "al" in explore[0] and "Singapore Airlines" in explore[0]["al"]
    meta = DB._explore_meta(explore)
    assert "Singapore Airlines" in meta["airlines"]
