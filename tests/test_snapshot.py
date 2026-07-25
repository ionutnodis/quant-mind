from quantmind.core.snapshot import BookSnapshot
from quantmind.portfolio import Portfolio, Position


def _p():
    return Portfolio(
        positions=(Position(con_id=1, symbol="AAA", qty=10.0),), as_of="2026-07-25"
    )


def test_snapshot_carries_identity_valuation_and_currency():
    s = BookSnapshot.create(_p(), valuation_ts="2026-07-24T20:00:00Z", base_currency="USD")
    assert s.base_currency == "USD"
    assert s.valuation_ts.endswith("Z")  # UTC ISO policy
    assert len(s.snapshot_id) == 12


def test_snapshot_id_is_stable_for_identical_content():
    a = BookSnapshot.create(_p(), valuation_ts="2026-07-24T20:00:00Z", base_currency="USD")
    b = BookSnapshot.create(_p(), valuation_ts="2026-07-24T20:00:00Z", base_currency="USD")
    assert a.snapshot_id == b.snapshot_id


def test_snapshot_id_changes_when_positions_change():
    a = BookSnapshot.create(_p(), valuation_ts="2026-07-24T20:00:00Z", base_currency="USD")
    p2 = _p().with_position(Position(con_id=2, symbol="BBB", qty=1.0))
    b = BookSnapshot.create(p2, valuation_ts="2026-07-24T20:00:00Z", base_currency="USD")
    assert a.snapshot_id != b.snapshot_id
