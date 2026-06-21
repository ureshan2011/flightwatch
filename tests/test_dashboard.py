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
