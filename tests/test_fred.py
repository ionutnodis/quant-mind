import numpy as np
import pandas as pd
import pytest

from quantmind.sources.fred import net_liquidity, parse_fred_csv

CSV = """DATE,DGS3MO
2026-07-20,4.35
2026-07-21,.
2026-07-22,4.40
"""


def test_parse_fred_csv_drops_missing_dot_values():
    s = parse_fred_csv(CSV)
    assert len(s) == 2
    assert s.loc["2026-07-20"] == pytest.approx(4.35)
    assert s.loc["2026-07-22"] == pytest.approx(4.40)
    assert "2026-07-21" not in s.index.strftime("%Y-%m-%d")


def test_net_liquidity_walcl_minus_tga_minus_rrp_with_ffill_alignment():
    # WALCL is weekly; TGA/RRP daily. Units already normalized to $bn by caller.
    walcl = pd.Series([7000.0], index=pd.DatetimeIndex(["2026-07-20"]))
    tga = pd.Series(
        [700.0, 710.0, 705.0],
        index=pd.DatetimeIndex(["2026-07-20", "2026-07-21", "2026-07-22"]),
    )
    rrp = pd.Series(
        [300.0, 290.0, 280.0],
        index=pd.DatetimeIndex(["2026-07-20", "2026-07-21", "2026-07-22"]),
    )
    nl = net_liquidity(walcl, tga, rrp)
    assert nl.loc["2026-07-20"] == pytest.approx(6000.0)
    # weekly WALCL forward-fills across the week
    assert nl.loc["2026-07-21"] == pytest.approx(7000.0 - 710.0 - 290.0)
    assert nl.loc["2026-07-22"] == pytest.approx(7000.0 - 705.0 - 280.0)


def test_net_liquidity_validator_rejects_unit_errors():
    from quantmind.sources.fred import validate_net_liquidity

    good = pd.Series([5700.0], index=pd.DatetimeIndex(["2026-07-24"]))
    validate_net_liquidity(good)  # no raise

    unit_bug = pd.Series([-822_876.0], index=pd.DatetimeIndex(["2026-07-24"]))
    with pytest.raises(ValueError, match="implausible"):
        validate_net_liquidity(unit_bug)
