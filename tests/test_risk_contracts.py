from __future__ import annotations

import copy
import json
import os
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

from quantmind.book.contracts import (
    AssetClass,
    CanonicalBookV1,
    ExerciseStyle,
    InstrumentV1,
    PositionV1,
    ReconciliationStatus,
    SettlementStyle,
    convert_via_usd,
)
from quantmind.snapshots.contracts import canonical_json_bytes


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic_book"
CANONICAL_BOOK_FIXTURE = FIXTURE_DIR / "canonical_book_v1.json"


def _canonical_book_payload() -> dict:
    return json.loads(CANONICAL_BOOK_FIXTURE.read_text(encoding="utf-8"))


def _validate_book_payload(payload: dict) -> CanonicalBookV1:
    return CanonicalBookV1.model_validate_json(json.dumps(payload))


def test_golden_canonical_book_preserves_identity_options_and_exact_reconciliation():
    book = CanonicalBookV1.model_validate_json(
        CANONICAL_BOOK_FIXTURE.read_text(encoding="utf-8")
    )

    assert book.schema_version == "canonical_book_v1"
    assert book.book_id == "SYNTHETIC_ONE_BOOK"
    assert [account.account_id for account in book.accounts] == [
        "SYNTH_ACCOUNT_US",
        "SYNTH_ACCOUNT_EU",
    ]
    assert [instrument.instrument_id for instrument in book.instruments[:2]] == [
        "SYNTH_US_EQ",
        "SYNTH_EU_ETF",
    ]
    assert book.instruments[0].asset_class is AssetClass.EQUITY
    assert book.instruments[1].asset_class is AssetClass.ETF

    options = [item for item in book.instruments if item.asset_class is AssetClass.OPTION]
    assert len(options) == 8
    assert {item.option_terms.exercise_style for item in options} == {
        ExerciseStyle.AMERICAN
    }
    assert {item.option_terms.settlement_style for item in options} == {
        SettlementStyle.PHYSICAL
    }
    assert {item.option_terms.contract_multiplier for item in options} == {
        Decimal("100")
    }
    assert {position.strategy_group_id for position in book.positions} == {
        None,
        "COVERED_CALL",
        "SHORT_PUT",
        "REVERSAL",
        "IRON_CONDOR",
    }
    assert sum(
        position.local_market_value
        for position in book.positions
        if position.strategy_group_id is not None
    ) == Decimal("-34500.00")
    assert sum(position.base_market_value for position in book.positions) == Decimal(
        "685500.00"
    )
    assert sum(balance.base_amount for balance in book.cash_balances) == Decimal(
        "314500.00"
    )
    assert book.source_nlv == book.normalized_nlv == Decimal("1000000.00")
    assert book.reconciliation.status is ReconciliationStatus.RECONCILED
    assert convert_via_usd(
        Decimal("200000"), "EUR", "USD", {"USD": Decimal("1"), "EUR": Decimal("1.10")}
    ) == Decimal("220000.00")


