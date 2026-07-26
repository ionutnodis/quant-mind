"""Data-provider implementations behind `providers.base.DataProvider`
(Task A2). IBKR (broker/sync.py) stays the primary path; providers in this
package are free-first fallbacks used only for instruments/series IBKR can't
serve, gated by an explicit config allowlist (Global Constraints: free-first
data, single-provenance law)."""
