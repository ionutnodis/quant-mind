# Data sources and provenance

QuantMind keeps source identity beside cached observations and does not silently mix providers for the same instrument. Local cache contents, account identifiers, and portfolio snapshots belong in `data/` and must never be committed.

| Source | Current use | Access boundary | Stored evidence |
| --- | --- | --- | --- |
| Interactive Brokers | Selected-account positions, contract identity, adjusted daily bars, held-option contracts and chains | User-supplied Gateway/TWS session and market-data entitlements; read-only application path | conId, listing currency/exchange, bar metadata, quote/chain timestamps |
| ECB Data Portal | Daily reference FX rates used for dated base-currency normalization | Public HTTPS API; reference rates are informational, not executable marks; responses are capped at 20 MiB | Canonical USD-per-currency series plus provider URL, fetch time, and as-of manifest |
| justETF | Optional UCITS ETF share-class profile: name, issuer, domicile, TER, distribution, replication, and benchmark | Disabled by default with `QM_UCITS_METADATA_ENABLED=false`; supported European-domicile ISIN requests only; responses capped at 5 MiB; 30-day local cache | Typed profile with source URL and UTC fetch timestamp, kept separate from price provenance |
| FRED | US rates and liquidity macro series | Public keyless graph CSV endpoint | Named series and watermark |
| yfinance | Explicitly allowlisted adjusted-bar fallback | Opt-in per symbol; never overwrites a positive IBKR conId; sync fails closed without a valid quote unit | Deterministic pseudo-conId, provider tag, validated ISO currency, original quote unit, and applied price scale |
| [World Monitor catalog](docs/data-sources.md) | Central-bank, economic, energy, geopolitical and disaster events for the local attention desk | 14 public routes plus contact-gated SEC and explicitly enabled X/Reddit; separate from analytical price evidence | Bounded plain-text event metadata, original links, published/observed time and independent source health in `world.sqlite3` |

## UCITS roadmap boundary

The current integration enriches an ETF only after IBKR supplies an ETF classification and a checksum-valid ISIN whose country prefix is in the supported European-fund set. That prefix is a conservative routing heuristic, not proof of UCITS regulatory status. It does not yet download issuer holdings files, KIID/KID documents, index methodologies, securities-lending data, or distribution histories. Those sources require independent parsers, freshness contracts, licensing review, and golden fixtures before they can enter risk calculations.

Do not commit downloaded vendor datasets or republish third-party content. Contributors are responsible for complying with source terms, robots policies, entitlements, and applicable law. A source becoming technically reachable does not make its data redistributable.

## Calculation boundary

ECB observations are transformed from currency-per-EUR into the canonical USD-per-currency quote. A local-currency value `V_c` is expressed in base currency `b` as `V_b = V_c × q_c / q_b`. Every cached market mark is converted with the latest admissible FX observation on or before that mark's own observation date—not the request date—with a bounded seven-calendar-day carry for weekends and holidays.

FX refreshes publish immutable generation-addressed series first and atomically replace a manifest only after the full generation succeeds. Readers therefore see either the prior complete generation or the next complete generation, never a mixture.

Current FX can normalize current market value, but it cannot reconstruct the acquisition-date base cost of a foreign holding. QuantMind therefore exposes broker average cost and unrealized P&L in the instrument's local currency, while withholding base-currency unrealized P&L for foreign positions until lot-level historical FX or broker-reported base P&L is available.

London yfinance listings reported in `GBp` or `GBX` are pence-denominated. Their OHLC fields are scaled by `0.01` before storage, recorded as GBP, and retain the source quote unit and scale in instrument metadata. Volume is not scaled.

IBKR and yfinance universe phases isolate failures per symbol. Successful bars and mappings remain usable, failed symbols remain in the required-universe readiness check, and the sync reports a partial result. FRED, FX, UCITS, and option failures likewise do not erase successful independent phases.
