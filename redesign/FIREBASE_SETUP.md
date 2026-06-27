# Firebase setup — project `flightproject-299a8`

This repo is **already wired** to Firebase project `flightproject-299a8`
(project number `634681815971`). The code paths — anonymous/Google auth, the
synced Watch list (`users/{uid}/trips` via `onSnapshot`), live route verdicts
(`routes/{route}`), FCM web push, and the scheduled per-user alert fan-out — are
all built. What remains is dropping in a few values only the Firebase console can
give you, and (optionally) turning on deploys.

## What's already wired in the repo

| Piece | File | Status |
|-------|------|--------|
| Default project | `.firebaserc` | ✅ `flightproject-299a8` |
| Public web config | `firebase.web.json` | ✅ live (`apiKey` + `appId` filled) |
| Firestore rules | `firestore.rules` | ✅ owner-only trips, public route reads |
| Firestore indexes | `firestore.indexes.json` | ✅ none required |
| Cloud Function (alert fan-out) | `functions/index.js` | ✅ deep-links to `flightproject-299a8.web.app` |
| Scan → Firestore writer | `flightwatch/publish.py` | ✅ needs a service account |
| Client cloud data layer | `flightwatch/dashboard.py` (`FaroStore`) | ✅ |
| Deploy workflow | `.github/workflows/firebase-deploy.yml` | ✅ no-op until secret set |

## Step 1 — Fill the public web config (lights up auth + sync)

In the Firebase console: **Project settings → General → Your apps**. If there's no
Web app yet, **Add app → Web** and register one (no Hosting checkbox needed). Copy
`apiKey` and `appId` from the `firebaseConfig` snippet into **`firebase.web.json`**:

```json
{
  "apiKey": "AIza…your-key…",
  "authDomain": "flightproject-299a8.firebaseapp.com",
  "projectId": "flightproject-299a8",
  "storageBucket": "flightproject-299a8.firebasestorage.app",
  "messagingSenderId": "634681815971",
  "appId": "1:634681815971:web:…your-app-id…",
  "vapidKey": ""
}
```

(The web `apiKey` is **not** a secret — it ships in client JS and is gated by
Firestore rules + Auth.) Prefer not to commit it? Leave the file blank and instead
set a GitHub **Variable** `FARO_FIREBASE_CONFIG` to the same JSON — the build
prefers the env var and falls back to the file. Either way, until `apiKey` is
present the site stays in localStorage **demo mode**.

## Step 2 — Enable Auth providers (required)

**Console → Authentication → Sign-in method**: enable **Anonymous** (every visitor
gets a private, rules-protected trip list) and **Google** (so "Sign in with
Google" links/upgrades that list). The site is hosted on **GitHub Pages**, so under
**Authentication → Settings → Authorized domains** add **`ureshan2011.github.io`**
(required for the Google sign-in popup to work) — alongside the default
`flightproject-299a8.firebaseapp.com`.

> Hosting = GitHub Pages (serving `docs/` from `main`). The push deep-link and the
> `firebase-deploy.yml` workflow are set up for that: Hosting is **not** deployed
> to Firebase by default (only via manual workflow dispatch). The Cloud Function's
> notification link defaults to `https://ureshan2011.github.io/flightwatch/` —
> override with `FARO_APP_URL` if you add a custom domain.

## Step 3 — Web push (optional)

**Console → Project settings → Cloud Messaging → Web Push certificates →
Generate key pair.** Put that key in `firebase.web.json`'s `vapidKey` (or the
`FARO_FCM_VAPID_KEY` env/Variable). The build then emits
`docs/firebase-messaging-sw.js` automatically and the Watch's push toggle works.

## Step 4 — Service account (server writes + deploys)

**Console → Project settings → Service accounts → Generate new private key.** Store
that JSON as a GitHub **Secret** `FARO_FIREBASE_SERVICE_ACCOUNT`. It enables:

* `python -m flightwatch publish` — upserts fresh `routes/{route}` verdicts after
  each scan (already invoked by `daily-scan.yml`, a no-op without the secret).
* `.github/workflows/firebase-deploy.yml` — deploys rules/indexes/functions on
  merge to main (and Hosting via manual dispatch). **Cloud Functions need the
  Blaze plan.**

Deploy manually anytime with the Firebase CLI:

```bash
firebase deploy --project flightproject-299a8 --only firestore:rules,firestore:indexes,functions
firebase deploy --project flightproject-299a8 --only hosting   # optional: serves docs/ at *.web.app
```

## Step 5 — Alert channels for the fan-out (optional)

`functions/index.js` (`fanOutAlerts`, every 6 h) pushes to each watched trip's
enabled channels:

* **Push (FCM)** — works out of the box once deployed (uses the project's runtime
  credentials).
* **Email** — install the **Trigger Email** extension; the function appends to the
  `mail` collection and the extension delivers it. No SMTP secrets in code.
* **Telegram** — set `TELEGRAM_BOT_TOKEN` in Secret Manager and bind it to the
  function; per-user chat ids live on the user doc as `telegramChatId`.

## Data model (for reference)

```
routes/{O-D}                     public read · server-only write (Admin SDK)
  { signal, confidence, fareNow, forecastLow, bestBuyWindowDays, outlook,
    observations, backtestHit, currency, updatedAt }
users/{uid}                      owner-only
  { email, name, fcmTokens[], telegramChatId }
users/{uid}/trips/{tripId}       owner-only
  { o, d, dep, ret, len, priceTarget, alerts:{push,telegram,email},
    lastNotifiedSignal, lastNotifiedAt }
```
