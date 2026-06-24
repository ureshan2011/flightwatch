# Faro — Complete Website Redesign Proposal

> A full, ground‑up redesign of the Faro dashboard (`flightwatch/dashboard.py` →
> `docs/index.html`). This proposes a new design *direction*, information
> architecture, visual system, and component set for the **entire** site — not a
> single section — plus a concrete, runnable mockup (`redesign/mockup.html`) and a
> staged implementation plan against the existing generator.

---

## 0. TL;DR

Faro already does something genuinely impressive: it scrapes a real fare dataset,
trains a forecast, and answers *"should I book now or wait?"* with a calibrated
confidence. The problem is the **packaging**. The current page is a maximalist
landing page — aurora blobs, floating animation, 3D‑tilt on every card, three
overlapping gradients, ~15 stacked sections — and the one thing a visitor came for
(the decision) competes for attention with decoration.

**This redesign reframes Faro from a "landing page that happens to show data" into
a "decision tool that happens to be beautiful."** The new direction is calm,
editorial, and data‑first — closer to Linear / Mercury / a Bloomberg terminal than
to a SaaS marketing splash. One confident answer at the top, the evidence beneath
it on demand, a real design system underneath, and a proper dark mode.

Three measurable goals:

1. **Time‑to‑answer < 2s** — the buy/wait verdict for the focused route is the
   first and largest thing on the page, legible without scrolling.
2. **One coherent system** — tokens, type scale, spacing grid, and a documented
   component library replace ~650 lines of ad‑hoc CSS.
3. **Lighter & faster** — drop the always‑running blur/tilt/blob animations,
   self‑host or subset fonts, and lazy‑mount below‑fold sections.

---

## 1. Understanding the current site

### 1.1 What it is
A single generated `index.html` (~370 KB) produced by `dashboard.py::_html()`. All
markup, ~650 lines of CSS, and ~900 lines of vanilla JS are inlined; a JSON
`payload` is embedded as `const D = …`, and the page renders itself client‑side.
Chart.js comes from a CDN; airline logos from `pics.avs.io`; fonts from Google
Fonts (`Sora`, `IBM Plex Mono`).

### 1.2 Current information architecture (top → bottom)
| # | Section | Scope | Purpose |
|---|---------|-------|---------|
| — | Sticky nav | global | brand, anchor links, live clock + weather |
| — | Route switcher | global | focus the page on one corridor |
| 1 | Hero | global | headline + 3D "best deal" card |
| 2 | Bento grid | global | live "buy index" gauge + dataset stats + carrier spotlight |
| 3 | Highlights | all routes | "today across every route" |
| 4 | Routes we track | all routes | corridor overview cards |
| 5 | Data freshness | global | scrape status / next scan |
| 6 | Price trends & airlines | flagship | history chart |
| 7 | When to book | flagship | forecast fan chart |
| 8 | Cheapest day to fly | flagship | weekday heatmap |
| 9 | What changed | flagship | diff since last scan |
| 10 | Signal accuracy | flagship | backtest |
| 11 | Market insights | per‑route | latest prices table |
| 12 | Latest fares | per‑route | cheapest per route |
| 13 | Today's signals | per‑route | buy/wait cards |
| 14 | Find a trip | data | filterable date‑combo finder |
| 15 | Market analytics | all routes | cross‑route patterns |
| — | Footer | global | sources, affiliate disclosure, author |

### 1.3 What works (keep the substance)
- **The decision engine is the product.** BUY/WAIT/WATCH + confidence + forecast
  band + expected savings is a real, defensible value proposition.
- **The route‑scope model** (`SCOPE`, `.sscope[data-for]`, `.rsec`) is smart: pick a
  corridor and the whole page focuses, with "still collecting" routes degrading to
  an empty‑state panel instead of showing another route's data. *Keep this whole
  concept* — only its presentation changes.
- **Honest empty/collecting states** already exist everywhere. Rare and valuable.
- **Real logos, stops, durations, live FX/weather** make it feel trustworthy.

### 1.4 What the redesign fixes (the critique)
1. **Decoration outweighs signal.** Aurora blobs (two `filter:blur(84px)` elements
   animating forever), per‑card 3D tilt, pointer‑follow glow, animated gauges, and
   three radial gradients all run at once. It's *busy*, and it taxes low‑end phones.
