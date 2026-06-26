# Faro + Firebase — turning the "Watch" into a real product

> Companion to `redesign/UX_REVAMP.md`. The revamp's three surfaces — **Answer**,
> **Watch**, **Lab** — are all buildable as a static GitHub Pages site *except the
> follow-through*: a static site can't hold an account, sync across your phone and
> laptop, or push *you* a personal alert when *your* trip hits BUY. **That's the
> exact gap Firebase fills.** This doc maps each Firebase product to a concrete Faro
> capability, gives the data model, the per-user alert function, the security rules,
> and a phased plan — and the prototype (`docs/redesign/answer.html`) now
> demonstrates the client side of it.

---

## 1. What Firebase unlocks (capability → product)

| New capability | Firebase product | Why it matters for Faro |
|---|---|---|
| **Real accounts, zero-friction** | **Auth** (Anonymous → Google/email upgrade) | Pin a trip with no sign-in (anonymous uid); later "Sign in with Google" to keep it. Trips stop being trapped in one browser. |
| **Cross-device "My trips"** | **Firestore** `users/{uid}/trips` | Pin on your phone in bed, see it on your laptop at work. The Watch view becomes durable, not `localStorage`. |
| **Per-user alerts (the big one)** | **Cloud Functions** (scheduled) + Firestore | A function runs after each scan, reads *every user's* watched trips, and alerts **only the users whose trip** just hit BUY / a new low / a closing window. Today's `alerts.py` only knows the maintainer. |
| **Real push to the device** | **Cloud Messaging (FCM)** | "CHC→CMB just hit a new low — book now" on the lock screen. Web push today; native if a mobile app ever ships. |
| **Email alerts with no SMTP** | **Trigger Email extension** | Drop-in transactional email; no `SMTP_*` secrets to manage. |
| **Live verdict, no refresh** | Firestore **`onSnapshot`** listeners | When a new scan writes a fresh verdict, the open card updates in place — the "fare ticker" feel. |
| **Custom price targets** | Firestore + the alert function | "Also tell me if it drops below NZD 1,800." Stored per trip, evaluated server-side. |
| **Honest success metrics** | **Analytics** | Measure the North-Star (qualified *Book* clicks, composer usage, alert opt-ins) properly. |
| **Tune the model live** | **Remote Config** | Adjust BUY/WAIT thresholds or the deal-score cutoff without a redeploy. |
| **Shareable verdict + OG image** | **Hosting** + a Function | Server-render `og/CHC-CMB.png` so a shared link previews the verdict. |

You do **not** need to abandon the static site or the $0 promise — most of this fits
comfortably in Firebase's free **Spark** tier (see §6).

---

## 2. Architecture — keep the scraper, add a cloud edge

The existing pipeline barely changes. The GitHub Action still scrapes and commits
CSV; it just gains **one extra step**: write the freshest verdict/fares to Firestore
so clients and the alert function read live data instead of a rebuilt page.

```
            ┌────────────────────────────────────────────────────────────┐
            │  EXISTING (unchanged)                                        │
   cron ───►│  collect.py → predict.py → analytics.py → CSV in data/      │
            │                          │                                   │
            │                          └──► dashboard.py → docs/index.html │
            └──────────────────────────┬─────────────────────────────────┘
                                        │  NEW: one writer step
                                        ▼
                          ┌──────────────────────────┐
                          │  Firestore               │
                          │  routes/{route}/verdict  │◄── clients read (onSnapshot)
                          │  routes/{route}/fares     │
                          └─────────────┬────────────┘
                                        │ onWrite / scheduled
                                        ▼
                          ┌──────────────────────────┐        ┌───────────────┐
                          │  Cloud Function          │  match │  users/{uid}/ │
                          │  "fan-out alerts"        │◄───────│  trips        │
                          └─────────────┬────────────┘        └───────────────┘
                                        │ for each matched user + channel
                          ┌─────────────┼───────────────┐
                          ▼             ▼               ▼
                        FCM push    Telegram        Trigger-Email
```

The client (the prototype) reads `routes/*` for verdicts and reads/writes
`users/{uid}/trips` for the Watch. The Function is the new brain that makes alerts
*personal*.

---

## 3. Firestore data model

```
routes/{route}                       e.g. routes/CHC-CMB
  ├─ signal: "WAIT"                   latest published verdict (written by the scan)
  ├─ confidence: 78
  ├─ fareNow: 2140
  ├─ forecastLow: 1960
  ├─ bestBuyWindowDays: 9
  ├─ outlook: "falling"
  ├─ observations: 140
  ├─ backtestHit: 0.71
  ├─ updatedAt: <ts>
  └─ fares/{itineraryId}             cheapest-per-day rows for the finder/heatmap
        ├─ dep, ret, price, airline, stops

users/{uid}
  ├─ profile: { displayName, createdAt, anon: true|false }
  ├─ fcmTokens: [ "<token>", … ]     devices to push to
  └─ trips/{tripId}                  tripId = "CHC-CMB-2026-09-04-21"
        ├─ o, d, dep, ret, len
        ├─ alerts: { push: true, telegram: false, email: false }
        ├─ priceTarget: 1800 | null
        ├─ lastNotifiedSignal: "WAIT"   de-dupe so we don't re-alert the same call
        └─ createdAt
```

