# World Monitor: sources, setup and data boundaries

World is a cached attention desk, separate from the price/FX/option evidence
used by QuantMind's risk calculations. A news mention does not establish an
exposure, a forecast, a risk contribution or a trading recommendation.

## Included routes

All endpoints are fixed in `src/quantmind/world/sources.py`. These are working
adapters, not links awaiting implementation. Availability is checked on each
refresh and is not guaranteed. The table records read-only smoke checks on
5 September 2026; counts change continuously.

| ID | Coverage / endpoint | Minimum refresh | Access / smoke result |
| --- | --- | --- | --- |
| `fed` | [Federal Reserve releases](https://www.federalreserve.gov/feeds/press_all.xml) | 15 min | Public RSS; 20 parsed |
| `ecb` | [ECB releases and speeches](https://www.ecb.europa.eu/rss/press.html) | 15 min | Public RSS; 15 parsed |
| `boe` | [Bank of England news](https://www.bankofengland.co.uk/rss/news) | 15 min | Public RSS; 50 parsed |
| `bls` | [US labour and inflation indicators](https://www.bls.gov/feed/bls_latest.rss) | 30 min | Public RSS; 1 aggregate item parsed |
| `bea` | [US growth and trade releases](https://apps.bea.gov/rss/rss.xml) | 30 min | Public RSS; 47 parsed |
| `bis` | [BIS press releases](https://www.bis.org/doclist/all_pressrels.rss) | 30 min | Public RSS; 10 parsed |
| `eia` | [Today in Energy](https://www.eia.gov/rss/todayinenergy.xml) | 30 min | Public RSS; 13 parsed |
| `un` | [UN global news](https://news.un.org/feed/subscribe/en/news/all/rss.xml) | 15 min | Public RSS; 30 parsed |
| `gdacs` | [Global disaster alerts](https://www.gdacs.org/xml/rss.xml) | 5 min | Public RSS; 200-item parser cap reached |
| `usgs` | [Magnitude 4.5+ earthquakes, past day](https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson) | 5 min | Public GeoJSON; 14 parsed |
| `gdelt` | [GDELT DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) | 15 min | Public JSON news index; live probe timed out, failure isolated |
| `imf` | [IMF SDMX update feed](https://sdmxcentral.imf.org/rss.xml) | 60 min | Public RSS; 22 parsed; statistical metadata updates, not an IMF forecast database |
| `ukgov` | [UK government communications](https://www.gov.uk/search/news-and-communications.atom) | 15 min | Public Atom; 20 parsed |
| `who` | [WHO news](https://www.who.int/rss-feeds/news-english.xml) | 15 min | Public RSS; 25 parsed |
| `sec` | [SEC press releases](https://www.sec.gov/news/pressreleases.rss) | 15 min | Contact identity required; not live-tested without one |
| `x` | [X recent search](https://docs.x.com/x-api/posts/recent-search) | 15 min | Paid, explicit opt-in; fixture-tested only |
| `reddit` | [Reddit Data API](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki) | 15 min | Approved OAuth, explicit opt-in; fixture-tested only |

The GDELT route uses `https://api.gdeltproject.org/api/v2/doc/doc` in JSON
article-list mode, a fixed economy/markets/geopolitics query, date ordering,
and at most 100 records. GDELT timestamps represent discovery, not original
publication. It is a machine-indexed lead to verify at the publisher.

## First use

Start the normal local app, open `/world`, click **Refresh sources**, and save
your watch symbols/interests/regions. No provider API key is necessary for
the 14 public routes. Refresh is explicit and can take roughly a minute when
sources are slow. Successful sources commit independently as they complete.
GET requests read only the cache; they never send your holdings to publishers.

Source statuses distinguish **never**, **ok**, **error** and **disabled**.
**Stale** is an independent warning after twice the source cadence (at least
30 minutes) without success. An error preserves the previous successful cache
and timestamp. Empty, valid source responses can be successful: earthquakes
and infrequent releases need not produce a new event every refresh.

The source timestamp is when retrieval succeeded. Each article is separately
dated. Missing publication time becomes first **Observed** time; later polls
do not continually promote the same undated article as new. Invalid,
timezone-less or future supplied dates are rejected. Keep your system clock
correct: the strict policy may reject a publisher whose clock runs ahead.

## Optional credentials

Set these only in local `.env` and restart the backend. Never commit real
credentials. Never use `VITE_` variables for provider tokens: they would be
included in the browser bundle. The World API never returns provider secrets.

### SEC

Set `QM_WORLD_SEC_USER_AGENT` to an identifying application/contact string
containing your actual email, following [SEC automated-access guidance](https://www.sec.gov/about/developer-resources).
This connector is **press releases**, not company filings, EDGAR full-text,
13F portfolio reconstruction or fundamental statements.

### X

[X bills API usage](https://docs.x.com/x-api/getting-started/pricing); it is not
part of the free-source promise. All three settings are required:

```dotenv
QM_WORLD_X_ENABLED=true
QM_WORLD_X_BEARER_TOKEN=your_private_token
QM_WORLD_X_QUERY=from:account_you_choose
```

Use official recent-search syntax, set spending limits in the X developer
console, and consider selecting only public source IDs in a continuous CLI
monitor. Each eligible X refresh requests up to 100 matching posts. A specific
handle query is useful for a breaking-news desk. This is periodic recent
search, not a realtime streaming firehose. Queries are explicitly supplied by
you and sent to X; the app does not build them from your portfolio.

### Reddit

[Reddit requires approved API access](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy).
Obtain a permitted OAuth app and refresh token with read access through the
official authorization flow, then set:

```dotenv
QM_WORLD_REDDIT_ENABLED=true
QM_WORLD_REDDIT_CLIENT_ID=your_client_id
QM_WORLD_REDDIT_CLIENT_SECRET=your_private_client_secret
QM_WORLD_REDDIT_REFRESH_TOKEN=your_private_refresh_token
QM_WORLD_REDDIT_USER_AGENT=your_app_identifier_with_contact
QM_WORLD_REDDIT_SUBREDDITS=investing,stocks,Economics
```

The connector refreshes OAuth and reads the selected communities' newest
posts. No comments, private communities, user profiling, password login or
unauthenticated scraping is implemented. Community posts are unverified
context; they are not issuer statements or trading signals.

## How your lens works

- A selected `book_ref` is read from the existing immutable snapshot and
  validated against the configured account, broker mode and reporting currency.
- Stock symbols and recognized option-underlier symbols are matched locally.
  Zero-quantity and cash entries are excluded; offsetting option legs do not
  cancel an underlier out of the attention list.
- Direct cashtags, case-sensitive unambiguous tickers and an explicit small
  company-name dictionary generate **Holding** or **Watchlist** reasons.
  Short/common-word symbols require stronger evidence. The name dictionary is
  intentionally incomplete; there is no fuzzy match disguised as certainty.
- Interests and regions produce independent reasons. The score orders
  attention, not expected P&L. It does not use position sizing, direction,
  Greeks, sector exposure or causal models.
- Unknown ETFs receive no invented constituent look-through. Multiple listings
  remain separate. A UCITS profile alone does not establish current holdings.
- Your saved lens is shared by this one local installation. A complete save
  replaces it; concurrent saves follow last-write-wins, not multiuser merging.

## Cache, resilience and privacy

`QM_DATA_DIR/world.sqlite3` is separate from the price and portfolio stores.
Transactions preserve last-good feeds; a leased database lock prevents
overlapping API/CLI ingestion across processes. An interrupted process's
lease expires in three minutes. Four feeds run concurrently, with an 8-second
HTTP timeout, 12-second total provider deadline and 2 MiB response bound.
HTTP redirects and non-identity content encodings are refused before reading
the response, preventing decompression from bypassing the memory cap.
XML entities/DTDs, malformed envelopes, unsafe
links and HTML scripts are rejected or stripped. Errors do not expose tokens
or upstream response bodies.

The cache retains at most 30 days, 250 records per source and 5,000 globally.
The view reads at most 30 recent events per source, 500 overall, before local
relevance ranking. These quotas stop a noisy feed monopolizing the desk.
Refresh cooldowns and `Retry-After` prevent repeated clicks from hammering a
publisher. There is no distributed scheduler, notification delivery or
availability SLA. CLI monitoring ends when its process stops.

Public availability is not a redistribution license. World stores bounded
plain-text titles/excerpts and original links locally, not full articles or
third-party pages. Use optional providers within their approved terms; do not
publish the cache as a data product. Review data rights before hosted/multiuser
distribution. This release does not promise complete global coverage, real-time
breaking news or social deletion reconciliation.

## Adding a source

Add a fixed registry entry only after verifying its official feed/API and
access terms. Reuse the RSS/Atom parser when possible; a new format gets an
isolated adapter with fixture tests for valid, empty, malformed, stale and
oversized responses. Do not add arbitrary user-supplied URLs to the API.
The World UI consumes the registry automatically; no source-specific panel is
required. Regenerate OpenAPI and frontend types when changing the API contract.

Next integrations need separate, tested work: structured economic release
calendars/consensus, EDGAR and 13Fs, full issuer UCITS holdings, historical
macro vintages, multilingual entity linking, licensed estimates and portfolio
risk-channel attribution. These are not implied by this news-feed catalog.
