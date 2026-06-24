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

Nothing here touches the data, scraping, or model layers — this is a proposal for
the presentation layer only.
