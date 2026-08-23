import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quantmind.core.snapshot import BookSnapshot
from quantmind.book.legacy import (
    InvalidLegacyBookRefError,
    LegacyBookCorruptError,
    LegacyBookNotFoundError,
    NonRegularLegacyBookFileError,
    adapt_legacy_book_snapshot,
    read_legacy_book,
)
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
from quantmind.snapshots.input_artifacts import ReproducibilityClass


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


def test_legacy_adapter_preserves_the_frozen_12_character_golden_without_claiming_reproducibility():
    snapshot = BookSnapshot.create(
        _p(), valuation_ts="2026-07-24T20:00:00Z", base_currency="USD"
    )
    reference = adapt_legacy_book_snapshot(snapshot)

    assert snapshot.snapshot_id == reference.book_ref == "e37bc82e4fe3"
    assert reference.position_count == 1
    assert reference.positions[0].symbol == "AAA"
    assert reference.positions[0].quantity == Decimal("10.0")
    assert reference.reproducibility_class is ReproducibilityClass.NON_REPRODUCIBLE_LEGACY
    assert reference.legacy_content_sha256 is None
    assert "MISSING_COMPLETE_MARKS" in reference.limitations
    assert "MISSING_FX_OBSERVATIONS" in reference.limitations
    assert "ANALYTICAL_SNAPSHOT_PUBLICATION" in reference.refused_outputs
    payload = reference.model_dump(mode="json")
    assert "snapshot_id" not in payload
    assert "canonical_book" not in payload


def test_legacy_adapter_marks_incomplete_option_terms_as_a_specific_refusal():
    portfolio = Portfolio(
        positions=(
            Position(
                con_id=7,
                symbol="AAA",
                qty=-1.0,
                multiplier=100.0,
                sec_type="OPT",
            ),
        ),
        as_of="2026-07-25",
    )
    snapshot = BookSnapshot.create(
        portfolio,
        valuation_ts="2026-07-24T20:00:00Z",
        base_currency="USD",
    )
    reference = adapt_legacy_book_snapshot(snapshot)
    assert "MISSING_COMPLETE_OPTION_TERMS" in reference.limitations
    assert "OPTION_REPRICING" in reference.refused_outputs


def test_read_legacy_book_validates_exact_bytes_shape_and_embedded_identity(tmp_path):
    book_ref = "abcdef012345"
    payload = {
        "snapshot_id": book_ref,
        "valuation_ts": "2026-07-24T20:00:00Z",
        "base_currency": "USD",
        "positions": [
            {
                "con_id": 1,
                "symbol": "AAA",
                "qty": 10.0,
                "sec_type": "STK",
                "multiplier": 1.0,
                "strike": None,
                "expiry": None,
                "right": None,
            }
        ],
    }
    raw = json.dumps(payload, indent=2).encode("utf-8")
    books = tmp_path / "books"
    books.mkdir()
    (books / f"{book_ref}.json").write_bytes(raw)

    reference = read_legacy_book(tmp_path, book_ref)
    assert reference.book_ref == book_ref
    assert reference.legacy_content_sha256 == (
        "63536f2b1a9071a9041c72d4ed9fc070077588006b774badeac07cd358f1dad9"
    )
    assert reference.positions[0].security_type == "STK"

    payload["snapshot_id"] = "111111111111"
    (books / f"{book_ref}.json").write_text(json.dumps(payload))
    with pytest.raises(LegacyBookCorruptError, match="embedded"):
        read_legacy_book(tmp_path, book_ref)


def test_read_legacy_book_rejects_malformed_missing_duplicate_and_nonregular_sources(tmp_path):
    with pytest.raises(InvalidLegacyBookRefError):
        read_legacy_book(tmp_path, "../instruments")
    with pytest.raises(LegacyBookNotFoundError):
        read_legacy_book(tmp_path, "abcdef012345")
    assert not (tmp_path / "books").exists()

    books = tmp_path / "books"
    books.mkdir()
    book_ref = "abcdef012345"
    duplicate = (
        b'{"snapshot_id":"abcdef012345","snapshot_id":"abcdef012345",'
        b'"valuation_ts":"2026-07-24T20:00:00Z","base_currency":"USD",'
        b'"positions":[]}'
    )
    (books / f"{book_ref}.json").write_bytes(duplicate)
    with pytest.raises(LegacyBookCorruptError, match="duplicate"):
        read_legacy_book(tmp_path, book_ref)

    (books / f"{book_ref}.json").unlink()
    symlink_target = tmp_path / "legacy-target.json"
    symlink_target.write_text("{}")
    (books / f"{book_ref}.json").symlink_to(symlink_target)
    with pytest.raises(NonRegularLegacyBookFileError):
        read_legacy_book(tmp_path, book_ref)


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), b'"0"'),
        (Decimal("0.00"), b'"0.00"'),
        (Decimal("-0.00"), b'"0.00"'),
    ],
)
def test_canonical_decimal_zero_preserves_scale_while_normalizing_sign(value, expected):
    assert canonical_json_bytes(value) == expected


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


def test_valuation_cut_rejects_target_after_capture_end():
    with pytest.raises(ValueError):
        ValuationCutV1(
            target_cut_utc=datetime(2026, 7, 24, 20, 21, tzinfo=UTC),
            display_timezone="America/New_York",
            capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
            capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
        )

    post_cut_capture = ValuationCutV1(
        target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        display_timezone="America/New_York",
        capture_start_utc=datetime(2026, 7, 24, 20, 16, tzinfo=UTC),
        capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
    )
    assert post_cut_capture.capture_start_utc > post_cut_capture.target_cut_utc
