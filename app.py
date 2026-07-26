"""QuantMind — morning brief (Streamlit lab bench).

Disposable UI by design: everything here calls the tested core; when the React
cockpit lands, only this file retires. Renders exclusively from the local cache —
works with the Gateway down; staleness is shown, never hidden.

Run: uv run streamlit run app.py
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from quantmind.brief import build_brief
from quantmind.config import Settings
from quantmind.datastore.store import BarStore
from quantmind.risk.returns import rolling_beta, simple_returns

st.set_page_config(page_title="QuantMind — Morning Brief", layout="wide")

settings = Settings()
store = BarStore(settings.data_dir)
brief = build_brief(store, benchmark=settings.benchmark)

st.title("QuantMind — Morning Brief")

if not brief.tiles:
    st.info(
        "Cache is empty. Run `uv run python -m quantmind.sync_cli` with IB Gateway "
        "up to populate it, then reload."
    )
    st.stop()

stale_days = (pd.Timestamp.today().normalize() - brief.as_of).days
staleness = f"as of {brief.as_of.date()}"
if stale_days > 3:
    st.warning(f"Data is {stale_days} days old ({staleness}) — run the sync.")
else:
    st.caption(staleness)

# ---- macro tiles -------------------------------------------------------------
cols = st.columns(len(brief.tiles))
for col, tile in zip(cols, sorted(brief.tiles, key=lambda t: t.symbol)):
    col.metric(tile.symbol, f"{tile.last_close:,.2f}", f"{tile.change_1d:+.2%}")

st.divider()
left, right = st.columns(2)

# ---- rolling beta vs benchmark ----------------------------------------------
with left:
    st.subheader(f"Rolling 60d beta vs {settings.benchmark}")
    symbol_map = store.read_symbol_map()
    bench_bars, _ = store.read_bars(con_id=symbol_map[settings.benchmark], bar_size="1d")
    bench_ret = simple_returns(bench_bars["close"])
    fig = go.Figure()
    for symbol, con_id in sorted(symbol_map.items()):
        if symbol == settings.benchmark:
            continue
        bars, _ = store.read_bars(con_id=con_id, bar_size="1d")
        ret = simple_returns(bars["close"])
        aligned = pd.concat({"a": ret, "b": bench_ret}, axis=1).dropna()
        beta = rolling_beta(aligned["a"], aligned["b"], window=60)
        fig.add_trace(go.Scatter(x=beta.index, y=beta, name=symbol, mode="lines"))
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="beta")
    st.plotly_chart(fig, use_container_width=True)
    if brief.benchmark_es is not None:
        st.metric(
            f"{settings.benchmark} daily ES (97.5%, 5y)",
            f"{brief.benchmark_es:.2%}",
            help="Historical Expected Shortfall: average loss in the worst 2.5% of days",
        )

# ---- correlation matrix ------------------------------------------------------
with right:
    st.subheader("Correlation matrix (daily returns, 5y)")
    fig = px.imshow(
        brief.correlation.round(2),
        text_auto=True,
        color_continuous_scale="RdBu_r",
        zmin=-1,
        zmax=1,
        aspect="auto",
    )
    fig.update_layout(height=480, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)

# ---- Fed net liquidity (weekly cadence, labeled) ----------------------------
st.divider()
st.subheader("Fed net liquidity — WALCL − TGA − RRP ($bn, weekly cadence)")
st.caption("Regime context, not today's move — WALCL updates weekly (Engineering Constraint 17).")


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _net_liquidity():
    from quantmind.sources.fred import fetch_net_liquidity

    return fetch_net_liquidity()


try:
    nl = _net_liquidity()
    fig = px.line(nl.loc["2020":])
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0), showlegend=False)
    st.plotly_chart(fig, use_container_width=True)
except Exception as exc:  # tile degrades, never blocks the brief
    st.info(f"Liquidity tile unavailable ({type(exc).__name__}) — brief renders from cache regardless.")
