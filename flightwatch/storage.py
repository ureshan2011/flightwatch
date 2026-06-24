"""
Append-only storage. One CSV per month under data/ (e.g. data/flights_2026_06.csv).

Why monthly CSVs and not one big file or SQLite?
  - Clean git diffs (each scan adds a few rows, not a rewritten binary blob).
  - The files ARE the open dataset -- anyone can download and use them.
  - pandas reads them all back trivially for modelling and the dashboard.

Idempotency & intraday: each row carries a `scan_slot` -- its `scan_datetime`
truncated to the hour (e.g. "2026-06-20T19:00Z"). The dedupe key is
(scan_slot + itinerary), so:
  - scans at DIFFERENT times of day ACCUMULATE (intraday booking curve), and
  - re-running the SAME hourly slot OVERWRITES it (safe manual re-runs).
The model still collapses to one cheapest fare per day where it wants a daily
series, so finer-grained collection is purely additive.
"""

import os
import glob
from datetime import datetime, date

import pandas as pd

from . import DATA_DIR

COLUMNS = [
    "scan_datetime", "scan_date", "scan_slot", "origin", "destination",
    "depart_date", "return_date", "trip_length", "days_to_departure",
    "offer_index", "price", "currency", "airline", "stops", "duration_minutes",
    "layover", "status", "source",
]


def _month_path(d: date) -> str:
    return os.path.join(DATA_DIR, f"flights_{d.year}_{d.month:02d}.csv")


def slot_from_datetime(s) -> str:
    """Truncate an ISO scan_datetime to the hour: '...T09:54:31Z' -> '...T09:00Z'."""
    if not isinstance(s, str) or "T" not in s:
        return ""
    date_part, _, time_part = s.partition("T")
    hour = time_part[:2] if len(time_part) >= 2 and time_part[:2].isdigit() else "00"
    return f"{date_part}T{hour}:00Z"


def _ensure_slot(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee a populated `scan_slot` column, backfilling from scan_datetime.

    Keeps older CSVs (written before scan_slot existed) fully usable.
    """
    df = df.copy()
    if "scan_slot" not in df.columns:
        df["scan_slot"] = pd.NA
    slot = df["scan_slot"].astype("string")
    need = slot.isna() | (slot.str.strip() == "")
    if need.any() and "scan_datetime" in df.columns:
        derived = df["scan_datetime"].map(slot_from_datetime)
        df["scan_slot"] = slot.where(~need, derived)
    return df


def itinerary_key(row) -> str:
    """Dedupe key: hourly scan slot + itinerary (origin-dest + both dates)."""
    slot = row.get("scan_slot") if hasattr(row, "get") else row["scan_slot"]
    if not isinstance(slot, str) or not slot:
        slot = slot_from_datetime(row.get("scan_datetime", "")) or str(row.get("scan_date", ""))
    return (f"{slot}|{row['origin']}-{row['destination']}|"
            f"{row['depart_date']}|{row['return_date']}")


def append_rows(rows):
    """Append dict rows, de-duplicating on (scan_slot + itinerary)."""
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    new = _ensure_slot(pd.DataFrame(rows)).reindex(columns=COLUMNS)
    today = datetime.strptime(new["scan_date"].iloc[0], "%Y-%m-%d").date()
    path = _month_path(today)

    if os.path.exists(path):
        existing = _ensure_slot(pd.read_csv(path, dtype=str))
        existing["_k"] = existing.apply(itinerary_key, axis=1)
        new["_k"] = new.apply(itinerary_key, axis=1)
        existing = existing[~existing["_k"].isin(set(new["_k"]))]
        combined = pd.concat([existing.drop(columns="_k"), new.drop(columns="_k")],
                             ignore_index=True)
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
    df = _ensure_slot(df)
    for c in ["price", "trip_length", "days_to_departure", "stops", "duration_minutes"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["scan_date"] = pd.to_datetime(df["scan_date"], errors="coerce")
    return df
