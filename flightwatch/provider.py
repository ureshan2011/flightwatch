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
  2. Each scrape picks a different *browser fingerprint* from a rotating pool --
     and crucially across two REAL engines, Chromium (Chrome/Edge) and Firefox
     (Gecko), not just different UA strings on one engine -- plus anti-automation
     launch flags/prefs and a stealth init script. Rotating a genuinely diverse
     browser mix is what stops Google soft-blocking us into empty `no_results`
     pages, so most scrapes now return a full list instead of nothing.
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
import asyncio

from . import ROOT_DIR

DEBUG_DIR = os.path.join(ROOT_DIR, "debug")

# A pool of realistic desktop browser fingerprints. Each scrape picks one (and a
# retry picks a *different* one), so Google sees varied "browser tags" instead of
# the same headless tell every time -- the key to not getting blocked.
#
# We deliberately mix TWO real engines -- Chromium (Chrome/Edge) and Firefox
# (Gecko) -- not just different UA strings on the same engine. A spoofed UA alone
# is shallow: the JS engine, TLS handshake and feature detection still betray the
# real engine. Driving an actual Firefox for the Gecko fingerprints means those
# deeper tells line up with the UA, so Google sees a genuinely diverse browser mix.
# Each fingerprint declares which `engine` Playwright should launch for it.
_FINGERPRINTS = [
    # --- Chromium-family (Chrome / Edge) -------------------------------------
    {"engine": "chromium",
     "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
     "platform": "MacIntel", "viewport": (1440, 900),
     "locale": "en-US", "tz": "Pacific/Auckland", "lang": "en-US,en;q=0.9"},
    {"engine": "chromium",
     "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
     "platform": "Win32", "viewport": (1536, 864),
     "locale": "en-US", "tz": "Pacific/Auckland", "lang": "en-US,en;q=0.9"},
    {"engine": "chromium",
     "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
     "platform": "Linux x86_64", "viewport": (1366, 768),
     "locale": "en-NZ", "tz": "Pacific/Auckland", "lang": "en-NZ,en;q=0.9"},
    {"engine": "chromium",
     "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
     "platform": "Win32", "viewport": (1600, 900),
     "locale": "en-GB", "tz": "Europe/London", "lang": "en-GB,en;q=0.9"},
    {"engine": "chromium",
     "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
     "platform": "MacIntel", "viewport": (1680, 1050),
     "locale": "en-AU", "tz": "Australia/Sydney", "lang": "en-AU,en;q=0.9"},
    {"engine": "chromium",
     "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
     "platform": "Win32", "viewport": (1920, 1080),
     "locale": "en-NZ", "tz": "Pacific/Auckland", "lang": "en-NZ,en;q=0.9"},
    # --- Firefox-family (Gecko) ----------------------------------------------
    {"engine": "firefox",
     "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) "
           "Gecko/20100101 Firefox/139.0",
     "platform": "Win32", "viewport": (1920, 1080),
     "locale": "en-US", "tz": "America/New_York", "lang": "en-US,en;q=0.5"},
    {"engine": "firefox",
     "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) "
           "Gecko/20100101 Firefox/138.0",
     "platform": "MacIntel", "viewport": (1512, 982),
     "locale": "en-AU", "tz": "Australia/Sydney", "lang": "en-AU,en;q=0.5"},
    {"engine": "firefox",
     "ua": "Mozilla/5.0 (X11; Linux x86_64; rv:139.0) "
           "Gecko/20100101 Firefox/139.0",
     "platform": "Linux x86_64", "viewport": (1680, 1050),
     "locale": "en-NZ", "tz": "Pacific/Auckland", "lang": "en-NZ,en;q=0.5"},
    {"engine": "firefox",
     "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
           "Gecko/20100101 Firefox/140.0",
     "platform": "Win32", "viewport": (1366, 768),
     "locale": "en-GB", "tz": "Europe/London", "lang": "en-GB,en;q=0.5"},
    {"engine": "firefox",
     "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:139.0) "
           "Gecko/20100101 Firefox/139.0",
     "platform": "MacIntel", "viewport": (1440, 900),
     "locale": "en-US", "tz": "America/Chicago", "lang": "en-US,en;q=0.5"},
]

