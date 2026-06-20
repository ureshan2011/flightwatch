"""
Flight fare provider: Google Flights, scraped with real headless browsers.

Why a browser and not an HTTP client?
  Google Flights serves its fare list through internal XHR ("batchexecute") calls
  that run *after* the page loads, and it fingerprints/loads differently for plain
  HTTP clients -- which is why lightweight scrapers (and the Travelpayouts cache)
  came back empty for thin routes like CHC <-> CMB. Driving an actual Chromium via
  Playwright renders the page the way a person's browser would, so the results
  actually populate. It needs no API key and no paid proxy, which keeps FlightWatch
  free and fully autonomous.

How it scrapes at volume without getting blocked:
  1. `fast_flights` builds Google's `?tfs=` Protobuf query (origin/dest/dates/seat),
     which is the hard part of forming a valid Google Flights URL.
  2. Each scrape picks a different *browser fingerprint* (user-agent + platform +
     viewport + language + timezone) from a rotating pool, plus anti-automation
     launch flags and a stealth init script. Rotating the "browser tag" is what
     stops Google soft-blocking us into empty `no_results` pages, so most scrapes
     now return a full list instead of nothing.
  3. After the first fare rows appear we scroll and click "View more flights" to
     pull the WHOLE results list out of the DOM -- not just the top "Best" few --
     so a single scrape yields many offers (massive data, free tier).
  4. We parse prices from stable ARIA labels ("... round trip total") rather than
     Google's churning CSS class names, so it survives cosmetic redesigns.

Concurrency lives in the collector: it runs several of these scrapes in parallel,
each in its own browser with its own fingerprint.

On failure it writes a screenshot + HTML to `debug/` (gitignored) so a CI run can
be inspected and selectors tuned without guessing.

The rest of the project only depends on `search_flight_offers` and
`cheapest_offer` returning normalised dicts, so this file is the only thing to
touch when swapping fare sources.
"""

import os
import re
import random
import time

from . import ROOT_DIR

DEBUG_DIR = os.path.join(ROOT_DIR, "debug")

# A pool of realistic desktop browser fingerprints. Each scrape picks one (and a
# retry picks a *different* one), so Google sees varied "browser tags" instead of
# the same headless Chromium tell every time -- the key to not getting blocked.
# We stay on Chromium-consistent UAs (Chrome/Edge across OSes) so the client hints
# Google reads don't contradict the user-agent string.
_FINGERPRINTS = [
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
     "platform": "MacIntel", "viewport": (1440, 900),
     "locale": "en-US", "tz": "Pacific/Auckland", "lang": "en-US,en;q=0.9"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
     "platform": "Win32", "viewport": (1536, 864),
     "locale": "en-US", "tz": "Pacific/Auckland", "lang": "en-US,en;q=0.9"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
     "platform": "Linux x86_64", "viewport": (1366, 768),
     "locale": "en-NZ", "tz": "Pacific/Auckland", "lang": "en-NZ,en;q=0.9"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
     "platform": "Win32", "viewport": (1600, 900),
     "locale": "en-GB", "tz": "Europe/London", "lang": "en-GB,en;q=0.9"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
     "platform": "MacIntel", "viewport": (1512, 982),
     "locale": "en-AU", "tz": "Australia/Sydney", "lang": "en-AU,en;q=0.9"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
     "platform": "Win32", "viewport": (1920, 1080),
     "locale": "en-US", "tz": "America/New_York", "lang": "en-US,en;q=0.9"},
]

# Launch flags that strip the most obvious automation tells.
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]


def _stealth_js(fp):
    """Per-context init script: hide webdriver and match navigator to the fingerprint."""
    langs = "['" + "','".join(fp["lang"].split(",")[0:2]).replace(";q=0.9", "") + "']"
    return (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        f"Object.defineProperty(navigator,'platform',{{get:()=>'{fp['platform']}'}});"
        f"Object.defineProperty(navigator,'languages',{{get:()=>{langs}}});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "window.chrome = window.chrome || {runtime:{}};"
    )


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


