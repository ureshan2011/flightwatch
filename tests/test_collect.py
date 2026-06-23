"""Collector grid generation: per-route overrides keep a multi-route sweep cheap."""
from datetime import date

import flightwatch.collect as CO


def _cfg(routes, **gen):
    base = {"horizon_days": 28, "min_days_out": 0, "depart_step_days": 1,
            "trip_length_min": 20, "trip_length_max": 21, "enabled": True}
    base.update(gen)
    base["routes"] = routes
    return {"auto_generate": base}


def test_grid_respects_per_route_step():
    # Dense route steps every day; the second route overrides to every 7 days, so
    # it generates far fewer departures over the same horizon.
    cfg = _cfg([{"origin": "CHC", "destination": "CMB"},
                {"origin": "AKL", "destination": "DEL", "depart_step_days": 7}])
    grid = CO.generate_grid(cfg)
    chc = [g for g in grid if g["origin"] == "CHC"]
    akl = [g for g in grid if g["origin"] == "AKL"]
    # 0..28 inclusive: 29 daily departures vs 5 weekly, each x 2 trip lengths.
    assert len(chc) == 29 * 2
    assert len(akl) == 5 * 2


def test_grid_per_route_trip_lengths_override():
    cfg = _cfg([{"origin": "AKL", "destination": "BOM",
                 "trip_lengths": [14, 28], "depart_step_days": 14}])
    grid = CO.generate_grid(cfg)
    lengths = {g_len(g) for g in grid}
    assert lengths == {14, 28}


def g_len(it):
    a = date.fromisoformat(it["depart_date"])
    b = date.fromisoformat(it["return_date"])
    return (b - a).days


def test_grid_dedupes_against_fixed_keys():
    cfg = _cfg([{"origin": "CHC", "destination": "CMB", "depart_step_days": 7}])
    grid_all = CO.generate_grid(cfg)
    # Excluding the first generated key should drop exactly one itinerary.
    first = CO._itin_key(grid_all[0])
    grid_excl = CO.generate_grid(cfg, existing_keys={first})
    assert len(grid_excl) == len(grid_all) - 1