def test_canonical_book_round_trip_and_canonical_bytes_are_hash_seed_stable():
    raw = CANONICAL_BOOK_FIXTURE.read_text(encoding="utf-8")
    book = CanonicalBookV1.model_validate_json(raw)
    dumped = book.model_dump(mode="json", exclude_none=False)
    restored = CanonicalBookV1.model_validate_json(json.dumps(dumped))
    assert restored == book
    assert canonical_json_bytes(restored) == canonical_json_bytes(book)

    program = (
        "from pathlib import Path; "
        "from quantmind.book.contracts import CanonicalBookV1; "
        "from quantmind.snapshots.contracts import canonical_json_bytes; "
        f"p=Path({str(CANONICAL_BOOK_FIXTURE)!r}); "
        "print(canonical_json_bytes(CanonicalBookV1.model_validate_json(p.read_text())).hex())"
    )
    outputs = []
    for seed in ("1", "2", "8675309"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        outputs.append(
            subprocess.run(
                [sys.executable, "-c", program],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout.strip()
        )
    assert len(set(outputs)) == 1


def test_usd_triangulation_uses_usd_per_currency_for_non_usd_base():
    quotes = {
        "USD": Decimal("1"),
        "EUR": Decimal("1.10"),
        "GBP": Decimal("1.25"),
    }
    assert convert_via_usd(Decimal("110"), "EUR", "GBP", quotes) == Decimal(
        "96.8"
    )
    assert convert_via_usd(Decimal("125"), "GBP", "EUR", quotes) == Decimal(
        "142.0454545454545454545454545"
    )
    with pytest.raises(ValueError):
        convert_via_usd(Decimal("1"), "JPY", "USD", quotes)


def test_canonical_book_rejects_identity_reference_fx_value_and_time_violations():
    mutations = []

    payload = _canonical_book_payload()
    payload["book_id"] = " "
    mutations.append(("blank book identity", payload))

    payload = _canonical_book_payload()
    payload["instruments"][1]["instrument_id"] = payload["instruments"][0][
        "instrument_id"
    ]
    mutations.append(("duplicate instrument identity", payload))

    payload = _canonical_book_payload()
    payload["positions"][0]["account_id"] = "MISSING_ACCOUNT"
    mutations.append(("missing account reference", payload))

    payload = _canonical_book_payload()
    payload["instruments"][2]["option_terms"][
        "underlying_instrument_id"
    ] = "MISSING_UNDERLYING"
    mutations.append(("missing option underlying", payload))

    payload = _canonical_book_payload()
    payload["fx_quotes"] = payload["fx_quotes"][:1]
    mutations.append(("missing EUR conversion", payload))

    payload = _canonical_book_payload()
    payload["fx_quotes"].append(copy.deepcopy(payload["fx_quotes"][1]))
    payload["fx_quotes"][-1]["fx_observation_id"] = "SYNTH_FX_EUR_DUP"
    mutations.append(("duplicate EUR conversion", payload))

    payload = _canonical_book_payload()
    payload["fx_quotes"][0]["usd_per_currency"] = "0.999"
    mutations.append(("USD quote not one", payload))

    payload = _canonical_book_payload()
    payload["positions"][0]["local_market_value"] = "499999.99"
    mutations.append(("wrong market value equation", payload))

    payload = _canonical_book_payload()
    payload["instruments"][2]["option_terms"]["contract_multiplier"] = "99"
    mutations.append(("wrong option multiplier equation", payload))

    payload = _canonical_book_payload()
    payload["positions"][0]["mark_effective_at_utc"] = "2026-07-24T20:15:01Z"
    mutations.append(("effective mark after cut", payload))

    payload = _canonical_book_payload()
    payload["positions"][0]["mark_observed_at_utc"] = "2026-07-24T20:21:00Z"
    mutations.append(("post-cut observation outside capture", payload))

    payload = _canonical_book_payload()
    payload["positions"][0]["mark_effective_at_utc"] = "2026-07-24T20:15:00"
    mutations.append(("naive mark timestamp", payload))

    payload = _canonical_book_payload()
    payload["unexpected"] = True
    mutations.append(("unknown field", payload))

    payload = _canonical_book_payload()
    payload["source_nlv"] = "999999.00"
    payload["reconciliation"]["source_nlv"] = "999999.00"
    payload["reconciliation"]["normalized_minus_source_nlv"] = "1.00"
    payload["reconciliation"]["status"] = "WITHIN_TOLERANCE"
    mutations.append(("reconciliation beyond one cent", payload))

    payload = _canonical_book_payload()
    payload["reconciliation"]["status"] = "WITHIN_TOLERANCE"
    mutations.append(("wrong declared reconciliation status", payload))

    for label, invalid in mutations:
        with pytest.raises(ValueError, match="."):
            _validate_book_payload(invalid)


def test_canonical_book_rejects_ambiguous_option_shape_and_zero_quantity():
    payload = _canonical_book_payload()
    option = payload["instruments"][2]
    option["option_terms"] = None
    with pytest.raises(ValueError):
        InstrumentV1.model_validate_json(json.dumps(option))

    payload = _canonical_book_payload()
    equity = payload["instruments"][0]
    equity["option_terms"] = copy.deepcopy(
        payload["instruments"][2]["option_terms"]
    )
    with pytest.raises(ValueError):
        InstrumentV1.model_validate_json(json.dumps(equity))

    payload = _canonical_book_payload()
    position = payload["positions"][0]
    position["quantity"] = "0"
    with pytest.raises(ValueError):
        PositionV1.model_validate_json(json.dumps(position))