2. **No clear visual hierarchy.** Hero card, bento pulse, and carrier spotlight are
   all loud, glossy, similarly‑sized tiles — nothing wins. The eye has no path.
3. **The actual verdict is small.** "Should I book?" is the whole point, yet on load
   it's one pill inside a tilting card next to six other shiny tiles.
4. **Gradient overload.** `--brand → --brand2 → --teal` appears on the wordmark,
   buttons, the "on" route pill, headline text, deal card, and mark. Gradients stop
   meaning anything when everything is one.
5. **15 flat sections, equal weight.** It's a long scroll with no rhythm,
   chaptering, or "you are here." Important (forecast) and minor (data freshness)
   read at the same volume.
6. **No dark mode** — unusual for a data/finance tool and a real comfort gap.
7. **System is implicit.** Spacing, radii (`13/14/18/22/24px` all appear), and font
   sizes are hand‑tuned per component. Hard to extend; a new section means new CSS.
8. **Accessibility gaps.** Gradient‑clipped text, thin `--dim #97a0b6` on `#f5f7fc`
   (≈2.3:1, fails WCAG AA), motion with no `prefers-reduced-motion` retreat,
   tilt/glow that aren't keyboard‑reachable.

None of this is a knock on the engineering — it's a maturity step from "look what it
can do" to "here's your answer."

---

## 2. Design concept

### Concept: **"The Decision, then the Evidence."**

Faro is an analyst that has already done the work. The page should feel like it
hands you a verdict with quiet confidence, then lets you *audit* that verdict as
deeply as you want. Editorial calm on top; rich, dense data on demand below.

Reference points: **Linear** (restraint, type, motion discipline), **Mercury /
Ramp** (financial trust, generous white space, one accent), **FlightyApp** (a
travel product that feels like an instrument, not a brochure), and the *information
density* of a Bloomberg terminal without its visual noise.

### Five principles
1. **Answer first.** The single most useful sentence — *"Book now"* or *"Wait,
   fares should fall ~$180"* — is the largest element on the page and visible
   without scrolling, for the focused route.
2. **One accent, earned.** A single brand accent (Faro blue). Color is reserved for
   *meaning*: green = buy/cheap, amber = wait, slate = watch. Decorative gradients
   are removed.
3. **Progressive depth.** Verdict → key numbers → chart → full table → raw finder.
   Each layer is opt‑in. Nothing dense above the fold.
4. **Motion with intent.** Animation only to explain change (a value counting to its
   reading, a chart drawing in once on reveal). No perpetual ambient motion. Full
   `prefers-reduced-motion` path.
5. **Honest by default.** Confidence, coverage, "collecting…", and "informational
   only" stay first‑class — trust is the product.

---

## 3. Visual system

A small set of tokens drives everything. These map cleanly onto CSS custom
properties so `dashboard.py` keeps a single `:root` block (now with a `[data-theme]`
override), and every component reads from tokens instead of literals.

### 3.1 Color

**Light (default)**
```
--bg            #fbfcfe   page
--surface       #ffffff   cards
--surface-2     #f4f6fb   insets, table stripes, chips
--border        #e7ebf3   hairlines
--border-strong #d6dced
--text          #0c1424   primary
--text-2        #4a5570   secondary
--text-3        #707a93   tertiary (AA‑safe on --bg: ≥4.5:1)
--accent        #2f6bff   single brand
--accent-ink    #1b3fa8   accent text on light
```

**Dark**
```
--bg            #0a0e17
--surface       #121826
--surface-2     #1a2233
--border        #232c40
--border-strong #2e3a52
--text          #eef2fb
--text-2        #aab4cc
--text-3        #7d889f
--accent        #5b8cff
--accent-ink    #b9ccff
```

**Semantic (shared, tuned per theme)**
```
--buy   #0fa371  / buy-bg  #e6f7f0 (dark: #0f2a22)
--wait  #d98a16  / wait-bg #fdf2dd (dark: #2c2412)
--watch #5b6981  / watch-bg#eef1f7 (dark: #1c2436)
--down  #0fa371   (price falling = good)
--up    #e0567d   (price rising = bad)
```

Decisions:
- **Kill the three‑stop gradient.** The wordmark and primary button use solid
  `--accent`. The only gradient retained is a *subtle* one‑hue surface sheen on the
  single hero verdict card — and even that is optional.
