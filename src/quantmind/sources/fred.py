"""FRED macro series via the keyless fredgraph CSV endpoint.

Fed net liquidity = WALCL − TGA − RRP (Engineering Constraint / design premise 2).
The tile is labeled with its weekly cadence (Constraint 17) — this series
explains regimes, not today's move.
"""

from __future__ import annotations

import io
import urllib.request

import pandas as pd

_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# series id -> scale factor to $bn. H.4.1 series (WALCL, WTREGEN) are $mn;
# RRPONTSYD (temporary OMO release) is $bn. Getting these wrong once produced a
# -$823tn "net liquidity" — hence validate_net_liquidity below.
NET_LIQUIDITY_SERIES = {"WALCL": 1e-3, "WTREGEN": 1e-3, "RRPONTSYD": 1.0}


def parse_fred_csv(text: str) -> pd.Series:
    """FRED CSVs mark missing values with '.'; those rows are dropped."""
    df = pd.read_csv(io.StringIO(text), na_values=".")
    date_col, value_col = df.columns[0], df.columns[1]
    s = pd.Series(df[value_col].to_numpy(), index=pd.DatetimeIndex(df[date_col]))
    return s.dropna().astype(float)


def fetch_series(series_id: str, timeout: float = 15.0) -> pd.Series:  # pragma: no cover - network
    # macOS framework Python ships without system CA certs; certifi's bundle is authoritative here
    import ssl

    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    url = _CSV_URL.format(series_id=series_id)
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
        return parse_fred_csv(resp.read().decode())


def net_liquidity(walcl: pd.Series, tga: pd.Series, rrp: pd.Series) -> pd.Series:
    """WALCL − TGA − RRP on the union calendar; weekly series forward-fill. Units: caller-normalized ($bn)."""
    union = pd.DatetimeIndex(sorted(set(walcl.index) | set(tga.index) | set(rrp.index)))
    w = walcl.reindex(union).ffill()
    t = tga.reindex(union).ffill()
    r = rrp.reindex(union).ffill()
    return (w - t - r).dropna()


def validate_net_liquidity(nl: pd.Series) -> None:
    """Unit-error tripwire: net liquidity outside a generous $0.5tn–$20tn band means
    a series' units changed upstream — refuse to render garbage (staleness policy:
    a failed tile is visible, a wrong tile is poison)."""
    latest = float(nl.iloc[-1])
    if not (500.0 <= latest <= 20_000.0):
        raise ValueError(
            f"implausible net liquidity ${latest:,.0f}bn — check FRED series units"
        )


def fetch_net_liquidity() -> pd.Series:  # pragma: no cover - network
    walcl = fetch_series("WALCL") * NET_LIQUIDITY_SERIES["WALCL"]
    tga = fetch_series("WTREGEN") * NET_LIQUIDITY_SERIES["WTREGEN"]
    rrp = fetch_series("RRPONTSYD") * NET_LIQUIDITY_SERIES["RRPONTSYD"]
    nl = net_liquidity(walcl, tga, rrp)
    validate_net_liquidity(nl)
    return nl
