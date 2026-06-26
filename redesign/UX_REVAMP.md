# Faro — A UX Revamp Proposal (the product, not just the paint)

> A second-pass redesign brief. The repo already has a strong **visual** redesign
> (`redesign/REDESIGN.md` → the shipped dark + gold theme): tokens, a verdict
> panel, chapters, accessibility and performance. That work is good and most of it
> should stay. **This document deliberately goes one level up** — from *"how the
> page looks"* to *"what the product actually is and how a real person uses it."*
>
> If `REDESIGN.md` answered *"make the dashboard calm and legible,"* this answers
> *"stop shipping a dashboard at all — ship an answer machine."*

---

## 0. The one-sentence thesis

**Faro is currently an analyst's console that a visitor has to operate. It should
be a personal answer machine that operates itself.** The single highest-leverage
UX change is to invert the model: instead of *"here are 15 sections about every
route I track — go find your situation,"* it becomes *"tell me your trip, I'll
tell you what to do, and I'll watch it for you."*

Everything below serves that inversion.

---

## 1. Diagnosis — who is this really for, and where does the current UX fail them?

### 1.1 The two real users (and the product serves neither cleanly today)

| | **Maya — the buyer** (95% of traffic) | **Sam — the data nerd / you** (5%, but loud) |
|---|---|---|
| Mental state | Anxious. "I have to fly CHC→CMB in September. Am I about to overpay? Should I book *tonight* or wait?" | Curious. "Show me the booking curve, the backtest, the cheapest weekday, the raw grid." |
| Wants | **One trustworthy verdict** for *her* dates, and to be told when to pull the trigger. | Density, receipts, the dataset, the model honesty. |
| Time budget | 20 seconds, on a phone, probably from a WhatsApp link her cousin sent. | 20 minutes, on a laptop, scrolling everything. |
| Today she gets | A 357 KB page of *every* route the maintainer configured, a fixed route switcher (she can't enter her own dates), 15 stacked sections, and a verdict buried among them. | Actually pretty well served — the depth exists. |

The product was built by Sam, for Sam, and Maya is the one who actually shows up.
**The revamp is mostly about building Maya's product without losing Sam's.**

### 1.2 The five structural UX failures (distinct from the visual critique already filed)

1. **It answers a question the visitor didn't ask.** The page is organized around
   *routes the maintainer tracks*, not *the trip the visitor has in mind*. Maya
   can't say "I want CHC→CMB, leave Sept 4, back Sept 25." She can only pick a
   pre-configured corridor and hope her dates are near the flagship's. The single
   most important input in the entire domain — *her dates* — has no front door.

2. **It's a monologue, not a tool.** Everything is render-once, scroll-forever.
   There's no state, no "my trip," no memory. Close the tab and Faro forgets you
   exist. For a product whose entire value is *"watch this over time and tell me
   when,"* having zero persistence or follow-through on the web side is the core
   miss. The alerting engine (`alerts.py`) is real — but it's wired to the
   *maintainer's* Telegram, not the *visitor's* trip.

3. **One audience, one firehose.** Maya and Sam get the identical 15-section wall.
   Maya drowns; Sam's depth makes Maya bounce. There is no "simple by default,
   deep on demand" split — just everything, always, for everyone.

4. **The trust story is buried.** Faro's *actual* moat vs Google Flights / Hopper
   is **honesty** — calibrated confidence, a walk-forward backtest, "still
   collecting," "informational only." That's the most persuasive thing here and
   it's scattered into footnotes and a mid-page "Signal accuracy" section. Trust
   should be a headline feature, not fine print.

5. **It's a desktop document on a phone-first journey.** Fare-watching is a
   lock-screen activity. The artifact is a dense desktop dashboard plus a **4.3 MB
   `explore.json`** that a phone on diaspora-grade mobile data has to swallow. The
   medium and the moment are mismatched.

---

## 2. The big bet — reframe Faro as **"Tell me your trip. I'll tell you when."**

Three product surfaces, in priority order. The first is new and is the whole game.

```
   ┌────────────────────────────────────────────────────────────────┐
   │  1. THE ANSWER          2. THE WATCH           3. THE LAB        │
   │  (Maya, 20s, phone)     (Maya, recurring)      (Sam, deep)       │
   │                                                                  │
   │  Trip composer  ─────►  Pin this trip   ─────► Full dataset,     │
   │  → one verdict          → get told when        backtest, finder, │
   │  → the evidence         to buy (TG/email/      every route,      │
   │    on demand            calendar/web push)     market analytics  │
   └────────────────────────────────────────────────────────────────┘
```

- **The Answer** — a *trip composer* replaces the fixed route switcher as the hero.
  Maya types/taps her route and rough dates; Faro returns the verdict card for the
  closest covered itinerary (or the honest "still collecting / try these dates"
  empty state). This is the front door the product is missing.
- **The Watch** — she pins that trip. It lives in `localStorage` as "My trips," and
  she can attach an alert (deep-link into the existing Telegram bot pre-filled with
  her route+dates, an `.ics` "remind me to check" calendar event, or browser Web
  Push). The dashboard finally *follows through*.
- **The Lab** — everything Sam loves (backtest receipts, the finder over
  `explore.json`, cross-route market analytics, the raw CSV) moves behind its own
  view. Nothing is deleted; it's demoted from "default firehose" to "deep mode you
  choose."

This is still **one statically-generated file on GitHub Pages with no backend** —
every bit of it is client-side state + the data Faro already commits. No new
infra, no API, no cost. It's a re-architecture of *attention*, not of stack.

---

## 3. Information architecture — from one scroll to a 3-view app

Today: 15 sections, equal weight, single infinite scroll. Proposed: a tiny
client-side **hash-routed** app (no framework — just `location.hash` + show/hide,
the same muscle `setScope()` already uses) with three views and a persistent shell.

```
┌ SHELL  Faro ·  [ My trips ▾ ]            ☀/☾   · fare outlook: ▼ falling ·  ⓘ about
│
├ ◉ ANSWER  (default · #/)
│    ┌──────────────────────── TRIP COMPOSER ───────────────────────┐
│    │  From [CHC ▾]   To [CMB ▾]   Leave [~ early Sep]  Back [~3 wk]│
│    └───────────────────────────────────────────────────────────────┘
│    ┌──────────────────────── THE VERDICT ─────────────────────────┐
│    │  WAIT          78% confident      ⟳ best-buy window: ~9 days  │
│    │  Fares should fall ~NZD 180 in ~2 weeks.                      │
│    │  NZD 2,140 now → NZD 1,960 forecast low      −8%              │
│    │  [ Pin & watch this trip ]   [ Book on Aviasales → ]  audit ▾ │
│    └───────────────────────────────────────────────────────────────┘
│    fare story (sparkline tape) · why we think this (collapsed) · trust line
│
├ ○ WATCH  (#/trips)   → "My trips" cards, each a pinned verdict + alert controls
│
└ ○ LAB    (#/lab)     → routes board · history · forecast fan · heatmap ·
                          what-changed · backtest · finder · market analytics
```

Key IA moves:

- **Trip composer is the hero**, not a fixed pill bar. The route switcher becomes
  *one input among* {from, to, leave, return-length}. It still resolves to a
  covered itinerary under the hood (reuse `setScope()` / the `recs`+`focus`
  payload), but now the *user's* trip is the unit, not the maintainer's route.
- **Three views, one shell.** "Answer" loads instantly and is all Maya ever needs.
  "Lab" is opt-in and is where the heavy stuff (and the 4.3 MB finder) lazy-loads.
- **"Fare outlook" in the shell** is a persistent, glanceable mood line (▼ falling
  / ▲ rising / ◆ steady) for the focused trip — the "fare weather" metaphor (§5).
- **Data freshness** stays a quiet shell status dot (it already is, post-`REDESIGN`).

---

## 4. The three flagship moves in detail

### 4.1 The Trip Composer (the missing front door)

```
From  ( CHC ▾ )   To  ( CMB ▾ )      Leave  [ ~ early Sep ]   Trip  [ ~3 weeks ]
                                              └ flexible? ◉ ±3 days
```

- **Forgiving input.** Most buyers don't have exact dates — they have *"early
  September, about three weeks."* Faro already generates a dense grid of
  date×trip-length combos (`auto_generate`, ~1,900 itineraries in `explore.json`).
  So the composer should accept **fuzzy** input (month + trip-length + flex) and
  resolve to the best-covered nearby itinerary, surfacing *"cheapest in your
  window is Sep 4 → Sep 25."* This turns the existing finder data from a
  power-user table into the *primary* matching engine.
- **Honest fallback, not a dead end.** If the route/dates aren't covered yet, show
  the existing "warming up / still collecting" panel **with a concrete nudge**:
  *"I'm not watching CHC→DEL yet — here are the 4 routes I know cold,"* plus a
  one-tap "request this route" (opens a pre-filled GitHub issue / Telegram msg).
- **Deep-linkable.** `#/answer?o=CHC&d=CMB&dep=2026-09-04&ret=2026-09-25`. Now a
  verdict is **shareable** — the WhatsApp-from-your-cousin distribution channel
  that diaspora travel actually runs on. Pair with a generated OG image
  (`docs/og/CHC-CMB.png`, rendered at build time by `dashboard.py`) so the share
  preview *is* the verdict ("WAIT · fares should fall ~$180").

### 4.2 The Verdict, upgraded from "pill" to "instrument"

The shipped redesign already made a verdict panel — good. Push it further into a
decision *instrument*:

- **Add the missing dimension: WHEN.** BUY/WAIT/WATCH answers *what*; the buyer's
  real anxiety is *when*. Add a **best-buy window**: *"book within ~9 days"* with a
  subtle countdown. The model already produces a forward curve + drop probability;
  the window is the argmin of the forecast band before it turns up. This is the
  single most decision-useful number you're not surfacing.
- **Make confidence a feeling, not a percent.** "78%" is abstract. Render it as a
  short confidence *track* (■■■■□) with a plain gloss: *"fairly sure — based on 140
  observations and a backtest that called this route right 71% of the time."*
  Confidence + provenance in one breath = the trust moat, made visible.
- **"Why we think this" is one tap, always honest.** Collapsed by default; expands
  to the generated natural-language reasoning (`r.reason`, already exists) + the
  forecast fan. Progressive disclosure: the *answer* is loud, the *audit* is one
  tap, the *receipts* (backtest) are in the Lab.

### 4.3 The Watch — turn the dashboard into a tool that remembers

This is the biggest *product* gap and it's achievable with zero backend:

- **"Pin & watch this trip"** writes the trip to `localStorage` (`faro.trips[]`).
  The Watch view renders each as a live verdict card that re-resolves against the
  latest committed payload every visit. Faro now has *state* and a reason to
  return.
- **Alerts become the visitor's, not the maintainer's.** "Notify me when it's time
  to buy" offers three keyless paths:
  1. **Telegram deep-link** — `https://t.me/<bot>?start=<base64 route+dates>`. The
     existing `alerts.py` bot already speaks Telegram; extend it to register a
     per-user watch from the `/start` payload. (One bot, many watchers.)
  2. **Calendar `.ics`** — a generated "Check Faro for CHC→CMB" event on the
     best-buy date. Zero infra, works on every phone.
  3. **Web Push** — for the in-browser crowd; degrade gracefully where unsupported.
- **Why this matters:** fare tracking is inherently a *background* job. A web page
  you have to remember to revisit is the wrong shape for it. Pinning + push is the
  shape. It also creates the only retention loop the product currently lacks.

---

## 5. A unifying metaphor — **fare weather**

Faro already shows a live clock + weather in the nav. Lean all the way in: **treat
fares like a forecast**, because that's literally what the model produces and it's
the most intuitive mental model a non-expert has.

- **Today's outlook:** ▼ *Falling* / ▲ *Rising* / ◆ *Steady* — a persistent mood
  for the focused trip, in the shell.
- **The 7-day fare forecast** reads like a weather strip, not a Chart.js axis dump:
  small day cells, each tinted by expected price pressure, the buy-window cell
  haloed. (Same conformal data, friendlier encoding.)
- **The verdict copy adopts forecast voice:** *"Outlook: prices easing. Hold off —
  good chance of ~$180 off in the next fortnight. We'll flag the moment it bottoms."*

This isn't decoration — it's an *information-scent* upgrade. "Should I bring an
umbrella?" is a question everyone can act on in 2 seconds. "Should I book?" should
feel the same. It also differentiates Faro's voice from every sterile fare table.

---

## 6. Visual & interaction system — keep what shipped, add three things

The dark + gold Faro theme and the §3 token system from `REDESIGN.md` are a good
foundation; **don't redo them.** Three additions the new model needs:

1. **A "tool" chrome, not a "page" chrome.** A persistent top shell (brand · My
   trips · theme · outlook · about) that stays across the three hash-views, so it
   reads like an app you operate, not an article you scroll. Mobile: shell collapses
   to a bottom tab bar (Answer · Watch · Lab) — thumb-reachable, app-native.
2. **State-ful components.** Pinned/unpinned, watching/not-watching, expanded
   audit — the current design has no "selected/saved" affordances because nothing
   was ever savable. Define a saved-state visual (gold left-border + pin glyph).
3. **Skeletons over spinners.** With a 4.3 MB finder and Chart.js deferred, the Lab
   needs perceived-performance scaffolding: render the *shape* of each panel
   immediately, hydrate on reveal. (The `IntersectionObserver` reveal pattern is
   already there — extend it to skeleton→content.)

Everything else — type scale, semantic color (green buy / amber wait), two
elevations, motion discipline, `prefers-reduced-motion` — carries over unchanged.

---

## 7. Mobile-first & notification-first (the medium correction)

- **Design the phone first, let desktop be the spacious case** — the reverse of
  today. Maya arrives on a phone from a shared link; that's the canonical journey.
- **Bottom tab bar** (Answer · Watch · Lab) for thumb reach; verdict card is full-
  width and first; the composer is sticky at top.
- **One-screen answer.** On a 390px viewport the verdict + outlook + the two CTAs
  fit above the fold with nothing else competing. Evidence is a tap, not a scroll.
- **Notification-first** (the Watch, §4.3) is what makes a phone product *sticky*
  rather than a one-visit lookup.

---

## 8. Trust as a first-class surface (the real differentiator)

Promote honesty from footnote to feature, because it's the one thing Hopper/Google
won't say:

- **A persistent "how Faro knows" affordance** — one tap from the verdict opens a
  plain-language trust panel: data source + freshness, observation count for *this*
  route, the backtest hit-rate, and the unmissable *"informational — always confirm
  the live price."* Currently this is spread across "Data freshness," "Signal
  accuracy," and footer disclaimers; unify it.
- **Calibrated confidence shown as such** (§4.2): tie the % to *N observations* and
  *backtest accuracy* inline, so "78%" carries its provenance.
- **Never fake certainty on thin data.** The existing "still collecting" states are
  a UX asset — give them equal polish to the populated states, because an honest
  empty state is what earns the trust that makes the *full* verdict believable.

---

## 9. Performance & the 4.3 MB problem

- **Never ship `explore.json` (4.3 MB) to the Answer view.** It's Lab-only and must
  lazy-load on first entry to `#/lab` (the finder already fetches it on demand —
  make sure the new IA preserves that and never blocks Maya's path on it).
- **Split the payload by view.** Answer needs only `recs` + `focus` + `stats` +
  per-route summaries (tiny). Ship that inline; fetch everything else per-view.
- **Budget:** Answer view interactive in < 1s on a mid phone / 3G-ish; total
  Answer transfer < 120 KB. Defer Chart.js until the Lab. Self-host/subset fonts
  (already recommended). Target green Lighthouse on *the Answer view specifically*,
  since that's the one 95% of people see.
- **Pre-render the verdict into static HTML** (don't make Maya wait for JS to
  compute the hero). `dashboard.py` can emit the focused verdict as real markup;
  JS only enhances/swaps it when she changes the composer. This also fixes SEO and
  the OG-share preview (§4.1).

---

## 10. How we'd know it worked (success metrics)

Even on a static GitHub Pages site you can measure most of this with privacy-light
client events (or just reason about them):

| Goal | Signal |
|---|---|
| Answer-first works | % of sessions that view a verdict without scrolling; time-to-verdict |
| Composer adoption | % of sessions that change route/dates (vs bounce on default) |
| The Watch retains | # trips pinned; return-visit rate; alert opt-ins |
| Trust lands | audit-panel open rate; backtest views; outbound *Book* clicks (also the revenue metric) |
| Perf | Lighthouse on Answer view; Answer payload KB; finder no longer in critical path |

The North-Star: **outbound qualified Book clicks per visitor** — it aligns the
user's win (a confident decision) with the project's only revenue (affiliate CTAs
in `config.yaml`).

---

## 11. Phased rollout (each phase independently shippable, data layer untouched)

The generator is one file emitting one string; nothing here touches
`collect/predict/analytics` or the payload *shape*.

- **Phase A — Split the views.** Wrap existing sections into 3 hash-routed views
  (Answer / Watch-stub / Lab). Pure re-org of `add(...)` output; instant clarity
  win; Lab lazy-loads `explore.json`. *Lowest risk, high payoff.*
- **Phase B — Trip composer.** Add the fuzzy from/to/when input on the Answer view,
  resolving against the existing grid; deep-linkable URLs; honest fallback.
- **Phase C — Verdict as instrument.** Best-buy *window* + confidence-with-
  provenance + fare-weather outlook line. Promote `r.reason` to one-tap audit.
- **Phase D — The Watch.** `localStorage` "My trips," pin affordance, the three
  alert paths (Telegram deep-link, `.ics`, Web Push). Extend `alerts.py` to
  register per-user Telegram watches.
- **Phase E — Polish & perf.** Static-prerendered verdict + OG images, skeletons,
  payload split, mobile bottom-tab shell, Lighthouse pass.

**Quick wins (ship this week):** Phase A's view split + the verdict best-buy
*window* + a unified trust tap. They're cheap, they're the clearest "this got
better" moments, and they don't depend on the bigger bets landing first.

---

## 12. What I'd explicitly NOT do

- **Don't rebuild the visual system.** The dark+gold theme and tokens are done and
  fine. This revamp is IA, flows, state, and follow-through — not a re-skin.
- **Don't add a backend or a build step.** Every proposal here is client-side state
  + data Faro already commits + GitHub Pages. Cost stays $0; the "self-hostable in
  10 minutes" promise stays intact.
- **Don't touch scraping, the model, or `config.yaml`'s contract.** Presentation
  and product layer only.
- **Don't delete Sam's depth.** The Lab keeps every chart, the backtest, and the
  finder — it just stops being the first thing Maya sees.

---

### TL;DR for the busy reader

The current site is a beautifully-themed **console** that makes the visitor do the
analyst's job: pick a route, scroll 15 sections, find their situation, infer a
verdict. The revamp makes Faro do that job *for* the visitor and then *keep doing
it*: **tell it your trip → get one honest, well-dated verdict → pin it → get told
when to buy.** Three views (Answer / Watch / Lab), a trip composer as the front
door, the verdict upgraded with a *when*, a *fare-weather* voice, trust promoted to
a headline, and a phone-first, notification-first shape — all still a single static
file at $0. Same engine, same data, same honesty — finally pointed at the person
who actually showed up.