# Chromium launch flags that strip the most obvious automation tells. Firefox
# rejects these CLI flags, so it gets its own (empty) arg list plus user prefs.
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-background-networking",
    "--disable-component-update",
    "--no-first-run",
    "--no-default-browser-check",
]
# Backwards-compatible alias: older callers (and the collector) reference this.
_LAUNCH_ARGS = _CHROMIUM_ARGS

# Firefox prefs that hide the automation tells Gecko exposes (the navigator
# .webdriver flag and Marionette's automation extension).
_FIREFOX_PREFS = {
    "dom.webdriver.enabled": False,
    "useAutomationExtension": False,
    "media.peerconnection.enabled": False,
}

# Engines we know how to launch. Defaults to chromium for any fingerprint that
# predates the `engine` key.
_ENGINES = ("chromium", "firefox")

# --------------------------------------------------------------------------- #
# Speed + success levers (the core of why scrapes now land more fares, faster).
# --------------------------------------------------------------------------- #
#
# 1) RESOURCE BLOCKING. A Google Flights page pulls megabytes of images, fonts,
#    map tiles, ads and analytics that carry ZERO fare data but dominate load
#    time and CPU. On a 2-core CI runner driving several browser contexts at once,
#    that contention is what stalls a page past our wait window and turns it into
#    an empty `no_results`. We abort those requests at the network layer, so each
#    page is ~80% lighter: it renders far faster and the runner stops choking --
#    which lifts BOTH throughput and success rate. We keep scripts + stylesheets
#    (Google Flights renders its fare list with JS, and we test button visibility).
_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCK_URL_HINTS = (
    "google-analytics", "googletagmanager", "googleadservices",
    "googlesyndication", "doubleclick", "/gen_204", "/maps/vt", "/maps/api",
    "play.google.com/log", "clients6.google.com",
    "youtube.com", "ytimg.com", "gstatic.com/og/",
    "pagead", "adservice", "adsense", "accounts.google.com",
    "plus.google.com", "apis.google.com/js/platform",
    "fundingchoicesmessages", "consent.google.com",
)


def _should_block(resource_type, url):
    if resource_type in _BLOCK_RESOURCE_TYPES:
        return True
    u = url or ""
    return any(h in u for h in _BLOCK_URL_HINTS)


# 2) FAST BLOCK DETECTION. When Google soft-blocks the runner it serves a
#    sorry/unusual-traffic/captcha page, not fares. Rather than burn the full
#    wait window on rows that will never appear, we sniff for those markers and
#    bail in seconds so the retry can switch to a different real engine -- by far
#    the most effective way to shake a soft-block.
_FARE_READY_JS = "() => !!document.querySelector('[aria-label*=\"trip total\" i]')"
_BLOCK_SIGNS = ("unusual traffic", "automated queries", "are you a robot",
                "/sorry/", "recaptcha", "detected unusual", "not a robot")


