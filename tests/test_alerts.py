"""Alerts: event detection, message composition, and graceful no-op."""
import flightwatch.alerts as AL
import flightwatch.analytics as A
import flightwatch.predict as P


def test_events_detect_low_and_drop(synth):
    df = synth(days=40, seed=9)
    df.loc[df["scan_date"] == df["scan_date"].max(), "price"] = 400.0
    recs = P.recommendations(df)
    ai = A.build(df, recs)
    kinds = {e["kind"] for e in AL._events(df, recs, ai)}
    assert "low" in kinds and "drop" in kinds


def test_compose_and_fmt_itin(synth):
    df = synth(days=40, seed=9)
    df.loc[df["scan_date"] == df["scan_date"].max(), "price"] = 400.0
    recs = P.recommendations(df)
    ai = A.build(df, recs)
    msg = AL._compose(AL._events(df, recs, ai), "NZD")
    assert "FlightWatch alerts" in msg
    assert "Christchurch" in AL.fmt_itin("CHC-CMB 2026-09-01 -> 2026-09-22")


def test_run_is_noop_without_credentials(tmp_path, monkeypatch, capsys):
    # Point storage + state at an empty temp dir; with no data it must not crash.
    monkeypatch.setattr(AL.storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(AL, "STATE_PATH", str(tmp_path / "alert_state.json"))
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)
    AL.run()
    assert "no" in capsys.readouterr().out.lower()
