# Security policy

QuantMind handles brokerage metadata and portfolio evidence, so treat every local cache as sensitive even though the repository contains only synthetic fixtures.

## Reporting a vulnerability

Please report vulnerabilities privately through the repository's GitHub Security Advisory workflow. Do not include account identifiers, tokens, holdings, broker logs, or exploit details in a public issue. If private reporting is not enabled, open a minimal issue asking the maintainer to enable it without disclosing the vulnerability.

## Supported version

Only the latest commit on `main` is supported during the pre-1.0 phase.

## Deployment boundary

- Bind the API to loopback unless you have added authentication, TLS, network controls, and an explicit threat model.
- Keep `.env`, IBKR credentials, account numbers, API tokens, local databases, Parquet files, logs, and portfolio exports out of Git.
- Use a dedicated read-only IBKR API client and verify the selected `QM_ACCOUNT_ID` before analysis.
- Treat third-party market data and scraped metadata as untrusted input. Preserve provenance and fail closed on malformed or stale evidence.
- Never use this software to submit orders; execution is intentionally outside the product boundary.
