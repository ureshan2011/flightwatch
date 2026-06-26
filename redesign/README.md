# Faro redesign proposal

A complete, ground-up redesign proposal for the Faro dashboard (the page generated
by `flightwatch/dashboard.py` into `docs/index.html`).

- **[`REDESIGN.md`](./REDESIGN.md)** — the full proposal: critique of the current
  site, design concept, visual system (tokens, type, spacing, motion), new
  information architecture, a section-by-section component library, accessibility &
  performance, and a staged implementation plan against `dashboard.py`.
- **[`UX_REVAMP.md`](./UX_REVAMP.md)** — a second, product-level pass that goes
  beyond the visual refresh: reframes Faro from an analyst's console into a personal
  "tell me your trip → get one verdict → pin it → get told when to buy" answer
  machine (trip composer, best-buy window, a pinnable Watch, a 3-view app, fare-
  weather voice) — all still a single static file at $0.
- **[`../docs/redesign/answer.html`](../docs/redesign/answer.html)** — a
  self-contained, **deep-linkable** runnable prototype of the new **Answer view**
  from `UX_REVAMP.md`: trip composer, the upgraded verdict (best-buy window +
  confidence-with-provenance), the fare-weather forecast strip, a pin-to-watch
  action, and light/dark. Because it lives under `docs/`, GitHub Pages serves it at
  `…github.io/<repo>/redesign/answer.html?o=CHC&d=CMB&dep=2026-09-04&len=21`.
  Sample data; a prototype, not the live app.
- **[`mockup.html`](./mockup.html)** — a self-contained, runnable prototype of the
  new direction (verdict band, market strip, routes board, restyled charts,
  heatmap) in both light and dark mode. Open it directly in a browser — no build
  step. Uses representative **sample** data; it is a design prototype, not the live
  app.
- **[`faro-reference.dc.html`](./faro-reference.dc.html)** — the authoritative
  **Claude Design** mockup for Faro (dark canvas, single gold accent `#E7B25A`,
  Sora + JetBrains Mono). This is the visual source of truth the live dashboard's
  theme now follows. Open alongside `support.js` (same folder) to render it.
- **[`DATA_SOURCE.md`](./DATA_SOURCE.md)** — recommendation to move the fare feed
  from the fragile Google Flights scrape to an affiliate-program API (Travelpayouts
  / Kiwi Tequila) so the price feed and the booking link become one pipe.

The visual identity from `faro-reference.dc.html` has been applied to the live
generator (`dashboard.py`): the dashboard is now the Faro dark + gold theme. The
data, scraping, and model layers are unchanged.
