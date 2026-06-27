# Exogenous signals (optional)

Local time-series that sharpen the fare model with market context it can't see
from price history alone. Everything stays free and key-less: no file here means
the matching feature is simply a neutral `0` (no signal), so the model trains fine
even before any data, and improves automatically once the files exist.

## How they're populated (auto)

`flightwatch/exogenous_fetch.py` fetches these on the **same cadence as the fare
scans** (the collector calls it best-effort each run; it's also a CLI):

```bash
python -m flightwatch exo              # refresh the latest points
python -m flightwatch exo --backfill   # also backfill history (first-time seed)
```

| Signal | Source (free, no key) | Cadence | Notes |
|--------|-----------------------|---------|-------|
| Brent crude (`fuel.csv`) | Yahoo Finance `BZ=F` daily close; **EIA** official Brent spot if `EIA_API_KEY` is set | daily (markets-open) | jet fuel tracks Brent ≈0.9 |
| `fx_INR.csv` | open.er-api.com, fallback ECB via frankfurter.dev | daily | full history available |
| `fx_LKR.csv` | open.er-api.com | daily | no free LKR *history* source, so it accumulates forward from first run |

**Accuracy & timing:** each row is keyed to the value's **own UTC reference date**
(not wall-clock now), the upsert is **idempotent** (re-running a slot re-confirms,
never duplicates), values are validated (finite, positive, sane day-over-day move),
and the model looks them up **backward-only** as-of the pricing day — so a market
holiday just carries the last close forward and nothing ever leaks a future value.

## Manual / custom data

The traveller this engine serves **always departs New Zealand and pays in NZD**,
so FX is keyed on the route's **destination** currency (the side that actually
moves local demand), never the booking currency.

## Files

Each file is a plain `date,value` CSV with an ISO date. Values can be in any unit
— each series is **z-scored over its own history**, so the model sees "unusually
high/low vs normal", not raw units.

| File | What it is |
|------|------------|
| `fuel.csv` | Jet-fuel / Brent crude proxy (global; affects every route). |
| `fx_LKR.csv` | LKR per 1 NZD (Sri Lanka — e.g. CHC↔CMB, AKL↔CMB). |
| `fx_INR.csv` | INR per 1 NZD (India — e.g. AKL↔DEL, AKL↔BOM). |

Example `fx_LKR.csv`:

```csv
date,value
2026-01-01,182.4
2026-02-01,184.1
2026-03-01,179.9
```

A value is looked up **as-of** the pricing day (most recent point at or before
it), so monthly or weekly granularity is fine — you don't need a row per day.

## Adding another destination currency

Map the airport/country in `config.yaml` under `route_context:` (see
`flightwatch/exogenous.py`), then drop a matching `fx_<CCY>.csv` here.