- **Color = meaning.** Green/amber/slate exclusively encode buy/wait/watch and
  price direction, so a glance reads as data, not decoration.
- **AA everywhere.** Replace `--dim #97a0b6` (fails) with a tertiary that clears
  4.5:1; verify all semantic chips.

### 3.2 Typography
- **Display / UI:** `Inter` (or keep `Sora` for headlines only) — self‑hosted/subset
  to Latin to cut the render‑blocking Google Fonts round trip.
- **Numerals / data:** `IBM Plex Mono` (kept) for every price, code, and metric —
  tabular figures so columns align.
- **Type scale** (1.20 ratio, rem‑based):
  ```
  display  44 / 1.04  (hero verdict number)
  h1       33 / 1.10
  h2       24 / 1.15  (section titles)
  h3       19 / 1.25
  body     15.5/ 1.55
  small    13 / 1.45
  micro    11.5 (mono labels, letter-spacing .12em, uppercase)
  ```
  `clamp()` only on the hero number. Everything else uses fixed steps for rhythm.

### 3.3 Spacing, grid, radius, elevation
- **4px base spacing scale:** `4 8 12 16 24 32 48 64 96`. Section vertical rhythm =
  `64` desktop / `40` mobile. No more bespoke margins.
- **Grid:** 12‑col, max‑width `1120px`, `24px` gutters. Bento becomes a disciplined
  `repeat(12, …)` span system rather than `auto‑fit minmax`.
- **Radius tokens:** `--r-sm 10 / --r-md 14 / --r-lg 20 / --r-pill 999`. Three radii,
  used consistently (currently five+ float around).
- **Elevation — two levels only:**
  `--e1: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.05)` (resting card)
  and `--e2: 0 8px 24px -8px rgba(16,24,40,.18)` (hover / hero). No 34px‑spread
  drop shadows.

### 3.4 Motion
- **One reveal:** `opacity 0→1, translateY 8px→0`, 320ms `cubic-bezier(.2,.7,.2,1)`,
  fired once by `IntersectionObserver`. (Reuse the existing `.reveal` observer.)
- **Counters** keep their ease‑out count‑up (it explains the number).
- **Charts** draw once on first reveal, then hold.
- **Removed:** floating blobs, perpetual gauge spin, per‑card 3D tilt, pointer‑glow.
- `@media (prefers-reduced-motion: reduce)` → all transitions ≤ 1ms, no count‑up,
  charts render in final state.

### 3.5 Iconography & logos
- One stroke‑icon set (1.75px, `currentColor`) — reuse the existing inline SVG
  paths, normalized to a 24‑grid. No emoji in structural UI (✈ 🛫 ⏱ become icons;
  emoji may stay only in prose/footer).
- Airline logos keep the `pics.avs.io` + initials‑fallback `avatar()` — it already
  degrades well; just standardize sizes to `20 / 24 / 32`.

---

## 4. New information architecture

Same data, re‑sequenced into **four chapters** with a persistent left rail (desktop)
/ segmented control (mobile) so "you are here" is always visible. The route scope
model is unchanged underneath.

```
┌ NAV  Faro · [route switcher inline] · clock/wx · ☀/☾ theme · Book
│
├ CHAPTER 1 — THE VERDICT        (was: hero + bento, fused)
│   • Verdict panel: BIG signal + one‑sentence reason + confidence
│   • Three "evidence chips": current low · forecast low · expected move
│   • Primary CTA (Book) + compare row
│   • Live market strip: buy index, fare‑value, momentum, buy/wait/watch mix
│
├ CHAPTER 2 — THE EVIDENCE       (was: trends, plan, heatmap, what‑changed, backtest)
│   • Price history (route's own curve)
│   • Forecast fan + best‑time‑to‑book
│   • Cheapest‑day heatmap
│   • What changed + anomalies
│   • Signal accuracy (backtest) — collapsible "show the receipts"
│
├ CHAPTER 3 — THE MARKET         (was: highlights, routes, insights, fares, market analytics)
│   • Routes board: every corridor, one row each, sortable
│   • Latest fares / market insights as one unified table
│   • Cross‑route analytics
│
├ CHAPTER 4 — FIND YOUR TRIP     (was: finder)
│   • Filter rail + cheapest‑by‑day calendar + result cards
│
└ FOOTER  sources · honest disclaimer · affiliate disclosure · author · data link
```

