"""Pure FX core (src/quantmind/fx.py) — TDD goldens, all hand-computed.

Pair-name convention: IBKR follows the standard FX priority
EUR > GBP > AUD > NZD > USD > CAD > CHF > JPY — the pair is named
higher-priority currency first (GBPUSD quotes USD per 1 GBP), and the
invert flag makes rate(currency→base) = close if not invert else 1/close.

Conversion goldens: with GBPUSD = 1.25 (1.25 USD per 1 GBP),
  $1000 → £800   (1000 × 1/1.25)
  £1000 → $1250  (1000 × 1.25)
"""

from __future__ import annotations

import pickle

import pytest

from quantmind.fx import FxConverter, fx_pair


# --- fx_pair: pair name + invert flag ---


def test_fx_pair_usd_to_gbp_is_gbpusd_inverted():
    # GBP outranks USD → pair GBPUSD (USD per GBP); USD→GBP needs 1/close.
    assert fx_pair("USD", "GBP") == ("GBPUSD", True)


def test_fx_pair_gbp_to_usd_is_gbpusd_direct():
    assert fx_pair("GBP", "USD") == ("GBPUSD", False)


def test_fx_pair_eur_to_gbp_is_eurgbp_direct():
    # EUR outranks GBP → EURGBP (GBP per EUR); EUR→GBP is the close itself.
    assert fx_pair("EUR", "GBP") == ("EURGBP", False)


def test_fx_pair_jpy_to_usd_is_usdjpy_inverted():
    # USD outranks JPY → USDJPY (JPY per USD); JPY→USD needs 1/close.
    assert fx_pair("JPY", "USD") == ("USDJPY", True)


def test_fx_pair_full_priority_order_over_usd_base():
    # EUR/GBP/AUD/NZD outrank USD (pair puts them first, direct);
    # CAD/CHF/JPY rank below USD (USD first, inverted).
    for cur in ("EUR", "GBP", "AUD", "NZD"):
        assert fx_pair(cur, "USD") == (f"{cur}USD", False)
    for cur in ("CAD", "CHF", "JPY"):
        assert fx_pair(cur, "USD") == (f"USD{cur}", True)


def test_fx_pair_unlisted_currency_ranks_below_the_majors():
    # A currency outside the priority list (e.g. SEK) ranks after JPY —
    # the major always leads the pair.
    assert fx_pair("SEK", "USD") == ("USDSEK", True)
    assert fx_pair("USD", "SEK") == ("USDSEK", False)


def test_fx_pair_same_currency_raises_value_error():
    # Identity is the CONVERTER's job (convert() short-circuits on base);
    # there is no such pair as GBPGBP.
    with pytest.raises(ValueError):
        fx_pair("GBP", "GBP")


# --- FxConverter: rate application, honest None on missing ---


def test_convert_identity_for_base_currency():
    c = FxConverter(base="GBP", rates={}, as_of=None)
    assert c.convert(1234.5, "GBP") == 1234.5


def test_convert_usd_to_gbp_golden():
    # GBPUSD 1.25 → rate(USD→GBP) = 1/1.25 = 0.8; $1000 → £800 exactly.
    c = FxConverter(base="GBP", rates={"USD": 0.8}, as_of="2026-07-24")
    assert c.convert(1000.0, "USD") == pytest.approx(800.0)


def test_convert_gbp_to_usd_golden():
    # GBPUSD 1.25 → rate(GBP→USD) = 1.25; £1000 → $1250 exactly.
    c = FxConverter(base="USD", rates={"GBP": 1.25}, as_of="2026-07-24")
    assert c.convert(1000.0, "GBP") == pytest.approx(1250.0)


def test_convert_missing_rate_is_honest_none_never_silent():
    c = FxConverter(base="GBP", rates={"USD": 0.8}, as_of="2026-07-24")
    assert c.convert(1000.0, "EUR") is None


def test_missing_names_unrated_currencies_and_skips_base():
    c = FxConverter(base="GBP", rates={"USD": 0.8}, as_of="2026-07-24")
    assert c.missing(["GBP", "USD", "EUR", "JPY"]) == {"EUR", "JPY"}


def test_converter_is_picklable_pure_core_law():
    # Pure-core law: risk/analytics/fx objects must be picklable (process
    # pools). Safe pickle use: same-process round-trip of our own object,
    # never deserialization of untrusted data.
    c = FxConverter(base="GBP", rates={"USD": 0.8}, as_of="2026-07-24")
    assert pickle.loads(pickle.dumps(c)) == c
