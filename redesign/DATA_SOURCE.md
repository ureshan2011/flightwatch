# Data source & monetization — recommendation

> Status: **advisory**. Nothing here has been ripped out. The Google Flights
> scraper still runs exactly as before; this is the migration path to make the
> data rail and the affiliate rail the same pipe.

## Where we are today

FlightWatch's fares come from **scraping Google Flights** with headless
browsers (`flightwatch/provider.py`). It works and is keyless, but:

- It is **fragile** — Google can reshape its markup or soft-block a CI IP at any
  time; a bad scan records `no_results` and the page silently goes stale.
- It is **against Google's ToS**, so it can't sit under a real business.
- It is the **root cause of the dirty `airline` field** this audit fixed
  (carrier names glued together, prices/CO₂/airport codes leaking into the
  column). Extraction noise is intrinsic to scraping a page that wasn't built
  for us. See `clean_airline()` in `dashboard.py` for the defensive layer.
- Critically, **it gives us no booking link.** The affiliate handoff — the one
  moment the product earns — is bolted on separately from the price we quote.

## The recommendation: one pipe for price **and** payout

Move the feed to an **affiliate-program API** that returns *both* a legitimate
price and a native, commission-bearing deep link, so the fare we score and the
link we hand off are the same object:

| Provider | Why it fits the corridor | Booking link |
|---|---|---|
| **Travelpayouts / Aviasales** | Affiliate-native; one of the few feeds with any depth on thin NZ↔South-Asia routes. | yes (affiliate) |
| **Kiwi.com Tequila** | Strong on multi-leg/self-transfer itineraries (exactly the CHC→SIN→CMB shape we track); affiliate built in. | yes (affiliate) |

Both replace *two* moving parts (a fragile scraper **and** a hand-built booking
URL) with one authorized call.

### This environment already has the feeds wired

The session exposes affiliate/booking MCP servers we can prototype against
immediately — **`Kiwi_com`** (`search-flight`), **`Expedia`**
(`search_flights`), and **`lastminute_com`**, which notably offers a native
**`generate_booking_link`**. That last one is the pattern in miniature: search →
score → emit the partner deep link from the same response.

## How to migrate without a rewrite

The provider is already isolated behind one interface — keep its
`search_flight_offers` / `search_flight_offers_async` shape and **everything
downstream (storage, predict, analytics, dashboard, alerts) is unchanged.**

1. Add `flightwatch/provider_affiliate.py` implementing the same two functions
   against the chosen API, emitting the existing offer record **plus a new
   `book_url` field** carrying the affiliate deep link.
2. Thread `book_url` through `storage.py` (one new column, append-only — old
   rows stay valid) into the dashboard's existing "Book" CTAs, so the verdict's
   green-light moment links straight to a commission-bearing URL.
3. **Keep the scraper as a labelled fallback** (`source` already distinguishes
   rows). If the API lacks a fare for a thin date, the scraper can still
   backfill history — we just don't *monetize* those rows.

## Net

- Removes the ToS and fragility risk from the critical path.
- Makes the affiliate link a property of the fare, not an afterthought —
  directly serving the Priority-1 "good-deal verdict → book" conversion.
- Preserves the longitudinal price-history moat (same record shape, same CSVs).
