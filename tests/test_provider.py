"""Provider: pure offer-normalisation logic (no browser / network needed)."""
import flightwatch.provider as PV


def test_normalize_dedupes_sorts_and_caps():
    raw = [
        {"price": 300, "airline": "A", "stops": 1, "duration_minutes": 900},
        {"price": 300, "airline": "A", "stops": 1, "duration_minutes": 900},  # dup
        {"price": 100, "airline": "B", "stops": 0, "duration_minutes": 800},
        {"price": 0, "airline": "C", "stops": 0, "duration_minutes": 0},       # junk price
    ]
    out = PV._normalize_offers(raw, "NZD", 50)
    assert [o["price"] for o in out] == [100, 300]      # sorted, deduped, >0 only
    assert all(o["currency"] == "NZD" for o in out)


def test_cap_preserves_each_airlines_cheapest():
    # 50 cheap fares all on one carrier, plus a pricey fare on another carrier.
    raw = [{"price": 100 + i, "airline": "Combo", "stops": 1, "duration_minutes": 900}
           for i in range(50)]
    raw.append({"price": 9999, "airline": "Singapore Airlines",
                "stops": 1, "duration_minutes": 900})
    out = PV._normalize_offers(raw, "NZD", 50)
    # The cap keeps the cheapest 50, but the otherwise-excluded carrier is retained
    # so premium end-to-end carriers (e.g. a full Singapore Airlines routing) show up.
    assert any(o["airline"] == "Singapore Airlines" for o in out)