def _attempt_fingerprints(retries):
    """One fingerprint per attempt, ALTERNATING engine across attempts.

    A retry after an empty/blocked attempt is far more likely to succeed if it
    flips Chromium<->Firefox: a genuinely different engine (TLS, JS, fingerprint)
    is what dodges a soft-block, where a second try on the same engine just gets
    blocked again. We round-robin the engines and pick a fresh fingerprint within
    each, so attempt 1/2/3 hit different browsers.
    """
    by_engine = {}
    for fp in _FINGERPRINTS:
        by_engine.setdefault(_engine_of(fp), []).append(fp)
    for lst in by_engine.values():
        random.shuffle(lst)
    engines = list(by_engine.keys())
    random.shuffle(engines)
    out, i = [], 0
    while len(out) < max(retries, 1):
        eng = engines[i % len(engines)]
        pool = by_engine[eng]
        out.append(pool[(i // len(engines)) % len(pool)])
        i += 1
    return out


def _engine_of(fp) -> str:
    eng = (fp or {}).get("engine", "chromium")
    return eng if eng in _ENGINES else "chromium"


def _launch_kwargs(engine, headless):
    """Per-engine launch arguments for Playwright's `*.launch(...)`."""
    if engine == "firefox":
        return {"headless": headless, "firefox_user_prefs": dict(_FIREFOX_PREFS)}
    return {"headless": headless, "args": list(_CHROMIUM_ARGS)}


def engines_in_use(fingerprints=None) -> list:
    """Distinct engines referenced by the fingerprint pool (for shared launch)."""
    pool = fingerprints if fingerprints is not None else _FINGERPRINTS
    seen = []
    for fp in pool:
        eng = _engine_of(fp)
        if eng not in seen:
            seen.append(eng)
    return seen


def headless_mode() -> bool:
    """Headless unless FLIGHTWATCH_HEADFUL=1 (CI runs headful under xvfb)."""
    return os.environ.get("FLIGHTWATCH_HEADFUL", "") != "1"


def _jitter_viewport(vp):
    """Add small random offsets to a viewport so consecutive scrapes with the same
    fingerprint don't have pixel-identical window dimensions."""
    w, h = vp
    return (w + random.randint(-20, 20), h + random.randint(-14, 14))


def _stealth_js(fp):
    """Per-context init script: hide webdriver and match navigator to the fingerprint.

    Engine-aware: the `window.chrome` shim is a Chrome-only object, so injecting
    it under a Firefox fingerprint would itself be a tell -- we skip it there.
    """
    langs = "['" + "','".join(fp["lang"].split(",")[0:2]).replace(";q=0.5", "")\
        .replace(";q=0.9", "") + "']"
    hw_conc = random.choice([4, 8, 12, 16])
    dev_mem = random.choice([4, 8, 16])
    js = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        f"Object.defineProperty(navigator,'platform',{{get:()=>'{fp['platform']}'}});"
        f"Object.defineProperty(navigator,'languages',{{get:()=>{langs}}});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "Object.defineProperty(navigator,'maxTouchPoints',{get:()=>0});"
        f"Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>{hw_conc}}});"
        f"Object.defineProperty(navigator,'deviceMemory',{{get:()=>{dev_mem}}});"
    )
    if _engine_of(fp) == "chromium":
        js += "window.chrome = window.chrome || {runtime:{},loadTimes:()=>({}),csi:()=>({}),"
        js += "app:{isInstalled:false,InstallState:{DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed'},RunningState:{CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running'}}};"
    js += ("if(window.Notification){"
           "Object.defineProperty(Notification,'permission',{get:()=>'default'});}")
    return js


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
    // Airline. The text heuristic alone was weak (~half blank, plus garbage like
    // route codes "CHC-CMB" and run-together names), so anchor on the carrier
    // logos' alt text first -- Google labels each flight's airline logo with the
    // operating carrier(s) -- and only fall back to scanning text lines.
    const isJunk = (s) => !s
      || /^[A-Z]{3}\s*[–—-]\s*[A-Z]{3}$/.test(s)   // "CHC-CMB" route label
      || /co2e?|co₂|emission|\bkg\b/i.test(s)       // "706 kg CO2e" emissions line
      || /[$€£₹]/.test(s)                            // any price string ("NZ$4,005")
      || /\d{2,}/.test(s)                            // price/flight-no tail ("4,005")
      || /^\$|\d{1,2}:\d{2}|\bhr\b|\bmin\b|stop|nonstop|round trip|select|operated/i.test(s);
    let airline = '';
    const alts = [...li.querySelectorAll('img[alt]')]
      .map(im => (im.getAttribute('alt') || '').trim())
      // keep logo alts that name a carrier, drop generic/UI/icon alts.
      .filter(a => a && a.length >= 2 && a.length <= 60 && !/logo|icon|google|seat/i.test(a)
                   && !isJunk(a));
    if (alts.length) {
      // de-dup (logos repeat per leg) and join multi-carrier itineraries cleanly.
      airline = [...new Set(alts)].join(', ');
    } else {
      for (const line of text.split('\n').map(s => s.trim()).filter(Boolean)) {
        if (isJunk(line)) continue;
        if (line.length >= 2 && line.length <= 40) { airline = line; break; }
      }
    }
    // Layover airport(s) -- "where the stop is". Google labels each connection
    // with an aria-label like "Layover (1 of 1) is a 2 hr 30 min layover at
    // Singapore Changi Airport in Singapore" and, in the row, a bare 3-letter
    // code. Wrapped so any markup change can NEVER break the core extraction.
    let layover = '';
    try {
      const codes = [];
      // (a) explicit layover aria-labels: pull a trailing/standalone 3-letter code.
      for (const el of li.querySelectorAll('[aria-label*="layover" i],[aria-label*="stop in" i]')) {
        const a = el.getAttribute('aria-label') || '';
        const cm = a.match(/\b([A-Z]{3})\b/);
        if (cm) codes.push(cm[1]);
      }
      // (b) fallback: a span whose WHOLE text is a 3-letter code (the layover chip).
      if (!codes.length && stops > 0) {
        for (const sp of li.querySelectorAll('span,div')) {
          const t = (sp.textContent || '').trim();
          if (/^[A-Z]{3}$/.test(t)) { codes.push(t); }
        }
      }
      layover = [...new Set(codes)].slice(0, 3).join(', ');
    } catch (e) { layover = ''; }
    out.push({price, airline, stops, duration_minutes: minutes, layover});
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


_MORE_SELECTORS = (
    "button:has-text('View more flights')",
    "button:has-text('Show more flights')",
    "button:has-text('More flights')",
    "[aria-label*='more flights' i]",
)
_ROW_COUNT_JS = ("() => document.querySelectorAll("
                 "'li [aria-label*=\"trip total\" i]').length")


def _install_blocker(ctx):
    """Drop fare-irrelevant heavy requests (images/fonts/media/ads/analytics)."""
    def _route(route):
        req = route.request
        try:
            if _should_block(req.resource_type, req.url):
                route.abort()
                return
        except Exception:
            pass
        try:
            route.continue_()
        except Exception:
            pass
    try:
        ctx.route("**/*", _route)
    except Exception:
        pass


def _is_blocked(page):
    """True if Google served a sorry/unusual-traffic/captcha page, not fares."""
    try:
        if "/sorry/" in (page.url or ""):
            return True
        blob = (page.evaluate(
            "() => ((document.title||'') + ' ' + "
            "(document.body ? document.body.innerText.slice(0,1500) : '')"
            ").toLowerCase()") or "")
    except Exception:
        return False
    return any(s in blob for s in _BLOCK_SIGNS)


def _wait_for_fares(page, timeout_ms=38000):
    """Poll for fares; return 'ok' once they appear, 'blocked' on a block page,
    or 'empty' on timeout -- exiting early either way instead of a fixed wait."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if page.evaluate(_FARE_READY_JS):
                return "ok"
        except Exception:
            pass
        if _is_blocked(page):
            return "blocked"
        page.wait_for_timeout(600)
    return "empty"


def _load_full_list(page):
    """
    Expand the results so the WHOLE fare list is in the DOM, not just the top few.
    Google hides most flights behind a "View more flights" button and lazy-loads
    the rest as you scroll. We click the button and scroll until the row count
    stops growing (or we hit a sane cap), which is what turns one scrape into many
    offers.
    """
    last, stable = -1, 0
    for _ in range(16):  # hard cap so a runaway page can't hang the scrape
        for sel in _MORE_SELECTORS:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(400)
            except Exception:
                pass
        try:
            page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(450)
        try:
            count = page.evaluate(_ROW_COUNT_JS)
        except Exception:
            count = last
        # Stop only after the row count holds steady for two polls -- that absorbs
        # a slow lazy-load without paying the old fixed multi-second waits.
        if count <= last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = count


def _scrape(url, fingerprint=None, debug_tag=None):
    """Open the Google Flights URL in Chromium and return a list of raw offers."""
    from playwright.sync_api import sync_playwright

    fp = fingerprint or random.choice(_FINGERPRINTS)
    headless = os.environ.get("FLIGHTWATCH_HEADFUL", "") != "1"
    engine = _engine_of(fp)
    vw, vh = _jitter_viewport(fp["viewport"])
    offers = []
    with sync_playwright() as p:
        launcher = getattr(p, engine)
        browser = launcher.launch(**_launch_kwargs(engine, headless))
        ctx = browser.new_context(
            user_agent=fp["ua"], locale=fp["locale"], timezone_id=fp["tz"],
            viewport={"width": vw, "height": vh},
            extra_http_headers={"Accept-Language": fp["lang"]},
        )
        _install_blocker(ctx)
        ctx.add_init_script(_stealth_js(fp))
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _dismiss_consent(page)
            # Poll for fares: return the instant rows appear, bail fast on a block.
            state = _wait_for_fares(page)
            if state == "ok":
                _load_full_list(page)    # expand to the whole results list
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

    # A fresh fingerprint per attempt, alternating engine; last attempt dumps debug.
    fps = _attempt_fingerprints(retries)

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
        time.sleep(1.5 + attempt)
    if not raw and last_err is not None:
        raise last_err

    return _normalize_offers(raw, cur, max_offers)


def _normalize_offers(raw, currency, max_offers):
    """De-dupe, clean, price-sort and cap a list of raw scraped offers."""
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
            "currency": currency,
            "airline": (o.get("airline") or "")[:60],
            "stops": int(o.get("stops", 0) or 0),
            "duration_minutes": int(o.get("duration_minutes", 0) or 0),
            "layover": (o.get("layover") or "")[:40],
        })
    offers.sort(key=lambda x: x["price"])
    if not max_offers or len(offers) <= max_offers:
        return offers
    # Keep the cheapest `max_offers`, but never let the cap hide a carrier
    # entirely: also retain the cheapest fare of any airline not already kept.
    # That's what surfaces premium end-to-end carriers (e.g. a full Singapore
    # Airlines routing via SIN) that sit above the cheapest connecting combos.
    kept = offers[:max_offers]
    seen = {o["airline"] for o in kept if o["airline"]}
    for o in offers[max_offers:]:
        a = o["airline"]
        if a and a not in seen:
            kept.append(o)
            seen.add(a)
    return kept


# --------------------------------------------------------------------------- #
# Async path: one shared browser, one lightweight context per itinerary.
#
# The collector drives this. Reusing a single Chromium across all itineraries --
# instead of launching a whole browser per scrape -- cuts memory and start-up
# cost dramatically, so we can run far more itineraries concurrently (and more
# often) on the same free CI runner. Each context still gets its own rotating
# fingerprint + stealth script, so Google sees varied browsers as before.
# --------------------------------------------------------------------------- #
async def _dismiss_consent_async(page):
    for sel in ("button[aria-label*='Accept all' i]",
                "button:has-text('Accept all')",
                "button:has-text('I agree')",
                "form[action*='consent'] button"):
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(1500)
                return
        except Exception:
            pass


async def _install_blocker_async(ctx):
    """Async twin of _install_blocker: abort fare-irrelevant heavy requests."""
    async def _route(route):
        req = route.request
        try:
            if _should_block(req.resource_type, req.url):
                await route.abort()
                return
        except Exception:
            pass
        try:
            await route.continue_()
        except Exception:
            pass
    try:
        await ctx.route("**/*", _route)
    except Exception:
        pass


async def _is_blocked_async(page):
    try:
        if "/sorry/" in (page.url or ""):
            return True
        blob = (await page.evaluate(
            "() => ((document.title||'') + ' ' + "
            "(document.body ? document.body.innerText.slice(0,1500) : '')"
            ").toLowerCase()") or "")
    except Exception:
        return False
    return any(s in blob for s in _BLOCK_SIGNS)


async def _wait_for_fares_async(page, timeout_ms=38000):
    """Async twin of _wait_for_fares: early-exit poll for fares / block / timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_ms / 1000.0
    while loop.time() < deadline:
        try:
            if await page.evaluate(_FARE_READY_JS):
                return "ok"
        except Exception:
            pass
        if await _is_blocked_async(page):
            return "blocked"
        await page.wait_for_timeout(600)
    return "empty"


async def _load_full_list_async(page):
    """Async twin of _load_full_list: expand the whole results list into the DOM."""
    last, stable = -1, 0
    for _ in range(16):
        for sel in _MORE_SELECTORS:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(400)
            except Exception:
                pass
        try:
            await page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            pass
        await page.wait_for_timeout(450)
        try:
            count = await page.evaluate(_ROW_COUNT_JS)
        except Exception:
            count = last
        if count <= last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = count


async def _save_debug_async(page, tag):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        await page.screenshot(path=os.path.join(DEBUG_DIR, f"{tag}.png"), full_page=True)
        html = await page.content()
        with open(os.path.join(DEBUG_DIR, f"{tag}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass


def _pick_browser(browsers, fp):
    """Pick the shared browser matching this fingerprint's engine.

    `browsers` is an engine->browser dict (e.g. {"chromium": ..., "firefox": ...}).
    Falls back to any available browser if the exact engine wasn't launched.
    """
    eng = _engine_of(fp)
    return browsers.get(eng) or next(iter(browsers.values()))


async def _scrape_async(browsers, url, fingerprint=None, debug_tag=None):
    """Open one URL in a fresh context on the matching SHARED browser; return offers."""
    fp = fingerprint or random.choice(_FINGERPRINTS)
    browser = _pick_browser(browsers, fp)
    vw, vh = _jitter_viewport(fp["viewport"])
    ctx = await browser.new_context(
        user_agent=fp["ua"], locale=fp["locale"], timezone_id=fp["tz"],
        viewport={"width": vw, "height": vh},
        extra_http_headers={"Accept-Language": fp["lang"]},
    )
    await _install_blocker_async(ctx)
    await ctx.add_init_script(_stealth_js(fp))
    page = await ctx.new_page()
    offers = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await _dismiss_consent_async(page)
        state = await _wait_for_fares_async(page)
        if state == "ok":
            await _load_full_list_async(page)
            offers = await page.evaluate(_EXTRACT_JS) or []
        if not offers and debug_tag:
            await _save_debug_async(page, debug_tag)
    finally:
        await ctx.close()
    return offers


async def search_flight_offers_async(browsers, origin, destination, depart_date,
                                     return_date, adults=1, currency="NZD",
                                     max_offers=50, market="nz", retries=3):
    """Async twin of search_flight_offers that reuses shared browsers.

    `browsers` is an engine->browser dict; each retry picks a different
    fingerprint (and therefore possibly a different engine), so a soft-block on
    one browser tag doesn't poison the rest.
    """
    url = _build_url(origin, destination, depart_date, return_date,
                     adults, currency, market)
    cur = (currency or "NZD").upper()
    tag = f"{origin}-{destination}-{depart_date}"

    fps = _attempt_fingerprints(retries)

    raw, last_err = [], None
    for attempt in range(max(retries, 1)):
        try:
            raw = await _scrape_async(browsers, url, fingerprint=fps[attempt],
                                      debug_tag=tag if attempt == retries - 1 else None)
            if raw:
                break
        except Exception as e:
            last_err = e
        await asyncio.sleep(1.5 + attempt)
    if not raw and last_err is not None:
        raise last_err
    return _normalize_offers(raw, cur, max_offers)


def cheapest_offer(offers):
    """Pick the lowest-price normalised offer, or None if there are none."""
    if not offers:
        return None
    return min(offers, key=lambda o: o["price"])
