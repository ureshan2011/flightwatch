"""
Optional scan -> Firestore writer (redesign/FIREBASE.md §2/§7 stage F4).

After the scrape has appended to data/ and the dashboard has been rebuilt, this
upserts the freshest per-route verdict into Firestore `routes/{route}` so the
web clients can read live data (via onSnapshot) and the per-user alert Cloud
Function has something to fan out from.

It is **strictly additive and skippable**:

  * It runs only when an Admin service account is configured. With none present
    it is a clean no-op -- the CSV write and dashboard build are never touched,
    affected, or blocked by it.
  * It NEVER writes to data/ and NEVER changes how/when CSVs are written.
  * The service-account JSON is a SECRET; it only comes from the environment
    (a GitHub Actions secret), never the repo.

Provide the credential in either form:

  FARO_FIREBASE_SERVICE_ACCOUNT   the service-account JSON itself (recommended in
                                  CI -- paste the JSON into a GitHub secret), or
  GOOGLE_APPLICATION_CREDENTIALS  a path to the service-account JSON file.

Run it with:  python -m flightwatch publish
"""

import os
import json
import re

from . import storage, predict, dashboard


def _configured():
    return bool((os.environ.get("FARO_FIREBASE_SERVICE_ACCOUNT") or "").strip()
                or (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip())


def _route_verdicts():
    """One published verdict per corridor, derived from the SAME recommendations
    and per-route backtest the dashboard shows -- the route's best current call."""
    df = storage.load_all()
    if df.empty or not (df["status"] == "ok").any():
        return [], "NZD"
    cur = str(df[df["status"] == "ok"]["currency"].iloc[0])
    bundle = predict.train_model(df)
    recs = predict.recommendations(df, bundle=bundle)
    backtests = predict.backtests_by_route(df)

    # recs are pre-sorted BUY > WATCH > WAIT then confidence, so the first rec we
    # see for a corridor is its strongest current call -- the one to publish.
    by_route = {}
    for r in recs:
        m = re.match(r"^([A-Z]{3}-[A-Z]{3}) ", r["itinerary"])
        if not m:
            continue
        by_route.setdefault(m.group(1), r)

    out = []
    for route, r in by_route.items():
        bt = backtests.get(route) or {}
        dtd, best = r.get("days_to_departure"), r.get("best_dtd")
        if r["signal"] == "BUY":
            window = 0
        elif dtd is not None and best is not None:
            window = max(0, dtd - best)
        else:
            window = None
        if r["signal"] == "BUY":
            outlook = "low"
        elif (r.get("expected_savings") or 0) > 0 or (r.get("momentum") or 0) < -1:
            outlook = "falling"
        else:
            outlook = "steady"
        out.append({
            "route": route,
            "signal": r["signal"],
            "confidence": r.get("confidence"),
            "fareNow": round(float(r["price"])),
            "forecastLow": round(float(r.get("predicted_low") or r["price"])),
            "bestBuyWindowDays": window,
            "outlook": outlook,
            "observations": r.get("points"),
            "backtestHit": bt.get("hit_rate"),
            "currency": cur,
        })
    return out, cur


def publish():
    if not _configured():
        print("[publish] No Firebase service account configured "
              "(FARO_FIREBASE_SERVICE_ACCOUNT / GOOGLE_APPLICATION_CREDENTIALS) "
              "-- skipping Firestore write. The CSV + dashboard build are unaffected.")
        return

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        print("[publish] firebase-admin is not installed -- skipping Firestore write. "
              "Add `firebase-admin` to the publish environment to enable it.")
        return

    sa_json = (os.environ.get("FARO_FIREBASE_SERVICE_ACCOUNT") or "").strip()
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
    else:
        cred = credentials.ApplicationDefault()
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    verdicts, _ = _route_verdicts()
    if not verdicts:
        print("[publish] No verdicts to publish yet (still collecting).")
        return

    from firebase_admin import firestore as _fs
    batch = db.batch()
    for v in verdicts:
        ref = db.collection("routes").document(v["route"])
        doc = {k: val for k, val in v.items() if k != "route"}
        doc["updatedAt"] = _fs.SERVER_TIMESTAMP
        batch.set(ref, doc, merge=True)
    batch.commit()
    print(f"[publish] Upserted {len(verdicts)} route verdict(s) to Firestore: "
          + ", ".join(v["route"] for v in verdicts))
