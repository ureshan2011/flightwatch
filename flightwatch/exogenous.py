"""
Route-aware exogenous context: per-route event calendars, destination-currency
FX and a jet-fuel index -- all OFFLINE and config-driven, true to FlightWatch's
no-paid-API ethos.

The traveller this engine serves ALWAYS flies *out of New Zealand* and pays in
NZD. Two consequences shape everything here:

  * The booking currency is fixed (NZD), so we never model FX on the price the
    customer pays. What varies *by route* is the OTHER end. A weak Sri Lankan
    rupee or Indian rupee, and festivals/holidays at the destination, shift local
    demand (diaspora visits home, inbound tourism) and therefore the fare. So the
    FX signal and the event calendar are keyed on each route's DESTINATION
    country, then blended with the NZ-origin school/holiday seasons every route
    shares.
  * Events are kept SEPARATE PER ROUTE. CHC->CMB runs hot on Sri Lankan New Year
    (April); AKL->DEL runs hot on Diwali (Oct/Nov). A single global "peak months"
    set blurs those; here each route gets the union of its NZ-origin seasons and
    its own destination's seasons.

Fuel (a jet-fuel index) and FX are read from optional local CSVs under
``data/exogenous/`` when present, and fall back to a NEUTRAL value (z-score 0,
"no signal") when absent -- so the features exist and train today, and sharpen
automatically the moment a data file is dropped in. Nothing here ever touches the
network.

Drop-in data files (both optional, both ``date,value`` with an ISO date):
    data/exogenous/fuel.csv          # a jet-fuel / Brent proxy, any unit
    data/exogenous/fx_LKR.csv        # LKR per 1 NZD (destination ccy per NZD)
    data/exogenous/fx_INR.csv        # INR per 1 NZD
We z-score each series over its own history, so the model sees "unusually
high/low fuel/FX vs normal" rather than raw units -- robust to whatever scale a
contributor's CSV happens to use.
"""

import os
import functools

import numpy as np
import pandas as pd

from . import DATA_DIR, CONFIG_PATH

EXO_DIR = os.path.join(DATA_DIR, "exogenous")

# Origin country for the traveller this engine serves. Every supported route
# departs New Zealand.
ORIGIN_COUNTRY = "NZ"

# Airport (IATA) -> country. Only the destinations we actually fly to need an
# entry; anything unmapped is treated as the destination's own isolated context.
_AIRPORT_COUNTRY = {
    # New Zealand origins
    "CHC": "NZ", "AKL": "NZ", "WLG": "NZ", "ZQN": "NZ", "DUD": "NZ",
    # Sri Lanka
    "CMB": "LK",
    # India
    "DEL": "IN", "BOM": "IN", "MAA": "IN", "BLR": "IN", "HYD": "IN", "CCU": "IN",
}

# Country -> destination-side booking currency (informational; the customer still
# pays in NZD). Used to pick which fx_<CCY>.csv series feeds the route.
_COUNTRY_CCY = {"NZ": "NZD", "LK": "LKR", "IN": "INR"}

# Demand-heavy months per country: festivals + school holidays that lift fares.
#   NZ -- Dec/Jan (summer + festive), Apr (school/Easter), Jul (winter school hols)
#   LK -- Apr (Sinhala & Tamil New Year, the big diaspora-return window),
#         Aug (Esala/Kandy Perahera), Dec (Christmas + year-end season)
#   IN -- Oct/Nov (Diwali/Dussehra), Dec (Christmas + year-end), May (summer hols)
_COUNTRY_PEAK_MONTHS = {
    "NZ": {12, 1, 4, 7},
    "LK": {4, 8, 12},
    "IN": {10, 11, 12, 5},
}

# Allow config.yaml to extend/override the maps without code changes. Optional
# top-level `route_context:` block:
#
#   route_context:
#     airport_country: {SIN: SG}
#     country_peak_months: {LK: [4, 8, 12]}
#     country_currency: {SG: SGD}
def _config_overrides():
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return
    rc = cfg.get("route_context") or {}
    for code, ctry in (rc.get("airport_country") or {}).items():
        _AIRPORT_COUNTRY[str(code).upper()] = str(ctry).upper()
    for ctry, ccy in (rc.get("country_currency") or {}).items():
        _COUNTRY_CCY[str(ctry).upper()] = str(ccy).upper()
    for ctry, months in (rc.get("country_peak_months") or {}).items():
        _COUNTRY_PEAK_MONTHS[str(ctry).upper()] = {int(m) for m in months}


_config_overrides()


