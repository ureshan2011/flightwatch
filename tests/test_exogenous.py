"""Route-aware exogenous context: per-route events/currency, offline FX/fuel."""
import numpy as np
import pandas as pd

import flightwatch.exogenous as X


def test_destination_currency_and_country():
    # The traveller always departs NZ; the foreign end drives currency + events.
    assert X.dest_country("CHC", "CMB") == "LK"
    assert X.dest_country("AKL", "DEL") == "IN"
    assert X.dest_currency("CHC", "CMB") == "LKR"
    assert X.dest_currency("AKL", "BOM") == "INR"
    # Customer books in NZD, so we never key FX on the NZ side.
    assert X.dest_currency("CHC", "CMB") != "NZD"


def test_peak_months_are_separated_per_route():
    chc_cmb = X.route_peak_months("CHC", "CMB")
    akl_del = X.route_peak_months("AKL", "DEL")
    # Both share the NZ-origin seasons...
    assert {12, 1, 4, 7}.issubset(chc_cmb)
    assert {12, 1, 4, 7}.issubset(akl_del)
    # ...but each carries its OWN destination's festivals: Sri Lankan New Year
    # window (Aug Perahera) vs India's Diwali (Oct/Nov).
    assert 8 in chc_cmb and 8 not in akl_del
    assert 10 in akl_del and 11 in akl_del and 10 not in chc_cmb


def test_route_peak_map_keys():
    m = X.route_peak_map(["CHC-CMB", "AKL-DEL"])
    assert set(m) == {"CHC-CMB", "AKL-DEL"}
    assert m["CHC-CMB"] != m["AKL-DEL"]


def test_signals_neutral_when_series_absent():
    # The core contract: an absent/empty series yields a flat neutral 0 everywhere
    # (no signal, no leakage). Tested at the lookup level so it holds regardless of
    # whatever real data files happen to be checked into data/exogenous/.
    dates = pd.to_datetime(["2026-06-01", "2026-06-08", "2026-06-15"])
    assert np.allclose(X._asof_z(pd.Series(dtype=float), dates), 0.0)
    # NZD never has an FX series (the traveller already books in NZD).
    assert np.allclose(X.fx_z("NZD", dates), 0.0)


def test_load_series_zscores_and_asof(tmp_path):
    p = tmp_path / "fx_LKR.csv"
    p.write_text("date,value\n"
                 "2026-05-01,100\n2026-05-15,110\n2026-06-01,90\n2026-06-15,130\n")
    s = X._load_series(str(p))
    assert not s.empty
    assert abs(float(s.mean())) < 1e-9        # z-scored -> mean ~0
    assert abs(float(s.std()) - 1.0) < 1e-6   # ...unit std
    # As-of lookup uses the most recent value at/just before each date, 0 before
    # the series starts.
    z = X._asof_z(s, pd.to_datetime(["2026-04-01", "2026-05-20", "2026-07-01"]))
    assert z[0] == 0.0                          # before the first datapoint
    assert z[1] == float(s.loc["2026-05-15"])   # carried forward
    assert z[2] == float(s.loc["2026-06-15"])   # latest known
