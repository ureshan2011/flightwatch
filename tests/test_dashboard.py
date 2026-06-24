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


def test_clean_airline_rejects_price_strings():
    # The scraper sometimes captures a PRICE where a carrier name should be
    # ("NZ$4,005"). Cleaned, its tail ("005") used to leak into the airline filter
    # as a fake carrier -- the dropdown-full-of-numbers bug. Reject these outright.
    for junk in ["NZ$4,005", "NZ$2,565", "USD 1,234", "NZ$ 4005", "$3,961",
                 "005", "4,005", "9,130"]:
        assert DB.clean_airline(junk) == "", junk
    # A bare flight-number-like fragment is not a carrier either.
    assert DB.clean_airline("305") == ""


def test_clean_airline_recognises_india_carriers():
    # New India-corridor carriers must be recognised (with correct logos) and
    # "Air India Express" must not be mis-peeled as "Air India".
    assert DB.clean_airline("Air India") == "Air India"
    assert DB.airline_iata("Air India") == "AI"
    assert DB.clean_airline("Air India Express") == "Air India Express"
    assert DB.airline_iata("Air India Express") == "IX"
    assert DB.clean_airline("IndiGo") == "IndiGo"
    assert DB.airline_iata("IndiGo") == "6E"


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


def _one_day(offers):
    """A single itinerary's offers (price, airline[, stops[, dur]]) as a frame."""
    import pandas as pd
    rows = []
    for o in offers:
        price, air = o[0], o[1]
        st = o[2] if len(o) > 2 else 1
        du = o[3] if len(o) > 3 else 900
        rows.append(dict(
            scan_datetime="2026-06-20T09:00:00Z", scan_date=pd.Timestamp("2026-06-20"),
            scan_slot="2026-06-20T09:00Z", origin="CHC", destination="CMB",
            depart_date="2026-09-10", return_date="2026-10-01", trip_length=21,
            days_to_departure=80, offer_index=0, price=price, currency="NZD",
            airline=air, stops=st, duration_minutes=du, status="ok", source="x"))
    return DB._with_itin(pd.DataFrame(rows))


def test_explore_byair_holds_each_carriers_own_cheapest_fare():
    # The itinerary's overall cheapest is Jetstar @ 1000; Singapore Airlines only
    # flies a connection priced at 1500. Filtering for Singapore Airlines must
    # surface SQ's OWN 1500 fare -- never Jetstar's 1000 (the original bug).
    ok = _one_day([(1000, "Jetstar", 0), (1500, "Air New Zealand, Singapore Airlines", 1)])
    e = DB._explore(ok)[0]
    assert e["min"] == 1000                              # headline = true cheapest
    assert "Jetstar" in e["airline"]
    sq = e["byair"]["Singapore Airlines"]
    assert sq["min"] == 1500                             # NOT the 1000 Jetstar fare
    assert sq["label"] == "Air New Zealand + Singapore Airlines"
    assert sq["iata"] == "SQ"                            # SQ's own logo, not Jetstar's
    assert e["byair"]["Jetstar"]["min"] == 1000
    # Nonstop flag is per-carrier: only Jetstar is nonstop here.
    assert e["byair"]["Jetstar"]["ns"] is True
    assert sq["ns"] is False


def test_explore_blank_cheapest_airline_falls_back_to_named_carrier():
    # The cheapest offer's "airline" is scraper noise that cleans to "" -- the
    # headline must fall back to a real carrier, never render blank.
    ok = _one_day([(900, "706 kg CO2e"), (1100, "SriLankan")])
    e = DB._explore(ok)[0]
    assert e["min"] == 900                               # overall cheapest preserved
    assert e["airline"] == "SriLankan" and e["iata"] == "UL"
    assert "" not in e["byair"]                          # noise never becomes a carrier


def test_carrier_focus_spotlights_the_selected_airline():
    import pandas as pd
    # Two departure months; on each, Singapore Airlines flies (combined with Air
    # NZ) and Jetstar undercuts it. The spotlight must report SQ's OWN fare.
    data = [("2026-07-10", "2026-07-31", 2400, "Air New Zealand, Singapore Airlines"),
            ("2026-07-10", "2026-07-31", 1200, "Jetstar"),
            ("2026-08-05", "2026-08-26", 2600, "Singapore Airlines, Air New Zealand"),
            ("2026-08-05", "2026-08-26", 1300, "Jetstar")]
    rows = [dict(scan_datetime="2026-06-20T09:00:00Z", scan_date=pd.Timestamp("2026-06-20"),
                 scan_slot="2026-06-20T09:00Z", origin="CHC", destination="CMB",
                 depart_date=dep, return_date=ret, trip_length=21, days_to_departure=40,
                 offer_index=0, price=p, currency="NZD", airline=a, stops=1,
                 duration_minutes=900, status="ok", source="x")
            for dep, ret, p, a in data]
    df = pd.DataFrame(rows)
    explore = DB._explore(DB._with_itin(df[df["status"] == "ok"]))
    focus = DB._carrier_focus(df, explore, "Singapore Airlines")

    assert focus["name"] == "Singapore Airlines" and focus["iata"] == "SQ"
    assert focus["trips"] == 2
    # Headline is SQ's own cheapest (2400) -- NOT the 1200 Jetstar fare alongside.
    assert focus["cheapest"]["price"] == 2400
    assert "Singapore Airlines" in focus["cheapest"]["label"]
    assert focus["cheapest"]["signal"] in {"BUY", "WAIT", "WATCH"}
    # One low per upcoming departure month, cheapest-SQ per month.
    assert [u["month"] for u in focus["upcoming"]] == ["2026-07", "2026-08"]
    assert focus["upcoming"][0]["price"] == 2400


