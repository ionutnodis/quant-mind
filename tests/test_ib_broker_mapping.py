"""Mapping logic from ib_async objects to our Portfolio — tested with fakes.

The network-touching paths are covered by the opt-in E2E smoke test.
"""

from types import SimpleNamespace

from quantmind.broker.ib_broker import positions_to_portfolio


def _ib_pos(con_id, symbol, qty, sec_type="STK", multiplier=""):
    contract = SimpleNamespace(conId=con_id, symbol=symbol, secType=sec_type, multiplier=multiplier)
    return SimpleNamespace(contract=contract, position=qty, avgCost=0.0)


def test_stock_position_maps_with_unit_multiplier():
    p = positions_to_portfolio([_ib_pos(265598, "AAPL", 100.0)], as_of="2026-07-25")
    assert len(p.positions) == 1
    pos = p.positions[0]
    assert pos.con_id == 265598
    assert pos.symbol == "AAPL"
    assert pos.qty == 100.0
    assert pos.sec_type == "STK"
    assert pos.multiplier == 1.0


def test_option_position_maps_string_multiplier_to_float():
    p = positions_to_portfolio(
        [_ib_pos(777, "AAPL", -2.0, sec_type="OPT", multiplier="100")], as_of="2026-07-25"
    )
    pos = p.positions[0]
    assert pos.sec_type == "OPT"
    assert pos.multiplier == 100.0
    assert pos.qty == -2.0


def test_empty_or_blank_multiplier_defaults_to_one():
    p = positions_to_portfolio([_ib_pos(1, "X", 1.0, multiplier="")], as_of="2026-07-25")
    assert p.positions[0].multiplier == 1.0


def test_zero_quantity_positions_are_dropped():
    # IBKR reports closed positions as qty 0 for the session — they are not holdings
    p = positions_to_portfolio(
        [_ib_pos(1, "X", 0.0), _ib_pos(2, "Y", 5.0)], as_of="2026-07-25"
    )
    assert [pos.con_id for pos in p.positions] == [2]
