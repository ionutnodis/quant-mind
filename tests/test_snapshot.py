from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quantmind.core.snapshot import BookSnapshot
from quantmind.portfolio import Portfolio, Position
from quantmind.snapshots.contracts import (
    ActiveSnapshotFreshness,
    GateEvidenceV1,
    GateStatus,
    RecoveryClass,
    RunOutcome,
    RunStage,
    SnapshotStatus,
    ValuationCutV1,
    canonical_json_bytes,
)


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


def test_canonical_json_bytes_normalizes_unicode_time_decimal_and_negative_zero():
    value = {
        "z": -0.0,
        "e\u0301": Decimal("1.2300"),
        "at": datetime(2026, 7, 24, 16, 15, tzinfo=timezone(timedelta(hours=-4))),
        "sequence": ("e\u0301", "before"),
    }

    assert canonical_json_bytes(value) == (
        b'{"at":"2026-07-24T20:15:00Z","sequence":["\xc3\xa9","before"],'
        b'"z":0.0,"\xc3\xa9":"1.2300"}'
    )


@pytest.mark.parametrize(
    "value",
    [
        {"at": datetime(2026, 7, 24, 20, 15)},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": Decimal("NaN")},
        {"e\u0301": 1, "\u00e9": 2},
    ],
)
def test_canonical_json_bytes_rejects_ambiguous_values(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_json_bytes(value)


def test_lifecycle_and_gate_vocabulary_serializes_to_exact_public_strings():
    assert [stage.value for stage in RunStage] == [
        "QUEUED",
        "INGESTING",
        "RECONCILING",
        "VALIDATING",
        "MODELING",
        "PUBLISHING",
    ]
    assert [outcome.value for outcome in RunOutcome] == [
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ]
    assert [status.value for status in SnapshotStatus] == ["BLESSED", "DEGRADED"]
    assert [freshness.value for freshness in ActiveSnapshotFreshness] == [
        "FRESH",
        "AGING",
        "STALE",
    ]
    assert [status.value for status in GateStatus] == [
        "PASSED",
        "WARNED",
        "REFUSED",
        "FAILED",
    ]
    assert [recovery.value for recovery in RecoveryClass] == [
        "USER_RESOLVABLE",
        "REFRESH_SOURCE_RESOLVABLE",
        "MODEL_OWNER_UPDATE",
        "MIXED",
    ]

    gate = GateEvidenceV1(
        gate_code="FX_RATE_INVALID",
        status=GateStatus.REFUSED,
        recovery_class=RecoveryClass.REFRESH_SOURCE_RESOLVABLE,
        evidence=("EUR quote missing",),
        recovery_action="Fetch from an approved FX source",
    )
    assert gate.model_dump(mode="json") == {
        "gate_code": "FX_RATE_INVALID",
        "status": "REFUSED",
        "recovery_class": "REFRESH_SOURCE_RESOLVABLE",
        "evidence": ["EUR quote missing"],
        "recovery_action": "Fetch from an approved FX source",
    }
    with pytest.raises(Exception):
        gate.status = GateStatus.PASSED
    with pytest.raises(Exception):
        GateEvidenceV1(
            gate_code="FX_RATE_INVALID",
            status=GateStatus.REFUSED,
            recovery_class=RecoveryClass.REFRESH_SOURCE_RESOLVABLE,
            evidence=("EUR quote missing",),
            recovery_action="Fetch from an approved FX source",
            unknown=True,
        )


def test_valuation_cut_requires_utc_resolvable_timezone_and_ordered_capture_window():
    cut = ValuationCutV1(
        target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        display_timezone="America/New_York",
        capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
    )
    assert cut.model_dump(mode="json") == {
        "target_cut_utc": "2026-07-24T20:15:00Z",
        "display_timezone": "America/New_York",
        "capture_start_utc": "2026-07-24T20:15:00Z",
        "capture_end_utc": "2026-07-24T20:20:00Z",
    }

    good = cut.model_dump(mode="python")
    invalid_updates = (
        {"target_cut_utc": datetime(2026, 7, 24, 20, 15)},
        {
            "capture_start_utc": datetime(
                2026, 7, 24, 16, 15, tzinfo=timezone(timedelta(hours=-4))
            )
        },
        {"display_timezone": "Mars/Olympus_Mons"},
        {
            "capture_start_utc": datetime(2026, 7, 24, 20, 21, tzinfo=UTC),
            "capture_end_utc": datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
        },
    )
    for update in invalid_updates:
        with pytest.raises(ValueError):
            ValuationCutV1.model_validate(good | update)