Key moves:
- **Hero + bento merge** into one "Verdict + Market" band. Today they're two loud,
  competing blocks; together they answer "what should I do *and* what's the market
  doing" in one screen.
- **All flagship deep‑dives gather under "The Evidence,"** clearly the audit layer —
  so they stop interrupting the market overview.
- **"Routes we track," "Highlights," "Latest fares," "Market insights"** collapse
  into one **Routes board** + one unified fares table (today they overlap heavily).
- **Data freshness** demotes to a quiet status line in the nav/footer, not a full
  section.
- **Left rail / segmented nav** gives the long page a spine and a progress sense.

---

## 5. Component library (the redesign in parts)

Each component is defined so `dashboard.py` can emit it from the same `payload`.

### 5.1 Verdict panel — *the* hero (replaces hero card + carrier spotlight)
The single most important component. For the focused route:
```
┌─────────────────────────────────────────────┐
│  CHC → CMB · 1 Sep – 22 Sep · 21 nights      │  ← context line (mono, micro)
│                                              │
│   WAIT                          ▢ 78% conf.  │  ← signal: 40px, semantic color
│   Fares should fall ~NZD 180 in ~2 weeks.    │  ← one plain sentence
│                                              │
│   NZD 2,140        NZD 1,960       −8%        │  ← 3 evidence chips
│   fare now         forecast low    expected  │
│                                              │
│   [  Book on Aviasales  →  ]  compare ▾      │  ← CTA + compare row
└─────────────────────────────────────────────┘
```
- Signal word is the visual anchor (green BUY / amber WAIT / slate WATCH).
- The reason sentence is generated already (`r.reason`), just promoted.
- One *optional* faint surface sheen, no tilt, no perpetual glow.
- On a still‑collecting route it becomes the existing collect panel, restyled.

### 5.2 Market strip (replaces bento pulse gauge)
A single horizontal strip, not a tile: `Buy index 64/100 · Fare value 58 · Since
last check −2% · 3 buy · 5 wait · 2 watch` with a thin segmented mix bar. The
"buy index" keeps its count‑up. Calmer than the current circular gauge tile; reads
left‑to‑right like a ticker.

### 5.3 Stat rail (replaces 6 tilting bento tiles)
Demote dataset vanity stats (fare records, routes, airlines, scans, fastest) to a
**single quiet inline rail** under the verdict — small mono numbers separated by
dots, count‑up preserved. They're proof‑of‑rigor, not headlines, so they should
*support* the verdict, not rival it.

### 5.4 Routes board (replaces "Routes we track" + "Highlights")
One sortable table, one row per corridor:
`Route · cheapest now · signal chip · 30‑day spark · best month · →focus`. Clicking a
row calls the existing `setScope()`. Replaces two separate card grids that show
overlapping info.

### 5.5 Forecast & history charts
Keep Chart.js, restyle to the token palette: hairline grid, mono tick labels,
semantic fan band (the conformal interval), single accent line. Add a clear "best
time to book" marker. Draw‑in once on reveal.

### 5.6 Heatmap
Cheapest‑day grid restyled to a 5‑step monochrome‑accent ramp (not rainbow) with a
legend and accessible cell labels (`aria-label="Tue dep / Sun ret — NZD 1,980"`).

### 5.7 Signal cards (Today's signals)
Tighten to a uniform card: dates lead, route chip secondary (already the logic),
signal + confidence, the forecast micro‑line, stops/duration/airline, CTA. Remove
per‑card tilt; hover = `--e2` lift only.

### 5.8 Trip finder
Keep the lazy‑loaded `explore.json` finder and its filter logic. Restyle: filter
rail as a left sidebar on desktop, calendar heatmap reusing 5.6's ramp, result
cards reusing 5.7. This is the power‑user layer and stays at the bottom.

### 5.9 Table & chip primitives
Define one `<.table>` (mono numerics, striped `--surface-2`, sticky header) and one
`<.chip>` (sizes sm/md, variants neutral/accent/buy/wait/watch) and reuse them
across insights, fares, routes board, and finder — today each section rolls its own.

### 5.10 Nav + theme toggle + route switcher
Slim nav: wordmark (solid accent mark), the route switcher pulled *inline* (it's
currently a second sticky bar — fold it in to reclaim vertical space), live
clock/weather, a **theme toggle**, and a persistent **Book** button. Mobile: nav
collapses, chapters become a sticky segmented control.

