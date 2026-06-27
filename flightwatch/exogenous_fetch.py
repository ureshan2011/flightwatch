"""
Fetch the OPTIONAL exogenous signals (destination-currency FX + a jet-fuel proxy)
from FREE, no-key public sources and upsert them into ``data/exogenous/*.csv`` on
the same cadence as the fare scans. True to the project's $0 ethos: standard
library only (urllib -- no `requests`), graceful failure (a fetch hiccup never
breaks a scan), and honest timing (rows are dated to the value's own reference
day in UTC and looked up backward-only by the model, so nothing leaks the future).

Sources -- all free, NO API key required:
  * FX  NZD->LKR / NZD->INR  --  open.er-api.com (daily reference rates; returns
        both currencies in one call). Fallback for INR: api.frankfurter.dev (ECB).
  * Fuel jet-fuel proxy      --  Brent crude daily close from Yahoo Finance
        (``BZ=F``). Brent tracks jet fuel ~0.9, is the standard proxy and needs no
        key. If ``EIA_API_KEY`` is set we additionally prefer EIA's official daily
        Brent spot series.

Timing & accuracy guarantees:
  * Rows are keyed on the UTC date the value is FOR (the provider's own reference
    date), not wall-clock now -- so a Saturday run returning Friday's rate is
    stored against Friday.
  * Idempotent upsert: re-running any cron slot re-confirms today's row instead of
    duplicating it. Weekends/market holidays simply add no new row; the model's
    as-of lookup carries the last close forward (never a future value).
  * Every value is validated (finite, positive, within a sane day-over-day band),
    so a bad tick can't poison the series.

Run it standalone (``python -m flightwatch exo`` / ``... exo --backfill``) or let
the collector call it best-effort at the start of each scan.
"""

import os
import csv
import json
import datetime as dt
import urllib.request

from . import exogenous

# Destination currencies we track (the traveller books in NZD; these are the
# foreign ends). Adding a route's currency here is all it takes to start a series.
FX_CURRENCIES = ("LKR", "INR")

# Reject a single new point that jumps more than this vs the last stored value --
# almost always a bad tick rather than a real move (esp. for slow-moving FX/fuel).
_MAX_DOD_JUMP = 0.40
_TIMEOUT = 25
_UA = "flightwatch-exogenous/1.0 (+https://github.com/ureshan2011/flightwatch)"


# --------------------------------------------------------------------------- #
# HTTP (stdlib; honours HTTPS_PROXY + SSL_CERT_FILE from the environment)
# --------------------------------------------------------------------------- #
def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _utc_today():
    return dt.datetime.now(dt.timezone.utc).date()


def _parse_rfc822_date(s):
    """'Sat, 27 Jun 2026 00:02:32 +0000' -> date in UTC, or today on failure."""
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).date()
    except Exception:
        return _utc_today()


# --------------------------------------------------------------------------- #
# FX  (NZD -> destination currency)
# --------------------------------------------------------------------------- #
def fetch_fx():
    """Return {ccy: (iso_date, rate)} for FX_CURRENCIES, with sources tried in
    order of coverage then quality. Missing currencies are simply omitted."""
    out = {}
    # Primary: open.er-api.com -- one call, covers LKR and INR, dated to its own
    # last-update timestamp.
    try:
        d = _get_json("https://open.er-api.com/v6/latest/NZD")
        if d.get("result") == "success":
            day = _parse_rfc822_date(d.get("time_last_update_utc", "")).isoformat()
            rates = d.get("rates") or {}
            for cur in FX_CURRENCIES:
                v = rates.get(cur)
                if isinstance(v, (int, float)) and v > 0:
                    out[cur] = (day, float(v))
    except Exception as e:
        print(f"  [fx] open.er-api failed: {type(e).__name__}: {e}")

    # Fallback for any still-missing currency frankfurter can serve (ECB; INR yes,
    # LKR no -- it just won't return one we can't get, which is fine).
    for cur in FX_CURRENCIES:
        if cur in out:
            continue
        try:
            d = _get_json(f"https://api.frankfurter.dev/v1/latest?base=NZD&symbols={cur}")
            v = (d.get("rates") or {}).get(cur)
            day = d.get("date") or _utc_today().isoformat()
            if isinstance(v, (int, float)) and v > 0:
                out[cur] = (day, float(v))
        except Exception as e:
            print(f"  [fx] frankfurter {cur} failed: {type(e).__name__}: {e}")
    return out


def fetch_fx_history(cur, days=180):
    """Backfill helper: ECB history for a currency frankfurter serves (INR).
    Returns [(date, rate), ...]. Empty for currencies it doesn't cover (e.g. LKR),
    which then just accumulate forward from today like the fare data does."""
    end = _utc_today()
    start = end - dt.timedelta(days=days)
    try:
        url = (f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
               f"?base=NZD&symbols={cur}")
        d = _get_json(url)
        rows = []
        for day, r in sorted((d.get("rates") or {}).items()):
            v = r.get(cur)
            if isinstance(v, (int, float)) and v > 0:
                rows.append((day, float(v)))
        return rows
    except Exception as e:
        print(f"  [fx] history {cur} failed: {type(e).__name__}: {e}")
        return []


