"""
Free push alerts. After a scan, this turns the freshest signals into short
notifications and (if credentials are configured) pushes them to Telegram and/or
email. With no credentials it is a graceful no-op -- so FlightWatch stays free
and runnable by anyone, and only YOU get pinged once you add secrets.

What fires an alert:
  * a fresh ALL-TIME LOW for a route (with enough history to be meaningful),
  * a statistically unusual PRICE DROP (the analytics MAD-z anomaly),
  * a new BUY call from the decision engine (confidence-gated),
  * a "book now -- window closing" nudge when departure is near and it's a BUY.

De-duplication: each event has a stable id keyed to the day, stored in
data/alert_state.json, so the same condition is never sent twice.

Setup (all free):
  Telegram -- message @BotFather to make a bot, then set repo secrets
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
  Email (optional) -- set SMTP_HOST, SMTP_USER, SMTP_PASS, ALERT_EMAIL_TO
    (e.g. a Gmail address + app password).
"""

import os
import re
import json
import smtplib
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

from . import DATA_DIR, storage, predict, analytics

STATE_PATH = os.path.join(DATA_DIR, "alert_state.json")
MIN_HISTORY_FOR_LOW = 3          # don't call day-one prices "all-time lows"
MIN_BUY_CONFIDENCE = 50
WINDOW_CLOSING_DTD = 10


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"sent": []}


def _save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=0)


def fmt_itin(itin):
    """'CHC-CMB 2026-09-01 -> 2026-09-22' -> 'Christchurch -> Colombo, 1 Sep-22 Sep'."""
    from .dashboard import city_name
    m = re.match(r"^([A-Z]{3})-([A-Z]{3}) (\d{4}-\d{2}-\d{2}) -> (\d{4}-\d{2}-\d{2})$", itin)
    if not m:
        return itin
    mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    def d(s):
        y, mo, da = s.split("-")
        return f"{int(da)} {mon[int(mo) - 1]}"
    return f"{city_name(m.group(1))} -> {city_name(m.group(2))}, {d(m.group(3))}-{d(m.group(4))}"


def _events(df, recs, ai):
    """Build the list of alert-worthy events with stable, day-keyed ids."""
    daily = predict.daily_min(df)
    counts = daily.groupby("itin").size().to_dict()
    day = str(df[df["status"] == "ok"]["scan_date"].max())[:10]
    events = []

    # Fresh all-time low (needs history; compares against the PRIOR minimum).
    for itin, h in daily.groupby("itin"):
        h = h.sort_values("scan_date")
        if len(h) < MIN_HISTORY_FOR_LOW:
            continue
        prices = h["price"].astype(float).to_numpy()
        if prices[-1] <= prices[:-1].min():
            events.append({"id": f"low|{itin}|{round(prices[-1])}", "kind": "low",
                           "itin": itin, "price": round(float(prices[-1]))})

    # Unusual drops (robust-z anomaly).
    for a in ai.get("anomalies", []):
        if a["kind"] == "drop":
            events.append({"id": f"drop|{a['itin']}|{day}", "kind": "drop",
                           "itin": a["itin"], "price": a["price"], "pct": a["pct"]})

    # New BUY calls + window-closing nudges.
    for r in recs:
        if r["signal"] != "BUY" or r["confidence"] < MIN_BUY_CONFIDENCE:
            continue
        closing = r.get("days_to_departure", 999) <= WINDOW_CLOSING_DTD
        kind = "closing" if closing else "buy"
        events.append({"id": f"{kind}|{r['itinerary']}|{day}", "kind": kind,
                       "itin": r["itinerary"], "price": round(r["price"]),
                       "confidence": r["confidence"], "reason": r["reason"],
                       "dtd": r.get("days_to_departure")})
    return events


def _compose(events, cur):
    money = lambda v: f"{cur} {round(v):,}"
    icon = {"low": "🔻", "drop": "📉", "buy": "🟢", "closing": "⏳"}
    lines = ["✈️ FlightWatch alerts", ""]
    for e in events:
        who = fmt_itin(e["itin"])
        if e["kind"] == "low":
            lines.append(f"🔻 New all-time low — {who}: {money(e['price'])}")
        elif e["kind"] == "drop":
            lines.append(f"📉 Price drop — {who}: {money(e['price'])} ({e['pct']}% vs recent norm)")
        elif e["kind"] == "closing":
            lines.append(f"⏳ Book soon ({e['dtd']}d to go) — {who}: {money(e['price'])} "
                         f"· BUY {e['confidence']}%")
        else:
            lines.append(f"🟢 BUY — {who}: {money(e['price'])} ({e['confidence']}% confidence). "
                         f"{e['reason']}")
    return "\n".join(lines)


def _send_telegram(token, chat, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        r.read()


def _send_email(text):
    host, user = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER")
    pw, to = os.environ.get("SMTP_PASS"), os.environ.get("ALERT_EMAIL_TO")
    if not all([host, user, pw, to]):
        return False
    msg = EmailMessage()
    msg["Subject"] = "FlightWatch alerts"
    msg["From"], msg["To"] = user, to
    msg.set_content(text)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=20) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return True


def run(dry_run=False):
    """Compute fresh events and dispatch them; safe to call every scan."""
    df = storage.load_all()
    if df.empty or not (df["status"] == "ok").any():
        print("No data yet; no alerts.")
        return
    cur = df[df["status"] == "ok"]["currency"].iloc[0]
    recs = predict.recommendations(df)
    ai = analytics.build(df, recs)

    state = _load_state()
    sent = set(state.get("sent", []))
    events = _events(df, recs, ai)
    fresh = [e for e in events if e["id"] not in sent]

    if not fresh:
        print(f"{len(events)} event(s) found, none new since the last run.")
        return

    text = _compose(fresh, cur)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")

    if dry_run or not (token and chat or os.environ.get("SMTP_HOST")):
        tag = "[dry-run]" if dry_run else "[no alert credentials set; not sending]"
        print(f"{tag} {len(fresh)} new event(s):\n{text}")
        return                       # don't persist -- so real creds still fire later

    delivered = False
    try:
        if token and chat:
            _send_telegram(token, chat, text)
            delivered = True
        if _send_email(text):
            delivered = True
    except Exception as e:
        print(f"Alert send failed: {e}")
        return

    if delivered:
        sent.update(e["id"] for e in fresh)
        state["sent"] = list(sent)[-500:]
        state["updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_state(state)
        print(f"Sent {len(fresh)} alert(s).")
