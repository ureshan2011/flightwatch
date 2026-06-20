"""
Append-only storage. One CSV per month under data/ (e.g. data/flights_2026_06.csv).

Why monthly CSVs and not one big file or SQLite?
  - Clean git diffs (each day adds a few rows, not a rewritten binary blob).
  - The files ARE the open dataset -- anyone can download and use them.
  - pandas reads them all back trivially for modelling and the dashboard.

Idempotency: each row has a dedupe key (scan_date + itinerary). Re-running the
collector on the same day overwrites that day's rows rather than duplicating them.
"""

import os
import glob
from datetime import datetime, date

import pandas as pd

from . import DATA_DIR

COLUMNS = [
    "scan_datetime", "scan_date", "origin", "destination",
    "depart_date", "return_date", "trip_length", "days_to_departure",
    "offer_index", "price", "currency", "airline", "stops", "duration_minutes",
    "status", "source",
]


def _month_path(d: date) -> str:
    return os.path.join(DATA_DIR, f"flights_{d.year}_{d.month:02d}.csv")


def itinerary_key(row) -> str:
    return f"{row['scan_date']}|{row['origin']}-{row['destination']}|{row['depart_date']}|{row['return_date']}"


def append_rows(rows):
    """Append a list of dict rows, de-duplicating on (scan_date + itinerary)."""
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    new = pd.DataFrame(rows)[COLUMNS]
    today = datetime.strptime(new["scan_date"].iloc[0], "%Y-%m-%d").date()
    path = _month_path(today)

    if os.path.exists(path):
        existing = pd.read_csv(path, dtype=str)
        existing["_k"] = existing.apply(itinerary_key, axis=1)
        new["_k"] = new.apply(itinerary_key, axis=1)
        existing = existing[~existing["_k"].isin(set(new["_k"]))]
        combined = pd.concat([existing.drop(columns="_k"), new.drop(columns="_k")], ignore_index=True)
    else:
        combined = new

    # Keep a stable column order even if an older CSV predates a new column.
    combined = combined.reindex(columns=COLUMNS)
    combined.to_csv(path, index=False)


def load_all() -> pd.DataFrame:
    """Load every monthly CSV into one DataFrame (empty frame if no data yet)."""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "flights_*.csv")))
    if not files:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    for c in ["price", "trip_length", "days_to_departure", "stops", "duration_minutes"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["scan_date"] = pd.to_datetime(df["scan_date"], errors="coerce")
    return df
