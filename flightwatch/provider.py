"""
Flight fare provider: Google Flights, scraped with a real headless browser.

Why a browser and not an HTTP client?
  Google Flights serves its fare list through internal XHR ("batchexecute") calls
  that run *after* the page loads, and it fingerprints/loads differently for plain
  HTTP clients -- which is why lightweight scrapers (and the Travelpayouts cache)
  came back empty for thin routes like CHC <-> CMB. Driving an actual Chromium via
  Playwright renders the page the way a person's browser would, so the results
  actually populate. It needs no API key and no paid proxy, which keeps FlightWatch
  free and fully autonomous.

How it works:
  1. `fast_flights` builds Google's `?tfs=` Protobuf query (origin/dest/dates/seat),
     which is the hard part of forming a valid Google Flights URL.
  2. Playwright opens that URL with currency + region forced, clears the consent
     wall if present, waits for the fare rows, and reads prices from the DOM.
  3. We parse prices from stable ARIA labels ("... round trip total") rather than
     Google's churning CSS class names, so it survives cosmetic redesigns.

On failure it writes a screenshot + HTML to `debug/` (gitignored) so a CI run can
be inspected and selectors tuned without guessing.

The rest of the project only depends on `search_flight_offers` and
`cheapest_offer` returning normalised dicts, so this file is the only thing to
touch when swapping fare sources.
"""

import os
import re
import time

from . import ROOT_DIR

DEBUG_DIR = os.path.join(ROOT_DIR, "debug")

# A normal desktop Chrome UA -- headless Chromium otherwise advertises itself.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

# Hide the most obvious automation tell before any page script runs.
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

# JS run inside the page to pull fare rows out of the DOM. Returns a list of
# {price, airline, stops, duration} dicts. Anchored on ARIA labels for stability.
_EXTRACT_JS = r"""
() => {
  const out = [];
  // Each fare row is an <li> that contains a "... round trip total" aria-label.
  const rows = [...document.querySelectorAll('li')].filter(li =>
    li.querySelector('[aria-label*="trip total" i]'));
  for (const li of rows) {
    const label = li.querySelector('[aria-label*="trip total" i]');
    const aria = (label && label.getAttribute('aria-label')) || '';
    // First sizeable number in the label is the price (e.g. "... 2,310 ...").
    const pm = aria.replace(/,/g, '').match(/(\d{2,7})/);
    if (!pm) continue;
    const price = parseInt(pm[1], 10);

    const text = li.innerText || '';
    // Stops: "Nonstop" or "1 stop" / "2 stops".
    let stops = 0;
    const sm = text.match(/(\d+)\s+stop/i);
    if (sm) stops = parseInt(sm[1], 10);
    else if (/nonstop/i.test(text)) stops = 0;
    // Duration: "19 hr 15 min" / "19h 15m".
    let minutes = 0;
    const dm = text.match(/(\d+)\s*hr(?:\s*(\d+)\s*min)?/i) ||
               text.match(/(\d+)\s*h\s*(\d+)?\s*m/i);
    if (dm) minutes = parseInt(dm[1], 10) * 60 + (dm[2] ? parseInt(dm[2], 10) : 0);
    // Airline: first non-empty line that isn't a time/price/duration.
    let airline = '';
    for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
      if (/^\$|\d{1,2}:\d{2}|hr|min|stop|nonstop|round trip|select/i.test(line)) continue;
      if (line.length >= 2 && line.length <= 40) { airline = line; break; }
    }
    out.push({price, airline, stops, duration_minutes: minutes});
  }
  return out;
}
"""


def _build_url(origin, destination, depart_date, return_date, adults, currency, market):
    from fast_flights import create_query, FlightQuery, Passengers
    q = create_query(
        flights=[
            FlightQuery(date=depart_date, from_airport=origin, to_airport=destination),
            FlightQuery(date=return_date, from_airport=destination, to_airport=origin),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=max(int(adults or 1), 1)),
        currency=(currency or "NZD").upper(),
        language="en-US",
    )
    # gl pins the Google region so the currency/market matches the route.
    return q.url() + "&gl=" + (market or "nz").lower()


def _dismiss_consent(page):
    """Click through Google's cookie/consent wall if it appears."""
    for sel in ("button[aria-label*='Accept all' i]",
                "button:has-text('Accept all')",
                "button:has-text('I agree')",
                "form[action*='consent'] button"):
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass


def _save_debug(page, tag):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{tag}.png"), full_page=True)
        with open(os.path.join(DEBUG_DIR, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def _scrape(url, debug_tag=None):
    """Open the Google Flights URL in Chromium and return a list of raw offers."""
    from playwright.sync_api import sync_playwright

    headless = os.environ.get("FLIGHTWATCH_HEADFUL", "") != "1"
    offers = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=_UA, locale="en-US", timezone_id="Pacific/Auckland",
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            _dismiss_consent(page)
            # Wait until at least one fare row appears, or give up after a while.
            try:
                page.wait_for_function(
                    "() => !!document.querySelector('[aria-label*=\"trip total\" i]')",
                    timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(1500)  # let the list settle
            offers = page.evaluate(_EXTRACT_JS) or []
            if not offers and debug_tag:
                _save_debug(page, debug_tag)
        finally:
            browser.close()
    return offers


def search_flight_offers(origin, destination, depart_date, return_date,
                         adults=1, currency="NZD", max_offers=5, market="nz",
                         retries=2):
    """
    Scrape cheapest round-trip fares for one itinerary from Google Flights.

    Returns a list of normalised offer dicts (possibly empty), each shaped like:
        {"price": float, "currency": str, "airline": str,
         "stops": int, "duration_minutes": int}
    """
    url = _build_url(origin, destination, depart_date, return_date,
                     adults, currency, market)
    cur = (currency or "NZD").upper()
    tag = f"{origin}-{destination}-{depart_date}"

    raw = []
    last_err = None
    for attempt in range(max(retries, 1)):
        try:
            raw = _scrape(url, debug_tag=tag if attempt == retries - 1 else None)
            if raw:
                break
        except Exception as e:  # browser/launch hiccups -- retry a couple of times
            last_err = e
        time.sleep(2 * (attempt + 1))
    if not raw and last_err is not None:
        raise last_err

    offers = []
    for o in raw:
        try:
            price = float(o["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        offers.append({
            "price": price,
            "currency": cur,
            "airline": (o.get("airline") or "")[:60],
            "stops": int(o.get("stops", 0) or 0),
            "duration_minutes": int(o.get("duration_minutes", 0) or 0),
        })
    # Keep the cheapest few; the collector only stores the single cheapest anyway.
    offers.sort(key=lambda x: x["price"])
    return offers[:max_offers]


def cheapest_offer(offers):
    """Pick the lowest-price normalised offer, or None if there are none."""
    if not offers:
        return None
    return min(offers, key=lambda o: o["price"])
