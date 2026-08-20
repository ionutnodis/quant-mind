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
from quantmind.risk.contracts import (
    BaseCurrencyReturnDecompositionV1,
    ExposureAmountUnit,
    LinearBookRiskV1,
    ModeledVarianceUnit,
    NlvUnit,
    PerNlvExposureUnit,
    ProductionCovarianceV1,
    ReturnBasis,
    ReturnUnit,
    RiskEntityExposureV1,
    ScenarioGridV1,
    SpotShockUnit,
    VolatilityShiftUnit,
    alpha_scenario_grid_v1,
    decompose_base_currency_return,
)
from quantmind.snapshots.contracts import canonical_json_bytes


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthetic_book"
CANONICAL_BOOK_FIXTURE = FIXTURE_DIR / "canonical_book_v1.json"
RISK_CONTRACT_FIXTURE = FIXTURE_DIR / "risk_contract_v1.json"
SCENARIO_GRID_FIXTURE = FIXTURE_DIR / "scenario_grid_v1.json"


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


def test_base_currency_return_exposes_local_fx_interaction_and_exact_total():
    decomposition = decompose_base_currency_return(0.10, -0.05)
    assert decomposition.local == pytest.approx(0.10, rel=1e-8, abs=1e-12)
    assert decomposition.fx == pytest.approx(-0.05, rel=1e-8, abs=1e-12)
    assert decomposition.interaction == pytest.approx(-0.005, rel=1e-8, abs=1e-12)
    assert decomposition.total == pytest.approx(0.045, rel=1e-8, abs=1e-12)
    assert decomposition.unit is ReturnUnit.SIMPLE_RETURN_DECIMAL

    invalid = decomposition.model_dump(mode="json")
    invalid["interaction"] = 0.0
    with pytest.raises(ValueError):
        BaseCurrencyReturnDecompositionV1.model_validate_json(json.dumps(invalid))


def test_risk_entity_exposure_requires_signed_delta_equivalent_per_positive_nlv():
    exposure = RiskEntityExposureV1(
        risk_entity_id="ENERGY_EUR",
        signed_delta_equivalent_amount=-200_000.0,
        nlv=1_000_000.0,
        per_nlv_exposure=-0.2,
        amount_unit=ExposureAmountUnit.BASE_CURRENCY_DELTA_EQUIVALENT,
        nlv_unit=NlvUnit.BASE_CURRENCY_NLV,
        per_nlv_unit=PerNlvExposureUnit.BASE_CURRENCY_DELTA_EQUIVALENT_PER_NLV,
    )
    assert exposure.per_nlv_exposure == -0.2

    payload = exposure.model_dump(mode="json")
    payload["per_nlv_exposure"] = 0.2
    with pytest.raises(ValueError):
        RiskEntityExposureV1.model_validate_json(json.dumps(payload))

    payload = exposure.model_dump(mode="json")
    payload["per_nlv_unit"] = "FAIR_VALUE_WEIGHT"
    with pytest.raises(ValueError):
        RiskEntityExposureV1.model_validate_json(json.dumps(payload))

    payload = exposure.model_dump(mode="json")
    payload["nlv"] = 0.0
    with pytest.raises(ValueError):
        RiskEntityExposureV1.model_validate_json(json.dumps(payload))


def test_golden_production_covariance_equals_factor_implied_literal_matrix():
    payload = json.loads(RISK_CONTRACT_FIXTURE.read_text(encoding="utf-8"))["covariance"]
    covariance = ProductionCovarianceV1.model_validate_json(json.dumps(payload))

    assert [entity.risk_entity_id for entity in covariance.risk_entities] == [
        "TECH_USD",
        "ENERGY_EUR",
    ]
    assert {entity.return_basis for entity in covariance.risk_entities} == {
        ReturnBasis.BASE_CURRENCY_SIMPLE_RETURN
    }
    assert covariance.factor_names == ("MARKET", "SEMI_THEME")
    assert covariance.loadings == ((1.0, 0.5), (0.8, -0.2))
    assert covariance.factor_covariance == ((0.0001, 0.0), (0.0, 0.0004))
    assert covariance.residual_covariance == (
        (0.0001, 0.00002),
        (0.00002, 0.00009),
    )
    assert covariance.production_covariance == (
        (0.0003, 0.00006),
        (0.00006, 0.00017),
    )
    with pytest.raises(TypeError):
        covariance.production_covariance[0][0] = 0.0


def test_production_covariance_rejects_bad_shape_order_numbers_psd_units_and_identity():
    base = json.loads(RISK_CONTRACT_FIXTURE.read_text(encoding="utf-8"))["covariance"]
    mutations = []

    payload = copy.deepcopy(base)
    payload["loadings"] = [[1.0, 0.5]]
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["risk_entities"][1]["risk_entity_id"] = "TECH_USD"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["factor_covariance"][0][0] = float("nan")
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["production_covariance"][0][1] = 0.00007
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["factor_covariance"] = [[0.0001, 0.0002], [0.0002, 0.0001]]
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["production_covariance"][0][0] = 0.00031
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["residual_covariance"] = [[0.0001, 0.0], [0.0, 0.00009]]
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["covariance_unit"] = "ANNUAL_COVARIANCE"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["annualization_factor"] = 365
    mutations.append(payload)

    for invalid in mutations:
        with pytest.raises(ValueError):
            ProductionCovarianceV1.model_validate_json(json.dumps(invalid))


