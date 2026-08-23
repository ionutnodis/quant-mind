"""Pure numerical contracts for one-book production risk."""

from __future__ import annotations

import math
from decimal import Decimal
from enum import Enum
from typing import Literal

import numpy as np
from pydantic import field_validator, model_validator

from quantmind.snapshots.contracts import FrozenContractBase


RELATIVE_TOLERANCE = 1e-8
ABSOLUTE_TOLERANCE = 1e-12
PSD_TOLERANCE = 1e-10


class ReturnUnit(str, Enum):
    SIMPLE_RETURN_DECIMAL = "SIMPLE_RETURN_DECIMAL"


class ExposureAmountUnit(str, Enum):
    BASE_CURRENCY_DELTA_EQUIVALENT = "BASE_CURRENCY_DELTA_EQUIVALENT"


class NlvUnit(str, Enum):
    BASE_CURRENCY_NLV = "BASE_CURRENCY_NLV"


class PerNlvExposureUnit(str, Enum):
    BASE_CURRENCY_DELTA_EQUIVALENT_PER_NLV = (
        "BASE_CURRENCY_DELTA_EQUIVALENT_PER_NLV"
    )


class ReturnBasis(str, Enum):
    BASE_CURRENCY_SIMPLE_RETURN = "BASE_CURRENCY_SIMPLE_RETURN"


class CovarianceUnit(str, Enum):
    DAILY_SIMPLE_RETURN_COVARIANCE = "DAILY_SIMPLE_RETURN_COVARIANCE"


class ModeledVarianceUnit(str, Enum):
    PER_NLV_DAILY_SIMPLE_RETURN_VARIANCE = (
        "PER_NLV_DAILY_SIMPLE_RETURN_VARIANCE"
    )


class VarianceShareUnit(str, Enum):
    FRACTION_OF_TOTAL_MODELED_VARIANCE = "FRACTION_OF_TOTAL_MODELED_VARIANCE"


class SpotShockUnit(str, Enum):
    SIMPLE_RETURN_DECIMAL = "SIMPLE_RETURN_DECIMAL"


class VolatilityShiftUnit(str, Enum):
    ABSOLUTE_IMPLIED_VOLATILITY_PERCENTAGE_POINTS = (
        "ABSOLUTE_IMPLIED_VOLATILITY_PERCENTAGE_POINTS"
    )


class IvFloorUnit(str, Enum):
    ABSOLUTE_IMPLIED_VOLATILITY_DECIMAL = "ABSOLUTE_IMPLIED_VOLATILITY_DECIMAL"


def _require_finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _close(actual: float, expected: float) -> bool:
    return math.isclose(
        actual,
        expected,
        rel_tol=RELATIVE_TOLERANCE,
        abs_tol=ABSOLUTE_TOLERANCE,
    )


def _components_reconcile(total: float, components: tuple[float, ...]) -> bool:
    component_total = sum(components)
    scale = max(abs(total), sum(abs(item) for item in components))
    return abs(total - component_total) <= max(
        ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * scale
    )


class BaseCurrencyReturnDecompositionV1(FrozenContractBase):
    local: float
    fx: float
    interaction: float
    total: float
    unit: ReturnUnit

    @field_validator("local", "fx", "interaction", "total")
    @classmethod
    def _values_are_finite(cls, value: float, info) -> float:
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def _components_reconcile(self) -> "BaseCurrencyReturnDecompositionV1":
        expected_interaction = self.local * self.fx
        expected_total = (1.0 + self.local) * (1.0 + self.fx) - 1.0
        if not _close(self.interaction, expected_interaction):
            raise ValueError("return interaction must equal local times FX")
        if not _close(self.total, expected_total):
            raise ValueError("base return must preserve the multiplicative identity")
        return self


def decompose_base_currency_return(
    local_return: float, fx_return: float
) -> BaseCurrencyReturnDecompositionV1:
    """Return the exact simple-return local/FX/interaction decomposition."""

    _require_finite(local_return, "local_return")
    _require_finite(fx_return, "fx_return")
    interaction = local_return * fx_return
    total = (1.0 + local_return) * (1.0 + fx_return) - 1.0
    return BaseCurrencyReturnDecompositionV1(
        local=local_return,
        fx=fx_return,
        interaction=interaction,
        total=total,
        unit=ReturnUnit.SIMPLE_RETURN_DECIMAL,
    )