# --------------------------------------------------------------------------- #
# Fuel  (Brent crude daily close as a jet-fuel proxy)
# --------------------------------------------------------------------------- #
def fetch_fuel(history_range="1y"):
    """Return [(date, brent_close)] from Yahoo Finance (no key), or EIA's official
    daily Brent spot when EIA_API_KEY is set. Most recent point last."""
    key = os.environ.get("EIA_API_KEY", "").strip()
    if key:
        rows = _fetch_fuel_eia(key)
        if rows:
            return rows
    return _fetch_fuel_yahoo(history_range)


def _fetch_fuel_yahoo(rng):
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/BZ=F"
               f"?interval=1d&range={rng}")
        d = _get_json(url)
        res = (d.get("chart") or {}).get("result") or []
        if not res:
            return []
        r0 = res[0]
        ts = r0.get("timestamp") or []
        closes = (((r0.get("indicators") or {}).get("quote") or [{}])[0]
                  .get("close") or [])
        rows = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            day = dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()
            if c > 0:
                rows.append((day, float(c)))
        return rows
    except Exception as e:
        print(f"  [fuel] yahoo failed: {type(e).__name__}: {e}")
        return []


def _fetch_fuel_eia(key):
    try:
        url = ("https://api.eia.gov/v2/petroleum/pri/spt/data/"
               f"?api_key={key}&frequency=daily&data[]=value"
               "&facets[series][]=RBRTE&sort[0][column]=period"
               "&sort[0][direction]=desc&length=400")
        d = _get_json(url)
        rows = []
        for rec in ((d.get("response") or {}).get("data") or []):
            day, v = rec.get("period"), rec.get("value")
            if day and isinstance(v, (int, float)) and v > 0:
                rows.append((str(day), float(v)))
        rows.sort()
        return rows
    except Exception as e:
        print(f"  [fuel] eia failed: {type(e).__name__}: {e}")
        return []


# --------------------------------------------------------------------------- #
# Idempotent, validated upsert into data/exogenous/<name>.csv
# --------------------------------------------------------------------------- #
def _read_existing(path):
    rows = {}
    if not os.path.exists(path):
        return rows
    try:
        with open(path, newline="") as f:
            for rec in csv.DictReader(f):
                day = (rec.get("date") or "").strip()
                try:
                    val = float(rec.get("value"))
                except (TypeError, ValueError):
                    continue
                if day:
                    rows[day] = (val, (rec.get("source") or "").strip())
    except Exception:
        pass
    return rows


def _upsert(name, new_rows, source):
    """Merge (date,value) rows into data/exogenous/<name>.csv.

    New fetches win over old rows for the same date (a later read corrects a
    provisional one). Each incoming point is validated finite/positive and, for an
    incremental update, sanity-checked against the last stored value so a bad tick
    can't poison the series. Returns the number of rows actually written/changed.
    """
    if not new_rows:
        return 0
    os.makedirs(exogenous.EXO_DIR, exist_ok=True)
    path = os.path.join(exogenous.EXO_DIR, name)
    merged = _read_existing(path)

    prior_last = None
    if merged:
        last_day = max(merged)
        prior_last = merged[last_day][0]

    changed = 0
    for day, val in sorted(new_rows):
        if not (isinstance(val, (int, float)) and val == val and val > 0):
            continue
        # Day-over-day sanity only when correcting/extending an existing series
        # with a SINGLE fresh point (backfills bring their own internally-consistent
        # history and shouldn't be gated against a stale last value).
        if len(new_rows) == 1 and prior_last is not None and prior_last > 0:
            if abs(val / prior_last - 1.0) > _MAX_DOD_JUMP:
                print(f"  [{name}] rejected {day}={val} (jump vs {prior_last})")
                continue
        old = merged.get(day)
        if old is None or abs(old[0] - val) > 1e-9 or old[1] != source:
            merged[day] = (val, source)
            changed += 1

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "value", "source"])
        for day in sorted(merged):
            val, src = merged[day]
            w.writerow([day, f"{val:.6f}".rstrip("0").rstrip("."), src])
    return changed


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(backfill=False):
    """Fetch every signal and upsert it. Best-effort and non-fatal: a failure in
    one source never stops the others (or the surrounding scan)."""
    print("Fetching exogenous signals (FX + jet-fuel proxy)...")
    total = 0

    fuel = fetch_fuel("1y" if backfill else "5d")
    total += _upsert("fuel.csv", fuel, "brent:yahoo"
                     if not os.environ.get("EIA_API_KEY") else "brent:eia")

    fx = fetch_fx()
    for cur, (day, val) in fx.items():
        rows = [(day, val)]
        if backfill:
            hist = fetch_fx_history(cur)
            if hist:
                rows = hist + rows
        total += _upsert(f"fx_{cur}.csv", rows, "open.er-api/frankfurter")

    exogenous.reset_cache()          # so a same-process model picks up new data
    avail = exogenous.available()
    print(f"  done: {total} row(s) written. Available now: "
          f"fuel={avail['fuel']} fx={avail['fx']}")
    return total