def test_byair_tracks_whole_trip_solo_operation():
    # SQ flies a connection (with Air NZ) @ 2400 AND the whole trip solo @ 2700.
    # byair must record SQ's solo fare separately from its overall (connection) low.
    ok = _one_day([(1200, "Jetstar", 0),
                   (2400, "Air New Zealand, Singapore Airlines", 1),
                   (2700, "Singapore Airlines", 1)])
    e = DB._explore(ok)[0]
    sq = e["byair"]["Singapore Airlines"]
    assert sq["min"] == 2400            # cheapest SQ-operated (a connection)
    assert sq["solo"] is True           # SQ also flies the whole trip on its metal
    assert sq["solo_min"] == 2700       # ...at this fare
    # Air NZ here only ever flies a leg -> no solo whole-trip fare.
    assert e["byair"]["Air New Zealand"]["solo"] is False
    assert e["byair"]["Air New Zealand"]["solo_min"] is None


def test_carrier_focus_surfaces_whole_trip_fare():
    import pandas as pd
    data = [("2026-09-05", "2026-09-26", 2400, "Air New Zealand, Singapore Airlines"),
            ("2026-09-05", "2026-09-26", 2900, "Singapore Airlines"),
            ("2026-10-03", "2026-10-24", 2600, "Singapore Airlines")]
    rows = [dict(scan_datetime="2026-06-20T09:00:00Z", scan_date=pd.Timestamp("2026-06-20"),
                 scan_slot="2026-06-20T09:00Z", origin="AKL", destination="CMB",
                 depart_date=dep, return_date=ret, trip_length=21, days_to_departure=80,
                 offer_index=0, price=p, currency="NZD", airline=a, stops=1,
                 duration_minutes=900, status="ok", source="x")
            for dep, ret, p, a in data]
    df = pd.DataFrame(rows)
    explore = DB._explore(DB._with_itin(df[df["status"] == "ok"]))
    focus = DB._carrier_focus(df, explore, "Singapore Airlines")
    assert focus["whole_trip"] is not None
    # Cheapest SQ-only whole-trip fare across the grid is 2600 (the Oct departure).
    assert focus["whole_trip"]["price"] == 2600
    assert focus["whole_trip"]["dep"] == "2026-10-03"


def test_clean_layover_keeps_only_airport_codes():
    assert DB._clean_layover("SIN") == "SIN"
    assert DB._clean_layover("sin, kul") == "SIN, KUL"
    assert DB._clean_layover("SIN, SIN") == "SIN"          # de-duped
    assert DB._clean_layover("706 kg") == ""               # noise rejected
    assert DB._clean_layover("") == ""
    import numpy as np
    assert DB._clean_layover(np.nan) == ""                 # legacy rows (NaN)


def test_explore_row_carries_layover_via():
    import pandas as pd
    rows = [dict(scan_datetime="2026-06-20T09:00:00Z", scan_date=pd.Timestamp("2026-06-20"),
                 scan_slot="2026-06-20T09:00Z", origin="CHC", destination="CMB",
                 depart_date="2026-09-10", return_date="2026-10-01", trip_length=21,
                 days_to_departure=80, offer_index=0, price=2310, currency="NZD",
                 airline="Air New Zealand, Singapore Airlines", stops=1,
                 duration_minutes=1155, layover="SIN", status="ok", source="x")]
    ok = DB._with_itin(pd.DataFrame(rows))
    e = DB._explore(ok)[0]
    assert e["via"] == "SIN"
    assert e["byair"]["Singapore Airlines"]["via"] == "SIN"


def test_routes_overview_shows_configured_routes_even_without_data(monkeypatch):
    import pandas as pd
    # Pretend config tracks two corridors; only one has scraped data.
    monkeypatch.setattr(DB, "_configured_routes",
                        lambda: [("CHC", "CMB"), ("AKL", "DEL")])
    rows = [dict(scan_datetime="2026-06-20T09:00:00Z", scan_date=pd.Timestamp("2026-06-20"),
                 scan_slot="2026-06-20T09:00Z", origin="CHC", destination="CMB",
                 depart_date="2026-09-10", return_date="2026-10-01", trip_length=21,
                 days_to_departure=80, offer_index=0, price=1400, currency="NZD",
                 airline="Jetstar", stops=0, duration_minutes=900, layover="",
                 status="ok", source="x")]
    ok = DB._with_itin(pd.DataFrame(rows))
    ro = DB._routes_overview(ok)
    by = {c["route"]: c for c in ro}
    assert by["CHC-CMB"]["has_data"] is True and by["CHC-CMB"]["min"] == 1400
    # The route we track but haven't scraped still appears -- as 'collecting'.
    assert by["AKL-DEL"]["has_data"] is False
    assert by["AKL-DEL"]["from"] == "Auckland" and by["AKL-DEL"]["to"] == "Delhi"


def test_carrier_focus_none_when_carrier_has_no_fares():
    import pandas as pd
    explore = DB._explore(_one_day([(1000, "Jetstar", 0)]))
    assert DB._carrier_focus(pd.DataFrame(), explore, "Singapore Airlines") is None