class RiskEntityExposureV1(FrozenContractBase):
    risk_entity_id: str
    signed_delta_equivalent_amount: float
    nlv: float
    per_nlv_exposure: float
    amount_unit: ExposureAmountUnit
    nlv_unit: NlvUnit
    per_nlv_unit: PerNlvExposureUnit

    @field_validator("risk_entity_id")
    @classmethod
    def _entity_id_is_explicit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("risk entity ID must be nonblank")
        return value

    @field_validator("signed_delta_equivalent_amount", "nlv", "per_nlv_exposure")
    @classmethod
    def _values_are_finite(cls, value: float, info) -> float:
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def _ratio_reconciles(self) -> "RiskEntityExposureV1":
        if self.nlv <= 0:
            raise ValueError("NLV must be positive")
        expected = self.signed_delta_equivalent_amount / self.nlv
        if not _close(self.per_nlv_exposure, expected):
            raise ValueError("per-NLV exposure ratio is inconsistent")
        return self


class RiskEntityDefinitionV1(FrozenContractBase):
    risk_entity_id: str
    return_basis: ReturnBasis

    @field_validator("risk_entity_id")
    @classmethod
    def _entity_id_is_explicit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("risk entity ID must be nonblank")
        return value


class ProductionCovarianceV1(FrozenContractBase):
    schema_version: Literal["production_covariance_v1"]
    risk_entities: tuple[RiskEntityDefinitionV1, ...]
    factor_names: tuple[str, ...]
    loadings: tuple[tuple[float, ...], ...]
    factor_covariance: tuple[tuple[float, ...], ...]
    residual_covariance: tuple[tuple[float, ...], ...]
    production_covariance: tuple[tuple[float, ...], ...]
    covariance_unit: CovarianceUnit
    annualization_factor: Literal[252]
    model_version: str

    @field_validator("factor_names")
    @classmethod
    def _factor_names_are_ordered_unique(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("factor names must be nonblank")
        if len(value) != len(set(value)):
            raise ValueError("factor names must be unique")
        return value

    @field_validator("model_version")
    @classmethod
    def _model_version_is_explicit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model version must be nonblank")
        return value

    @model_validator(mode="after")
    def _validate_factor_implied_covariance(self) -> "ProductionCovarianceV1":
        entity_ids = [item.risk_entity_id for item in self.risk_entities]
        if not entity_ids or len(entity_ids) != len(set(entity_ids)):
            raise ValueError("risk entity definitions must be nonempty and unique")
        entity_count = len(entity_ids)
        factor_count = len(self.factor_names)

        expected_shapes = {
            "loadings": (entity_count, factor_count),
            "factor_covariance": (factor_count, factor_count),
            "residual_covariance": (entity_count, entity_count),
            "production_covariance": (entity_count, entity_count),
        }
        arrays: dict[str, np.ndarray] = {}
        for name, expected_shape in expected_shapes.items():
            rows = getattr(self, name)
            if len(rows) != expected_shape[0] or any(
                len(row) != expected_shape[1] for row in rows
            ):
                raise ValueError(f"{name} has malformed shape")
            array = np.asarray(rows, dtype=float)
            if not np.isfinite(array).all():
                raise ValueError(f"{name} must contain only finite values")
            arrays[name] = array

        for name in (
            "factor_covariance",
            "residual_covariance",
            "production_covariance",
        ):
            matrix = arrays[name]
            if not np.allclose(
                matrix,
                matrix.T,
                rtol=RELATIVE_TOLERANCE,
                atol=ABSOLUTE_TOLERANCE,
            ):
                raise ValueError(f"{name} must be symmetric")
            if float(np.linalg.eigvalsh(matrix).min()) < -PSD_TOLERANCE:
                raise ValueError(f"{name} must be PSD within tolerance")

        implied = (
            arrays["loadings"]
            @ arrays["factor_covariance"]
            @ arrays["loadings"].T
            + arrays["residual_covariance"]
        )
        if not np.allclose(
            arrays["production_covariance"],
            implied,
            rtol=RELATIVE_TOLERANCE,
            atol=ABSOLUTE_TOLERANCE,
        ):
            raise ValueError("production covariance is not factor-implied")
        return self


class VarianceContributionV1(FrozenContractBase):
    factor_name: str
    variance_contribution: float
    unit: ModeledVarianceUnit

    @field_validator("factor_name")
    @classmethod
    def _factor_name_is_explicit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("factor name must be nonblank")
        return value

    @field_validator("variance_contribution")
    @classmethod
    def _contribution_is_finite(cls, value: float) -> float:
        return _require_finite(value, "variance_contribution")


class UnexplainedResidualContributionV1(FrozenContractBase):
    factor_name: Literal["UNEXPLAINED_RESIDUAL"]
    variance_contribution: float
    unit: ModeledVarianceUnit

    @field_validator("variance_contribution")
    @classmethod
    def _contribution_is_finite(cls, value: float) -> float:
        return _require_finite(value, "variance_contribution")


class EntityVarianceShareV1(FrozenContractBase):
    risk_entity_id: str
    variance_share: float
    unit: VarianceShareUnit

    @field_validator("risk_entity_id")
    @classmethod
    def _entity_id_is_explicit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("risk entity ID must be nonblank")
        return value

    @field_validator("variance_share")
    @classmethod
    def _share_is_finite(cls, value: float) -> float:
        return _require_finite(value, "variance_share")


class LinearBookRiskV1(FrozenContractBase):
    schema_version: Literal["linear_book_risk_v1"]
    covariance: ProductionCovarianceV1
    exposures: tuple[RiskEntityExposureV1, ...]
    total_modeled_variance: float
    total_modeled_variance_unit: ModeledVarianceUnit
    factor_variance_contributions: tuple[VarianceContributionV1, ...]
    unexplained_residual: UnexplainedResidualContributionV1
    entity_variance_shares: tuple[EntityVarianceShareV1, ...]

    @field_validator("total_modeled_variance")
    @classmethod
    def _total_is_finite(cls, value: float) -> float:
        return _require_finite(value, "total_modeled_variance")

    @model_validator(mode="after")
    def _validate_signed_risk_decomposition(self) -> "LinearBookRiskV1":
        entity_ids = tuple(
            entity.risk_entity_id for entity in self.covariance.risk_entities
        )
        exposure_ids = tuple(item.risk_entity_id for item in self.exposures)
        if exposure_ids != entity_ids:
            raise ValueError("exposure order must match covariance risk-entity order")
        if len({item.nlv for item in self.exposures}) != 1:
            raise ValueError("all risk exposures must reference the same positive NLV")

        factor_names = tuple(self.covariance.factor_names)
        contribution_names = tuple(
            item.factor_name for item in self.factor_variance_contributions
        )
        if contribution_names != factor_names:
            raise ValueError("named contribution order must match covariance factors")
        share_ids = tuple(item.risk_entity_id for item in self.entity_variance_shares)
        if share_ids != entity_ids:
            raise ValueError("entity share order must match covariance risk entities")

        x = np.asarray([item.per_nlv_exposure for item in self.exposures], dtype=float)
        production = np.asarray(self.covariance.production_covariance, dtype=float)
        loadings = np.asarray(self.covariance.loadings, dtype=float)
        factor_covariance = np.asarray(self.covariance.factor_covariance, dtype=float)
        residual = np.asarray(self.covariance.residual_covariance, dtype=float)

        expected_total = float(x @ production @ x)
        if expected_total <= ABSOLUTE_TOLERANCE:
            raise ValueError("total variance is zero or too near zero for attribution")
        if not _close(self.total_modeled_variance, expected_total):
            raise ValueError("total modeled variance does not match production covariance")

        book_factor_exposure = loadings.T @ x
        expected_factors = book_factor_exposure * (
            factor_covariance @ book_factor_exposure
        )
        supplied_factors = np.asarray(
            [
                item.variance_contribution
                for item in self.factor_variance_contributions
            ],
            dtype=float,
        )
        if not np.allclose(
            supplied_factors,
            expected_factors,
            rtol=RELATIVE_TOLERANCE,
            atol=ABSOLUTE_TOLERANCE,
        ):
            raise ValueError("named factor variance contributions are inconsistent")

        expected_residual = float(x @ residual @ x)
        if not _close(
            self.unexplained_residual.variance_contribution, expected_residual
        ):
            raise ValueError("full unexplained residual contribution is inconsistent")
        all_components = tuple(float(item) for item in supplied_factors) + (
            self.unexplained_residual.variance_contribution,
        )
        if not _components_reconcile(self.total_modeled_variance, all_components):
            raise ValueError("named plus unexplained variance does not reconcile")

        expected_share_values = x * (production @ x) / expected_total
        supplied_shares = np.asarray(
            [item.variance_share for item in self.entity_variance_shares], dtype=float
        )
        if not np.allclose(
            supplied_shares,
            expected_share_values,
            rtol=RELATIVE_TOLERANCE,
            atol=ABSOLUTE_TOLERANCE,
        ):
            raise ValueError("signed entity variance shares are inconsistent")
        if not _close(float(supplied_shares.sum()), 1.0):
            raise ValueError("entity variance shares must sum to one")
        return self


_SPOT_SHOCKS = tuple(
    Decimal(value)
    for value in (
        "-0.20",
        "-0.15",
        "-0.10",
        "-0.05",
        "0",
        "0.05",
        "0.10",
        "0.15",
        "0.20",
    )
)
_VOLATILITY_SHIFTS = tuple(
    Decimal(value) for value in ("-20", "-10", "0", "10", "20")
)


class ScenarioGridNodeV1(FrozenContractBase):
    node_index: int
    spot_shock: Decimal
    volatility_shift: Decimal

    @field_validator("spot_shock", "volatility_shift")
    @classmethod
    def _coordinate_is_finite(cls, value: Decimal, info) -> Decimal:
        if not value.is_finite():
            raise ValueError(f"{info.field_name} must be finite")
        return value


class ScenarioGridV1(FrozenContractBase):
    version: Literal["alpha_scenario_v1"]
    spot_shock_unit: SpotShockUnit
    volatility_shift_unit: VolatilityShiftUnit
    iv_floor: Decimal
    iv_floor_unit: IvFloorUnit
    nodes: tuple[ScenarioGridNodeV1, ...]

    @field_validator("iv_floor")
    @classmethod
    def _iv_floor_is_one_percent(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value != Decimal("0.01"):
            raise ValueError("alpha scenario IV floor must be exactly 1%")
        return value

    @model_validator(mode="after")
    def _grid_is_exact_and_ordered(self) -> "ScenarioGridV1":
        expected_coordinates = tuple(
            (spot, volatility)
            for spot in _SPOT_SHOCKS
            for volatility in _VOLATILITY_SHIFTS
        )
        coordinates = tuple(
            (node.spot_shock, node.volatility_shift) for node in self.nodes
        )
        if len(coordinates) != 45 or len(set(coordinates)) != 45:
            raise ValueError("alpha scenario grid must contain 45 unique nodes")
        if tuple(node.node_index for node in self.nodes) != tuple(range(45)):
            raise ValueError("alpha scenario node indices must be stable and ordered")
        if coordinates != expected_coordinates:
            raise ValueError("alpha scenario coordinates or spot-major order are invalid")
        return self


def alpha_scenario_grid_v1() -> ScenarioGridV1:
    """Return the fixed coordinate-only M1.5 diagnostic grid."""

    nodes = tuple(
        ScenarioGridNodeV1(
            node_index=index,
            spot_shock=spot,
            volatility_shift=volatility,
        )
        for index, (spot, volatility) in enumerate(
            (spot, volatility)
            for spot in _SPOT_SHOCKS
            for volatility in _VOLATILITY_SHIFTS
        )
    )
    return ScenarioGridV1(
        version="alpha_scenario_v1",
        spot_shock_unit=SpotShockUnit.SIMPLE_RETURN_DECIMAL,
        volatility_shift_unit=(
            VolatilityShiftUnit.ABSOLUTE_IMPLIED_VOLATILITY_PERCENTAGE_POINTS
        ),
        iv_floor=Decimal("0.01"),
        iv_floor_unit=IvFloorUnit.ABSOLUTE_IMPLIED_VOLATILITY_DECIMAL,
        nodes=nodes,
    )