---

## 6. Responsive & mobile
- **Single‑column < 720px.** Verdict panel full‑width and first. Market strip wraps
  to two rows. Stat rail scrolls horizontally.
- **Left rail → sticky segmented control** (Verdict · Evidence · Market · Find) at
  the top of the viewport on scroll.
- **Charts** get a min‑height and horizontal scroll rather than squashing.
- **Tap targets ≥ 44px**; route switcher stays a horizontal scroller (already is).
- Test matrix: 360 / 390 / 768 / 1024 / 1280.

## 7. Accessibility
- WCAG **AA** contrast on all text/!chips (fix `--dim`, gradient‑clipped headline →
  solid).
- Real landmarks: `<header> <nav> <main> <section aria-labelledby> <footer>`.
- Every chart has a `<figure>`/`<table>` text equivalent (the data is already in
  `D`); heatmap cells carry `aria-label`s.
- Full keyboard path; visible `:focus-visible` ring (`2px --accent`); no
  hover‑only information.
- `prefers-reduced-motion` and `prefers-color-scheme` both honored;
  theme toggle persists to `localStorage`.

## 8. Performance
- **Remove** the two animating blur blobs, tilt transforms, and pointer‑glow
  listeners (constant compositor work on scroll).
- **Self‑host + subset fonts** (Latin), `font-display:swap` — kills two render‑
  blocking Google requests.
- **Defer Chart.js** and only instantiate a chart when its section first reveals
  (the finder already lazy‑loads `explore.json`; extend the pattern).
- Keep the page a single self‑contained file (great for GitHub Pages); target
  < 250 KB HTML and a green Lighthouse perf/a11y.

---

## 9. Implementation plan (against `dashboard.py`)

The generator is one file producing one string, so the redesign ships
incrementally without touching the data layer (`collect/predict/analytics`).

**Phase 0 — Tokens (no visual risk).** Replace the `:root` literals with the token
set in §3, add a `[data-theme="dark"]` block and the toggle. Map old vars to new so
nothing breaks. *Ship dark mode alone first — instant, visible win.*

**Phase 1 — Calm the canvas.** Delete aurora blobs, tilt, and pointer‑glow CSS/JS;
collapse to two elevation levels and three radii; wire `prefers-reduced-motion`.
Page looks the same structurally but reads twice as calm.

**Phase 2 — The Verdict band.** Build the verdict panel (§5.1) + market strip (§5.2)
+ stat rail (§5.3), replacing `hero` + `renderBento()`. Highest‑impact change.

**Phase 3 — Re‑chapter the body.** Wrap the existing `add(...)` sections into the
four chapters (§4), add the left rail / segmented nav, merge Routes/Highlights into
the routes board (§5.4) and the two fares tables into one.

**Phase 4 — Restyle data viz.** Charts, heatmap, finder, signal cards to the token
system and shared table/chip primitives (§5.5–5.9).

**Phase 5 — A11y & perf pass.** Landmarks, chart text equivalents, font subsetting,
deferred Chart.js, Lighthouse.

Each phase is independently shippable and testable; the `tests/test_dashboard.py`
suite guards the data contract throughout.

## 10. Risks & non‑goals
- **Non‑goal:** changing scraping, the model, route config, or the `payload` shape.
  This is purely the presentation layer.
- **Risk:** the verdict band leans on `recs`/`focus` always being populated — keep
  the existing graceful fallbacks (`focusTile()` already returns `''` cleanly).
- **Risk:** removing beloved motion. Mitigated by phasing — dark mode + calm canvas
  land first and tend to win people over before the structural changes.

## 11. The mockup
`redesign/mockup.html` is a **self‑contained, runnable** prototype of the new
direction with representative sample data: the new verdict band, market strip, stat
rail, routes board, restyled history chart, and footer — in both **light and dark**
(toggle top‑right). Open it directly in a browser; no build step. It demonstrates
the system in §3–§5 so the proposal is tangible rather than abstract.

---

*Prepared for the `claude/website-redesign-proposal` branch. The substance of Faro —
a free, honest, self‑hosted "buy or wait" engine — is strong; this redesign is about
giving that substance the clarity and confidence it deserves.*