def _load_full_list(page):
    """
    Expand the results so the WHOLE fare list is in the DOM, not just the top few.
    Google hides most flights behind a "View more flights" button and lazy-loads
    the rest as you scroll. We click the button and scroll until the row count
    stops growing (or we hit a sane cap), which is what turns one scrape into many
    offers.
    """
    more_selectors = (
        "button:has-text('View more flights')",
        "button:has-text('Show more flights')",
        "button:has-text('More flights')",
        "[aria-label*='more flights' i]",
    )
    last = -1
    for _ in range(12):  # hard cap so a runaway page can't hang the scrape
        for sel in more_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(1200)
            except Exception:
                pass
        try:
            page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(800)
        try:
            count = page.evaluate(
                "() => document.querySelectorAll("
                "'li [aria-label*=\"trip total\" i]').length")
        except Exception:
            count = last
        if count <= last:          # nothing new loaded -- we've got the full list
            break
        last = count


def _scrape(url, fingerprint=None, debug_tag=None):
    """Open the Google Flights URL in Chromium and return a list of raw offers."""
    from playwright.sync_api import sync_playwright

    fp = fingerprint or random.choice(_FINGERPRINTS)
    headless = os.environ.get("FLIGHTWATCH_HEADFUL", "") != "1"
    vw, vh = fp["viewport"]
    offers = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
        ctx = browser.new_context(
            user_agent=fp["ua"], locale=fp["locale"], timezone_id=fp["tz"],
            viewport={"width": vw, "height": vh},
            extra_http_headers={"Accept-Language": fp["lang"]},
        )
        ctx.add_init_script(_stealth_js(fp))
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
            _load_full_list(page)        # expand to the whole results list
            offers = page.evaluate(_EXTRACT_JS) or []
            if not offers and debug_tag:
                _save_debug(page, debug_tag)
        finally:
            browser.close()
    return offers


def _save_debug(page, tag):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{tag}.png"), full_page=True)
        with open(os.path.join(DEBUG_DIR, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def _dedupe(offers):
    """Collapse identical fare rows (same airline/price/stops/duration)."""
    seen, out = set(), []
    for o in offers:
        key = (o.get("airline"), o.get("price"),
               o.get("stops"), o.get("duration_minutes"))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


def search_flight_offers(origin, destination, depart_date, return_date,
                         adults=1, currency="NZD", max_offers=50, market="nz",
                         retries=3):
    """
    Scrape round-trip fares for one itinerary from Google Flights.

    Returns a list of normalised offer dicts -- ALL distinct fares found, not just
    the cheapest -- so the caller can store the whole list. Each is shaped like:
        {"price": float, "currency": str, "airline": str,
         "stops": int, "duration_minutes": int}

    Each retry uses a different browser fingerprint, so a soft-block on one tag
    doesn't poison the rest of the attempts.
    """
    url = _build_url(origin, destination, depart_date, return_date,
                     adults, currency, market)
    cur = (currency or "NZD").upper()
    tag = f"{origin}-{destination}-{depart_date}"

    # A fresh fingerprint per attempt; the last attempt also dumps debug on empty.
    fps = random.sample(_FINGERPRINTS, k=min(retries, len(_FINGERPRINTS)))
    while len(fps) < retries:
        fps.append(random.choice(_FINGERPRINTS))

    raw = []
    last_err = None
    for attempt in range(max(retries, 1)):
        try:
            raw = _scrape(url, fingerprint=fps[attempt],
                          debug_tag=tag if attempt == retries - 1 else None)
            if raw:
                break
        except Exception as e:  # browser/launch hiccups -- retry with a new tag
            last_err = e
        time.sleep(2 * (attempt + 1))
    if not raw and last_err is not None:
        raise last_err

    offers = []
    for o in _dedupe(raw):
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
    offers.sort(key=lambda x: x["price"])
    return offers[:max_offers] if max_offers else offers


def cheapest_offer(offers):
    """Pick the lowest-price normalised offer, or None if there are none."""
    if not offers:
        return None
    return min(offers, key=lambda o: o["price"])