def test_golden_linear_risk_preserves_named_residual_and_signed_entity_shares():
    risk = LinearBookRiskV1.model_validate_json(
        RISK_CONTRACT_FIXTURE.read_text(encoding="utf-8")
    )

    assert [item.risk_entity_id for item in risk.exposures] == [
        "TECH_USD",
        "ENERGY_EUR",
    ]
    assert [item.per_nlv_exposure for item in risk.exposures] == [0.6, -0.2]
    assert risk.total_modeled_variance == pytest.approx(
        0.0001004, rel=1e-8, abs=1e-12
    )
    assert risk.total_modeled_variance_unit is ModeledVarianceUnit.PER_NLV_DAILY_SIMPLE_RETURN_VARIANCE
    assert [
        (item.factor_name, item.variance_contribution)
        for item in risk.factor_variance_contributions
    ] == [("MARKET", 0.00001936), ("SEMI_THEME", 0.00004624)]
    assert risk.unexplained_residual.factor_name == "UNEXPLAINED_RESIDUAL"
    assert risk.unexplained_residual.variance_contribution == pytest.approx(
        0.0000348, rel=1e-8, abs=1e-12
    )
    shares = {
        item.risk_entity_id: item.variance_share
        for item in risk.entity_variance_shares
    }
    assert shares["TECH_USD"] == pytest.approx(
        1.0039840637450199, rel=1e-8, abs=1e-12
    )
    assert shares["ENERGY_EUR"] == pytest.approx(
        -0.00398406374501992, rel=1e-8, abs=1e-12
    )
    assert sum(shares.values()) == pytest.approx(1.0, rel=1e-8, abs=1e-12)


def test_linear_risk_rejects_unit_order_component_residual_share_and_zero_variance_errors():
    base = json.loads(RISK_CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    mutations = []

    payload = copy.deepcopy(base)
    payload["total_modeled_variance_unit"] = "FAIR_VALUE_WEIGHT"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["exposures"].reverse()
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["covariance"]["risk_entities"].reverse()
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["factor_variance_contributions"][0]["variance_contribution"] = 0.00002
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["residual_covariance"] = "not-a-production-field"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["covariance"]["residual_covariance"] = [
        [0.0001, 0.0],
        [0.0, 0.00009],
    ]
    payload["covariance"]["production_covariance"] = [
        [0.0003, 0.00004],
        [0.00004, 0.00017],
    ]
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["entity_variance_shares"][1]["variance_share"] = 0.0
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["covariance"]["factor_covariance"] = [[0.0, 0.0], [0.0, 0.0]]
    payload["covariance"]["residual_covariance"] = [[0.0, 0.0], [0.0, 0.0]]
    payload["covariance"]["production_covariance"] = [[0.0, 0.0], [0.0, 0.0]]
    payload["total_modeled_variance"] = 0.0
    for contribution in payload["factor_variance_contributions"]:
        contribution["variance_contribution"] = 0.0
    payload["unexplained_residual"]["variance_contribution"] = 0.0
    mutations.append(payload)

    for invalid in mutations:
        with pytest.raises(ValueError):
            LinearBookRiskV1.model_validate_json(json.dumps(invalid))


def test_alpha_scenario_grid_equals_literal_45_node_coordinate_fixture():
    expected = ScenarioGridV1.model_validate_json(
        SCENARIO_GRID_FIXTURE.read_text(encoding="utf-8")
    )
    actual = alpha_scenario_grid_v1()

    assert actual == expected
    assert actual.version == "alpha_scenario_v1"
    assert actual.spot_shock_unit is SpotShockUnit.SIMPLE_RETURN_DECIMAL
    assert (
        actual.volatility_shift_unit
        is VolatilityShiftUnit.ABSOLUTE_IMPLIED_VOLATILITY_PERCENTAGE_POINTS
    )
    assert actual.iv_floor == Decimal("0.01")
    assert len(actual.nodes) == 45
    assert len(
        {(node.spot_shock, node.volatility_shift) for node in actual.nodes}
    ) == 45
    assert actual.nodes[0].model_dump(mode="json") == {
        "node_index": 0,
        "spot_shock": "-0.20",
        "volatility_shift": "-20",
    }
    assert actual.nodes[22].spot_shock == Decimal("0")
    assert actual.nodes[22].volatility_shift == Decimal("0")
    assert actual.nodes[44].model_dump(mode="json") == {
        "node_index": 44,
        "spot_shock": "0.20",
        "volatility_shift": "20",
    }
    assert all("pnl" not in type(node).model_fields for node in actual.nodes)


def test_alpha_scenario_grid_rejects_duplicate_missing_reordered_or_unit_confused_nodes():
    base = json.loads(SCENARIO_GRID_FIXTURE.read_text(encoding="utf-8"))
    mutations = []

    payload = copy.deepcopy(base)
    payload["nodes"][1]["spot_shock"] = "-0.20"
    payload["nodes"][1]["volatility_shift"] = "-20"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["nodes"].pop()
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["nodes"][0], payload["nodes"][1] = payload["nodes"][1], payload["nodes"][0]
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["spot_shock_unit"] = "PERCENT"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["volatility_shift_unit"] = "RELATIVE_PERCENT"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["iv_floor"] = "1.0"
    mutations.append(payload)

    payload = copy.deepcopy(base)
    payload["nodes"][0]["pnl"] = 0
    mutations.append(payload)

    for invalid in mutations:
        with pytest.raises(ValueError):
            ScenarioGridV1.model_validate_json(json.dumps(invalid))
