# Faro redesign proposal

A complete, ground-up redesign proposal for the Faro dashboard (the page generated
by `flightwatch/dashboard.py` into `docs/index.html`).

- **[`REDESIGN.md`](./REDESIGN.md)** — the full proposal: critique of the current
  site, design concept, visual system (tokens, type, spacing, motion), new
  information architecture, a section-by-section component library, accessibility &
  performance, and a staged implementation plan against `dashboard.py`.
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
