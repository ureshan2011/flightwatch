/**
 * Faro — per-user alert fan-out (redesign/FIREBASE.md §4).
 *
 * This is the missing half of flightwatch/alerts.py: the same *signals*, but
 * fanned out to EACH visitor's pinned trips and their chosen channels, instead
 * of one hard-wired maintainer chat.
 *
 * Runs on a schedule (after the scan publishes fresh verdicts to routes/*). For
 * every watched trip across all users it checks whether the trip just hit a
 * price target, a fresh BUY, or a closing best-buy window, and if so pushes via
 * the channels that trip has enabled (FCM push / Telegram / email). It de-dupes
 * with lastNotifiedSignal so the same call is never sent twice.
 *
 * Config (functions config or env):
 *   TELEGRAM_BOT_TOKEN   shared bot token for Telegram pushes (per-user chat id
 *                        lives on the user doc as telegramChatId).
 * Email uses the Firebase "Trigger Email" extension: we just append a doc to the
 * `mail` collection and the extension delivers it (no SMTP secrets here).
 */
const {onSchedule} = require("firebase-functions/v2/scheduler");
const {initializeApp} = require("firebase-admin/app");
const {getFirestore, FieldValue} = require("firebase-admin/firestore");
const {getMessaging} = require("firebase-admin/messaging");
const logger = require("firebase-functions/logger");

initializeApp();
const db = getFirestore();

const CITY = {
  CHC: "Christchurch", AKL: "Auckland", CMB: "Colombo", DEL: "Delhi", BOM: "Mumbai",
};
const money = (cur, v) => `${cur || "NZD"} ${Math.round(v).toLocaleString("en-NZ")}`;

/** Plain-English alert line for a matched trip — mirrors alerts.py's tone. */
function compose(trip, v, why) {
  const o = CITY[trip.o] || trip.o;
  const d = CITY[trip.d] || trip.d;
  const who = `${o} → ${d}`;
  const cur = v.currency || "NZD";
  if (why === "target") {
    return `🔻 ${who} dropped to ${money(cur, v.fareNow)} — under your ${money(cur, trip.priceTarget)} target.`;
  }
  if (why === "buy") {
    return `🟢 ${who}: BUY now — ${v.confidence}% confident, this looks like the low at ${money(cur, v.fareNow)}.`;
  }
  return `⏳ ${who}: book within ~${v.bestBuyWindowDays}d — the best-buy window is closing (${money(cur, v.fareNow)}).`;
}

// Where a push notification deep-links to. The site is hosted on GitHub Pages;
// override with FARO_APP_URL if you move to a custom domain or Firebase Hosting.
const APP_URL = process.env.FARO_APP_URL || "https://ureshan2011.github.io/flightwatch/";

async function sendFcm(tokens, text) {
  const list = (tokens || []).filter(Boolean);
  if (!list.length) return;
  await getMessaging().sendEachForMulticast({
    tokens: list,
    notification: {title: "Faro", body: text},
    webpush: {fcmOptions: {link: APP_URL}},
  });
}

async function sendTelegram(chatId, text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token || !chatId) return;
  // Same Telegram Bot API call alerts.py uses.
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chat_id: chatId, text, disable_web_page_preview: true}),
  });
}

async function queueEmail(to, text) {
  if (!to) return;
  // Firebase "Trigger Email" extension: writing the doc sends the mail.
  await db.collection("mail").add({
    to,
    message: {subject: "Faro — time to look at your trip", text},
  });
}

exports.fanOutAlerts = onSchedule(
    {schedule: "every 6 hours", timeZone: "Etc/UTC"},
    async () => {
      const routesSnap = await db.collection("routes").get();
      const verdict = Object.fromEntries(routesSnap.docs.map((d) => [d.id, d.data()]));

      const trips = await db.collectionGroup("trips").get();
      let sent = 0;
      for (const t of trips.docs) {
        const trip = t.data();
        const v = verdict[`${trip.o}-${trip.d}`];
        if (!v) continue;

        const alerts = trip.alerts || {};
        const hitTarget = trip.priceTarget && v.fareNow != null && v.fareNow <= trip.priceTarget;
        const isBuy = v.signal === "BUY" && trip.lastNotifiedSignal !== "BUY";
        const closing = v.bestBuyWindowDays != null && v.bestBuyWindowDays <= 2 &&
          trip.lastNotifiedSignal !== "CLOSING";
        const why = hitTarget ? "target" : isBuy ? "buy" : closing ? "closing" : null;
        if (!why) continue;

        const uid = t.ref.parent.parent.id;
        const user = (await db.doc(`users/${uid}`).get()).data() || {};
        const text = compose(trip, v, why);

        try {
          if (alerts.push) await sendFcm(user.fcmTokens, text);
          if (alerts.telegram) await sendTelegram(user.telegramChatId, text);
          if (alerts.email) await queueEmail(user.email, text);
        } catch (e) {
          logger.error("send failed", uid, e);
          continue;
        }
        // De-dupe: record what we just told them so we don't repeat the same call.
        await t.ref.update({
          lastNotifiedSignal: why === "closing" ? "CLOSING" : v.signal,
          lastNotifiedAt: FieldValue.serverTimestamp(),
        });
        sent++;
      }
      logger.info(`fanOutAlerts: ${sent} alert(s) across ${trips.size} watched trips.`);
    },
);