def airport_country(code) -> str:
    return _AIRPORT_COUNTRY.get(str(code).upper(), "")


def dest_country(origin, destination) -> str:
    """The route's foreign end. The traveller departs NZ, so the destination is
    the non-NZ side; if neither/both look like NZ we fall back to the raw
    destination's country (or empty)."""
    oc, dc = airport_country(origin), airport_country(destination)
    if dc and dc != ORIGIN_COUNTRY:
        return dc
    if oc and oc != ORIGIN_COUNTRY:
        return oc
    return dc or oc


def dest_currency(origin, destination) -> str:
    return _COUNTRY_CCY.get(dest_country(origin, destination), "")


def route_peak_months(origin, destination) -> set:
    """Months this SPECIFIC route runs hot: NZ-origin seasons unioned with the
    destination country's own festival/holiday seasons."""
    months = set(_COUNTRY_PEAK_MONTHS.get(ORIGIN_COUNTRY, set()))
    months |= set(_COUNTRY_PEAK_MONTHS.get(dest_country(origin, destination), set()))
    return months or set(_COUNTRY_PEAK_MONTHS.get(ORIGIN_COUNTRY, set()))


def route_peak_map(routes) -> dict:
    """{ 'CHC-CMB': {months...} } for each distinct 'O-D' route string."""
    out = {}
    for r in set(map(str, routes)):
        o, _, d = r.partition("-")
        out[r] = route_peak_months(o, d)
    return out


# --------------------------------------------------------------------------- #
# Optional local time series (fuel, FX) -- z-scored, neutral when absent.
# --------------------------------------------------------------------------- #
def _load_series(path) -> pd.Series:
    """Load a `date,value` CSV into a z-scored, date-indexed, sorted Series.

    Returns an empty Series if the file is missing/unreadable/degenerate, which
    the feature layer reads as a neutral 0 (no signal) everywhere.
    """
    try:
        raw = pd.read_csv(path)
    except Exception:
        return pd.Series(dtype=float)
    cols = {c.lower(): c for c in raw.columns}
    dcol = cols.get("date")
    vcol = next((cols[c] for c in cols if c in ("value", "rate", "price", "close")), None)
    if dcol is None or vcol is None:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(raw[dcol], errors="coerce")
    val = pd.to_numeric(raw[vcol], errors="coerce")
    s = pd.Series(val.to_numpy(), index=idx).dropna().sort_index()
    if s.size < 3 or float(s.std()) == 0.0:
        return pd.Series(dtype=float)
    return (s - float(s.mean())) / float(s.std())


@functools.lru_cache(maxsize=None)
def fuel_series() -> pd.Series:
    return _load_series(os.path.join(EXO_DIR, "fuel.csv"))


@functools.lru_cache(maxsize=None)
def fx_series(currency) -> pd.Series:
    cur = (currency or "").upper()
    if not cur or cur == "NZD":
        return pd.Series(dtype=float)
    return _load_series(os.path.join(EXO_DIR, f"fx_{cur}.csv"))


def _asof_z(series, dates) -> np.ndarray:
    """As-of lookup: the most recent series value at/just before each date.

    Empty series -> all zeros (neutral). `dates` is a datetime-like array.
    """
    dates = pd.to_datetime(pd.Series(dates).reset_index(drop=True), errors="coerce")
    if series is None or series.empty:
        return np.zeros(len(dates), dtype=float)
    left = pd.DataFrame({"d": dates, "_o": np.arange(len(dates))}).sort_values("d")
    right = pd.DataFrame({"d": series.index, "z": series.to_numpy()})
    merged = pd.merge_asof(left, right, on="d", direction="backward")
    merged = merged.sort_values("_o")
    return merged["z"].fillna(0.0).to_numpy()


def fuel_z(dates) -> np.ndarray:
    """Z-scored jet-fuel level as of each date (0 when no data file present)."""
    return _asof_z(fuel_series(), dates)


def fx_z(currency, dates) -> np.ndarray:
    """Z-scored destination-currency-per-NZD as of each date (0 when absent).

    A positive value means the destination currency is unusually STRONG vs the
    NZD; the model learns whatever relationship that carries for the route.
    """
    return _asof_z(fx_series(currency), dates)


def reset_cache():
    """Drop cached series (tests / after dropping in a new data file)."""
    fuel_series.cache_clear()
    fx_series.cache_clear()


def available() -> dict:
    """Which optional series are actually present -- surfaced for transparency."""
    return {
        "fuel": not fuel_series().empty,
        "fx": {c: not fx_series(c).empty for c in ("LKR", "INR")},
    }
