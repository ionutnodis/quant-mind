import pytest

from quantmind.portfolio import Portfolio, Position


def test_weights_sum_to_one_for_market_value_weights():
    p = Portfolio(
        positions=(
            Position(con_id=1, symbol="AAA", qty=10),
            Position(con_id=2, symbol="BBB", qty=30),
        ),
        as_of="2026-07-25",
    )
    w = p.weights(prices={1: 100.0, 2: 100.0})
    assert w[1] == pytest.approx(0.25)
    assert w[2] == pytest.approx(0.75)
    assert w.sum() == pytest.approx(1.0)


def test_option_multiplier_respected_in_market_value():
    p = Portfolio(
        positions=(
            Position(con_id=1, symbol="AAA", qty=100),
            Position(con_id=9, symbol="AAA 250918C00100000", qty=1, sec_type="OPT", multiplier=100.0),
        ),
        as_of="2026-07-25",
    )
    # 100 shares @ 50 = 5000; 1 option contract @ 5 * 100 multiplier = 500
    mv = p.market_value(prices={1: 50.0, 9: 5.0})
    assert mv == pytest.approx(5500.0)


def test_empty_portfolio_gives_empty_weights_not_division_error():
    p = Portfolio(positions=(), as_of="2026-07-25")
    w = p.weights(prices={})
    assert len(w) == 0


def test_missing_price_raises_clear_error():
    p = Portfolio(positions=(Position(con_id=1, symbol="AAA", qty=1),), as_of="2026-07-25")
    with pytest.raises(KeyError, match="con_id 1"):
        p.weights(prices={})


def test_live_and_hypothetical_books_are_same_type():
    live = Portfolio(positions=(Position(con_id=1, symbol="AAA", qty=5),), as_of="2026-07-25")
    hypo = live.with_position(Position(con_id=2, symbol="BBB", qty=7))
    assert isinstance(hypo, Portfolio)
    assert len(hypo.positions) == 2
    assert len(live.positions) == 1  # immutable: original untouched
