"""
EXPERIMENTAL, OPT-IN fare source: Singapore Airlines' own website
(singaporeair.com), scraped with a real headless browser.

Why this exists
---------------
The default fare source is Google Flights (`provider.py`), which is what powers
every scheduled scan. This module is a *second*, opt-in source that goes straight
to Singapore Airlines' public **Best Fare Finder** so we can track SQ's OWN
published return fares for a route -- the genuine "Singapore Airlines flying the
whole trip" price, end to end on SQ metal, rather than the cheapest connecting
combo Google surfaces.

Why it is OFF by default
------------------------
singaporeair.com is a heavy single-page app behind aggressive bot protection
(Akamai). Scraping it is markedly more fragile than Google Flights and is very
likely to need selector tuning against a live run. So this module is deliberately
**not wired into the scheduled scan** (`collect.py` only calls `provider.py`):
that keeps the free, autonomous Google-Flights pipeline rock-solid. You opt in
explicitly, route by route, and tune it with:

    python -m flightwatch sq-diag CHC CMB 2026-09-05 2026-09-26

which prints what it found and drops a screenshot + HTML into ``debug/`` so the
selectors below can be adjusted to whatever SQ is currently serving.

Contract
--------
``search_sq_offers(...)`` returns the SAME normalised offer shape as
``provider.search_flight_offers`` -- a list of
``{"price", "currency", "airline", "stops", "duration_minutes"}`` dicts -- so the
rest of FlightWatch (storage, analytics, dashboard) consumes it unchanged. Every
offer is tagged ``airline="Singapore Airlines"`` because, by construction, these
come from SQ's own site.
"""

import os
import re
import time

from . import provider

# SQ's Best Fare Finder results page. The exact path/markup changes over time --
# this is the documented starting point; tune in sq-diag against a live page.
_SQ_BFF_URL = (
    "https://www.singaporeair.com/en_UK/sg/plan-travel/special-fares/best-fare/"
    "?journeyType=round-trip&origin={o}&destination={d}"
    "&departureDate={dep}&returnDate={ret}&adult=1&cabinClass=Y"
)

# Fare numbers on the page are rendered next to a currency code/symbol. We anchor
# on that so a stray standalone number (a flight count, a duration) isn't mistaken
# for a price. Defensive: matches "SGD 1,234", "NZD 2,345", "$1,234".
_PRICE_RE = re.compile(r"(?:SGD|NZD|AUD|USD|S?\$)\s*([0-9][0-9,]{2,7})", re.I)


def _build_url(origin, destination, depart_date, return_date):
    return _SQ_BFF_URL.format(o=origin.upper(), d=destination.upper(),
                              dep=depart_date, ret=return_date)