`tripId` is deterministic (route + dates) so a pin is idempotent and the alert
function can join `users/*/trips` against `routes/*` by id prefix.

---

## 4. The per-user alert function (the part a static site can't do)

```js
// functions/index.js  — runs after the scan publishes fresh verdicts
exports.fanOutAlerts = onSchedule("every 6 hours", async () => {
  const routes = await db.collection("routes").get();
  const verdict = Object.fromEntries(routes.docs.map(d => [d.id, d.data()]));

  // one collectionGroup query gets every watched trip across all users
  const trips = await db.collectionGroup("trips").get();
  for (const t of trips.docs) {
    const trip = t.data();
    const v = verdict[`${trip.o}-${trip.d}`];
    if (!v) continue;

    const hitTarget = trip.priceTarget && v.fareNow <= trip.priceTarget;
    const isBuy     = v.signal === "BUY" && trip.lastNotifiedSignal !== "BUY";
    const closing   = v.bestBuyWindowDays != null && v.bestBuyWindowDays <= 2;
    if (!(hitTarget || isBuy || closing)) continue;          // nothing to say

    const uid  = t.ref.parent.parent.id;
    const user = (await db.doc(`users/${uid}`).get()).data();
    const msg  = hitTarget
      ? `${trip.o}→${trip.d} dropped to ${v.fareNow} — under your ${trip.priceTarget} target.`
      : isBuy
        ? `${trip.o}→${trip.d}: BUY now — ${v.confidence}% confident, this is the low.`
        : `${trip.o}→${trip.d}: book within ~${v.bestBuyWindowDays}d — window closing.`;

    if (trip.alerts.push)     await sendFcm(user.fcmTokens, msg);
    if (trip.alerts.telegram) await sendTelegram(user.telegramChatId, msg);   // reuse alerts.py logic
    if (trip.alerts.email)    await queueEmail(user.email, msg);              // Trigger-Email
    await t.ref.update({ lastNotifiedSignal: v.signal });                    // de-dupe
  }
});
```

This is the missing half of `alerts.py`: same *signals*, but fanned out to **each
visitor's** trips and channels instead of one hard-wired chat.

---

## 5. Security rules (a user only ever touches their own data)

```
match /databases/{db}/documents {
  match /routes/{route} {                 // public read, server-only write
    allow read: if true;
    allow write: if false;                // only the Admin-SDK scan writes here
    match /fares/{f} { allow read: if true; allow write: if false; }
  }
  match /users/{uid} {                    // strictly the signed-in owner
    allow read, write: if request.auth != null && request.auth.uid == uid;
    match /trips/{t} { allow read, write: if request.auth.uid == uid; }
  }
}
```

Anonymous auth still produces a real `request.auth.uid`, so even un-signed-in users
get a private, rules-protected trip list that later "upgrades" in place when they
link a Google account (`linkWithPopup`) — no data migration.

---

## 6. Cost & footprint

- **Spark (free) tier** covers a hobby Faro comfortably: Firestore 50k reads / 20k
  writes / 1 GiB free per day; Auth unlimited; FCM free; Hosting 10 GB/mo.
- **Cloud Functions** scheduled triggers need the **Blaze** (pay-as-you-go) plan,
  but at this volume (a handful of routes, a few scans/day, a small user base) it
  rounds to **cents/month** — and the free Blaze grant usually absorbs it.
- The GitHub Action, scraper, model, and static dashboard stay exactly as they are.
  Firebase is an **additive edge**, not a rewrite.

---

## 7. Phased plan (extends UX_REVAMP §11)

| Stage | What ships | Firebase pieces |
|---|---|---|
| **F1 — Identity** | Anonymous uid on first visit; "Sign in with Google" to sync; trips move from `localStorage` → Firestore. | Auth (anon + Google), Firestore `users/{uid}/trips` |
| **F2 — Cloud Watch** | The Watch view reads/writes Firestore; verdict cards live-update via `onSnapshot`. | Firestore listeners |
| **F3 — Personal alerts** | Per-trip channels (push/telegram/email) + price target; the fan-out function. | Cloud Functions, FCM, Trigger-Email, reuse `alerts.py` for Telegram |
| **F4 — Live data feed** | Scan writes `routes/*`; site/finder read live data, decoupled from page rebuilds. | Admin SDK writer step in the Action |
| **F5 — Measure & tune** | North-Star analytics; threshold tuning without redeploy; shareable OG verdicts. | Analytics, Remote Config, Hosting + Function |

**The prototype now demonstrates the client side of F1–F3** in *demo mode* (no real
project needed): anonymous-vs-signed-in identity, a cloud-synced **Watch** view,
per-trip alert preferences + price target, and a "new scan landed" live-update. Drop
a real `window.FARO_FIREBASE` config in and the same code paths talk to a live
project.

---

### TL;DR

Static Pages gives you **Answer** and **Lab** for free. **Firebase is what makes the
*Watch* real** — accounts that sync, a cloud-stored trip list, live-updating
verdicts, and (the headline) a Cloud Function that pushes *each user* a personal
alert the moment *their* specific trip says BUY. Same scraper, same model, same
honesty — now with follow-through, for cents a month.
