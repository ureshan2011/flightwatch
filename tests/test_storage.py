"""Storage: intraday slots accumulate, same slot overwrites, legacy CSVs load."""
import os

import pandas as pd

import flightwatch.storage as storage


def _row(hour, price, sd="2026-06-20"):
    return dict(scan_datetime=f"{sd}T{hour}:54:31Z", scan_date=sd,
                scan_slot=f"{sd}T{hour}:00Z", origin="CHC", destination="CMB",
                depart_date="2026-09-01", return_date="2026-09-22", trip_length=21,
                days_to_departure=73, offer_index=0, price=price, currency="NZD",
                airline="Jetstar", stops=1, duration_minutes=960,
                status="ok", source="googleflights")


def test_intraday_slots_accumulate(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.append_rows([_row("09", 1344)])
    storage.append_rows([_row("15", 1290)])           # different hour, same day
    df = storage.load_all()
    assert len(df) == 2
    assert set(df["scan_slot"]) == {"2026-06-20T09:00Z", "2026-06-20T15:00Z"}


def test_same_slot_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    storage.append_rows([_row("09", 1344)])
    storage.append_rows([_row("09", 1300)])           # same hour -> idempotent
    df = storage.load_all()
    assert len(df) == 1
    assert float(df["price"].iloc[0]) == 1300


def test_legacy_csv_without_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    legacy = {k: v for k, v in _row("09", 1344).items() if k != "scan_slot"}
    os.makedirs(str(tmp_path), exist_ok=True)
    pd.DataFrame([legacy]).to_csv(
        os.path.join(str(tmp_path), "flights_2026_06.csv"), index=False)

    df = storage.load_all()
    assert "scan_slot" in df.columns
    assert df["scan_slot"].iloc[0] == "2026-06-20T09:00Z"   # backfilled from datetime

    storage.append_rows([_row("09", 1280)])           # same slot as legacy -> overwrite
    df = storage.load_all()
    assert len(df) == 1 and float(df["price"].iloc[0]) == 1280