# Pull candidate return fares out of the rendered DOM. Kept intentionally broad
# (price-anchored text scan) because SQ's class names churn; tune to taste.
_EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const rx = /(?:SGD|NZD|AUD|USD|S?\$)\s*([0-9][0-9,]{2,7})/i;
  // Candidate fare containers: anything that looks like a fare cell/card.
  const nodes = document.querySelectorAll(
    '[class*="fare" i],[class*="price" i],[data-price],td,li,button');
  for (const n of nodes) {
    const t = (n.innerText || n.textContent || '').trim();
    if (!t) continue;
    const m = t.match(rx);
    if (!m) continue;
    const price = parseInt(m[1].replace(/,/g, ''), 10);
    if (!price || price < 200 || price > 99999) continue;   // sane fare window
    if (seen.has(price)) continue;
    seen.add(price);
    let stops = 0;
    const sm = t.match(/(\d+)\s*stop/i);
    if (sm) stops = parseInt(sm[1], 10);
    else if (/non-?stop|direct/i.test(t)) stops = 0;
    let minutes = 0;
    const dm = t.match(/(\d+)\s*h(?:r|ours?)?\s*(\d+)?\s*m/i);
    if (dm) minutes = parseInt(dm[1], 10) * 60 + (dm[2] ? parseInt(dm[2], 10) : 0);
    out.push({price, stops, duration_minutes: minutes});
  }
  return out;
}
"""

_SQ_READY_JS = ("() => /(?:SGD|NZD|AUD|USD|S?\\$)\\s*[0-9]/.test"
                "(document.body ? document.body.innerText : '')")


def _wait_for_fares(page, timeout_ms=40000):
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if page.evaluate(_SQ_READY_JS):
                return "ok"
        except Exception:
            pass
        if provider._is_blocked(page):
            return "blocked"
        page.wait_for_timeout(700)
    return "empty"


def _scrape(url, fingerprint=None, debug_tag=None):
    """Open SQ's Best Fare Finder and pull candidate return fares from the DOM.

    Reuses provider.py's fingerprint pool, stealth script and resource blocker so
    we present the same realistic Chrome/Firefox mix and stay light."""
    from playwright.sync_api import sync_playwright
    import random

    fp = fingerprint or random.choice(provider._FINGERPRINTS)
    headless = provider.headless_mode()
    engine = provider._engine_of(fp)
    vw, vh = fp["viewport"]
    offers = []
    with sync_playwright() as p:
        launcher = getattr(p, engine)
        browser = launcher.launch(**provider._launch_kwargs(engine, headless))
        ctx = browser.new_context(
            user_agent=fp["ua"], locale=fp["locale"], timezone_id=fp["tz"],
            viewport={"width": vw, "height": vh},
            extra_http_headers={"Accept-Language": fp["lang"]},
        )
        provider._install_blocker(ctx)
        ctx.add_init_script(provider._stealth_js(fp))
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            provider._dismiss_consent(page)
            if _wait_for_fares(page) == "ok":
                page.wait_for_timeout(1200)          # let the fare grid settle
                offers = page.evaluate(_EXTRACT_JS) or []
            if not offers and debug_tag:
                provider._save_debug(page, debug_tag)
        finally:
            browser.close()
    return offers


def search_sq_offers(origin, destination, depart_date, return_date,
                     currency="NZD", max_offers=50, retries=2):
    """Scrape Singapore Airlines' own return fares for one itinerary.

    Returns the SAME normalised shape as provider.search_flight_offers, with every
    offer tagged airline="Singapore Airlines". Raises nothing on an empty result
    (returns []) -- SQ may simply not publish a fare for that pair, or the page
    needs selector tuning (run sq-diag to inspect)."""
    url = _build_url(origin, destination, depart_date, return_date)
    cur = (currency or "NZD").upper()
    tag = f"sq-{origin}-{destination}-{depart_date}"
    fps = provider._attempt_fingerprints(retries)

    raw = []
    for attempt in range(max(retries, 1)):
        try:
            raw = _scrape(url, fingerprint=fps[attempt],
                          debug_tag=tag if attempt == retries - 1 else None)
            if raw:
                break
        except Exception as e:
            print(f"  SQ ERR {origin}->{destination} {depart_date}: {e}")
        time.sleep(2 * (attempt + 1))

    for o in raw:
        o["airline"] = "Singapore Airlines"
    return provider._normalize_offers(raw, cur, max_offers)


def diagnose(origin="CHC", destination="CMB",
             depart_date="2026-09-05", return_date="2026-09-26"):
    """Verbosely scrape ONE itinerary off singaporeair.com and always save debug.

    Run: python -m flightwatch sq-diag [ORIGIN DEST DEPART RETURN]
    Inspect debug/sq-*.png / .html to tune the selectors above for the live page.
    """
    url = _build_url(origin, destination, depart_date, return_date)
    print("Singapore Airlines Best Fare Finder -- EXPERIMENTAL scrape")
    print(f"Route: {origin}->{destination}  {depart_date} -> {return_date}")
    print(f"URL:   {url}\n")
    raw = _scrape(url, debug_tag=f"sq-{origin}-{destination}")
    print(f"Scraped {len(raw)} candidate SQ fares.")
    for o in raw[:20]:
        print(f"  {o.get('price')}  stops={o.get('stops')}  "
              f"dur={o.get('duration_minutes')}m")
    if not raw:
        print("\nNo fares parsed. SQ likely served a bot wall or changed its markup.")
    print(f"\nDebug screenshot + HTML saved under: {provider.DEBUG_DIR}")
