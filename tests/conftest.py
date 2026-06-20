"""Shared test helpers: a synthetic fare dataset with a realistic booking curve."""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

_NUM = ["price", "trip_length", "days_to_departure", "stops", "duration_minutes"]


def make_df(itins=None, days=140, seed=0, slots=1):
    """Build a FlightWatch-shaped DataFrame for one or more itineraries.

    The price follows a classic booking curve (gently falls to a mid-window
    trough, firms up last-minute) plus noise, so models and analytics have
    something realistic to chew on. `slots` adds intra-day scans per day.
    """
    itins = itins or [
        ("CHC", "CMB", "2026-09-01", "2026-09-22", 1200, 1),
        ("CHC", "CMB", "2026-09-08", "2026-10-15", 1350, 1),
    ]
    rng = np.random.default_rng(seed)
    start, rows = date(2026, 1, 1), []
    for o, d, dep, ret, level, st in itins:
        depd = date.fromisoformat(dep)
        for k in range(days):
            sd = start + timedelta(days=k)
            dtd = (depd - sd).days
            if dtd < 8:
                continue
            shape = (1.0 + 0.25 * max(0, (dtd - 120) / 120)
                     + 0.45 * max(0, (25 - dtd) / 25)
                     - 0.08 * max(0, 1 - abs(dtd - 55) / 40))
            base = level * shape
            for si in range(slots):
                hh = "09" if si == 0 else "15"
                price = base - si * 30 + rng.normal(0, 12)
                rows.append(dict(
                    scan_datetime=f"{sd.isoformat()}T{hh}:00:00Z",
                    scan_date=sd.isoformat(), scan_slot=f"{sd.isoformat()}T{hh}:00Z",
                    origin=o, destination=d, depart_date=dep, return_date=ret,
                    trip_length=(date.fromisoformat(ret) - depd).days,
                    days_to_departure=dtd, offer_index=0, price=round(price, 0),
                    currency="NZD", airline="SriLankan", stops=st,
                    duration_minutes=960, status="ok", source="googleflights"))
    df = pd.DataFrame(rows)
    for c in _NUM:
        df[c] = pd.to_numeric(df[c])
    df["scan_date"] = pd.to_datetime(df["scan_date"])
    return df


@pytest.fixture
def synth():
    return make_df
