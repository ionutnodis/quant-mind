"""Provider-neutral, decimal-exact canonical book contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from quantmind.snapshots.contracts import FrozenContractBase, ValuationCutV1


ONE_CENT = Decimal("0.01")


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    OPTION = "OPTION"


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class ExerciseStyle(str, Enum):
    AMERICAN = "AMERICAN"
    EUROPEAN = "EUROPEAN"


class SettlementStyle(str, Enum):
    PHYSICAL = "PHYSICAL"
    CASH = "CASH"


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"


def _require_nonblank(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value


def _require_currency(value: str) -> str:
    if len(value) != 3 or not value.isalpha() or value != value.upper():
        raise ValueError("currency must be an uppercase three-letter code")
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be explicitly UTC")
    return value


def _require_finite(value: Decimal, name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def convert_via_usd(
    amount: Decimal,
    currency: str,
    base_currency: str,
    usd_per_currency: Mapping[str, Decimal],
) -> Decimal:
    """Convert ``amount`` using q[c] / q[base], where q is USD per currency."""

    _require_finite(amount, "amount")
    _require_currency(currency)
    _require_currency(base_currency)
    try:
        currency_quote = _require_finite(
            usd_per_currency[currency], f"{currency} quote"
        )
        base_quote = _require_finite(
            usd_per_currency[base_currency], f"{base_currency} quote"
        )
    except KeyError as error:
        raise ValueError(f"missing USD-per-currency quote: {error.args[0]}") from error
    if currency_quote <= 0 or base_quote <= 0:
        raise ValueError("USD-per-currency quotes must be positive")
    return amount * currency_quote / base_quote


class AccountV1(FrozenContractBase):
    account_id: str
    source_id: str
    account_label: str

    @field_validator("account_id", "source_id", "account_label")
    @classmethod
    def _identity_is_explicit(cls, value: str, info) -> str:
        return _require_nonblank(value, info.field_name)


class ExternalIdentifierV1(FrozenContractBase):
    namespace: str
    value: str

    @field_validator("namespace", "value")
    @classmethod
    def _identifier_is_explicit(cls, value: str, info) -> str:
        return _require_nonblank(value, info.field_name)


class OptionTermsV1(FrozenContractBase):
    underlying_instrument_id: str
    expiry_date: date
    strike: Decimal
    right: OptionRight
    exercise_style: ExerciseStyle
    settlement_style: SettlementStyle
    contract_multiplier: Decimal

    @field_validator("underlying_instrument_id")
    @classmethod
    def _underlying_is_explicit(cls, value: str) -> str:
        return _require_nonblank(value, "underlying_instrument_id")

    @field_validator("strike", "contract_multiplier")
    @classmethod
    def _positive_decimal(cls, value: Decimal, info) -> Decimal:
        _require_finite(value, info.field_name)
        if value <= 0:
            raise ValueError(f"{info.field_name} must be positive")
        return value


class InstrumentV1(FrozenContractBase):
    instrument_id: str
    risk_entity_id: str
    symbol: str
    mic: str
    venue: str
    asset_class: AssetClass
    trading_currency: str
    settlement_currency: str
    isin: str | None
    external_identifiers: tuple[ExternalIdentifierV1, ...]
    valid_from_utc: datetime
    valid_to_utc: datetime | None
    option_terms: OptionTermsV1 | None

    @field_validator("instrument_id", "risk_entity_id", "symbol", "mic", "venue")
    @classmethod
    def _identity_is_explicit(cls, value: str, info) -> str:
        return _require_nonblank(value, info.field_name)

    @field_validator("isin")
    @classmethod
    def _optional_isin_is_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonblank(value, "isin")

    @field_validator("trading_currency", "settlement_currency")
    @classmethod
    def _currency_is_explicit(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("valid_from_utc", "valid_to_utc")
    @classmethod
    def _validity_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _instrument_shape_matches_asset_class(self) -> "InstrumentV1":
        if self.asset_class is AssetClass.OPTION and self.option_terms is None:
            raise ValueError("options require complete option terms")
        if self.asset_class is not AssetClass.OPTION and self.option_terms is not None:
            raise ValueError("non-options must not carry option terms")
        if self.valid_to_utc is not None and self.valid_from_utc >= self.valid_to_utc:
            raise ValueError("instrument validity interval must be ordered")
        namespaces = [item.namespace for item in self.external_identifiers]
        if len(namespaces) != len(set(namespaces)):
            raise ValueError("external identifier namespaces must be unique")
        return self


class FxObservationV1(FrozenContractBase):
    fx_observation_id: str
    currency: str
    usd_per_currency: Decimal
    observed_at_utc: datetime
    effective_at_utc: datetime
    source_reference: str

    @field_validator("fx_observation_id", "source_reference")
    @classmethod
    def _identity_is_explicit(cls, value: str, info) -> str:
        return _require_nonblank(value, info.field_name)

    @field_validator("currency")
    @classmethod
    def _currency_is_explicit(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("usd_per_currency")
    @classmethod
    def _quote_is_positive(cls, value: Decimal) -> Decimal:
        _require_finite(value, "usd_per_currency")
        if value <= 0:
            raise ValueError("USD-per-currency quote must be positive")
        return value

    @field_validator("observed_at_utc", "effective_at_utc")
    @classmethod
    def _times_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class PositionV1(FrozenContractBase):
    position_id: str
    account_id: str
    instrument_id: str
    quantity: Decimal
    local_mark: Decimal
    local_market_value: Decimal
    base_market_value: Decimal
    mark_observed_at_utc: datetime
    mark_effective_at_utc: datetime
    sleeve_id: str
    strategy_group_id: str | None
    source_reference: str

    @field_validator(
        "position_id", "account_id", "instrument_id", "sleeve_id", "source_reference"
    )
    @classmethod
    def _identity_is_explicit(cls, value: str, info) -> str:
        return _require_nonblank(value, info.field_name)

    @field_validator("strategy_group_id")
    @classmethod
    def _optional_strategy_is_not_blank(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonblank(value, "strategy_group_id")

    @field_validator("quantity", "local_mark", "local_market_value", "base_market_value")
    @classmethod
    def _numbers_are_finite(cls, value: Decimal, info) -> Decimal:
        return _require_finite(value, info.field_name)

    @field_validator("quantity")
    @classmethod
    def _quantity_is_nonzero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("position quantity must be nonzero")
        return value

    @field_validator("local_mark")
    @classmethod
    def _mark_is_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("local mark must be positive")
        return value

    @field_validator("mark_observed_at_utc", "mark_effective_at_utc")
    @classmethod
    def _times_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class CashBalanceV1(FrozenContractBase):
    cash_balance_id: str
    account_id: str
    currency: str
    local_amount: Decimal
    base_amount: Decimal
    observed_at_utc: datetime
    effective_at_utc: datetime
    source_reference: str

    @field_validator("cash_balance_id", "account_id", "source_reference")
    @classmethod
    def _identity_is_explicit(cls, value: str, info) -> str:
        return _require_nonblank(value, info.field_name)

    @field_validator("currency")
    @classmethod
    def _currency_is_explicit(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("local_amount", "base_amount")
    @classmethod
    def _numbers_are_finite(cls, value: Decimal, info) -> Decimal:
        return _require_finite(value, info.field_name)

    @field_validator("observed_at_utc", "effective_at_utc")
    @classmethod
    def _times_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ReconciliationResultV1(FrozenContractBase):
    status: ReconciliationStatus
    source_nlv: Decimal
    normalized_nlv: Decimal
    normalized_minus_source_nlv: Decimal
    tolerance_base_currency: Decimal

    @field_validator(
        "source_nlv",
        "normalized_nlv",
        "normalized_minus_source_nlv",
        "tolerance_base_currency",
    )
    @classmethod
    def _numbers_are_finite(cls, value: Decimal, info) -> Decimal:
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def _declared_reconciliation_is_true(self) -> "ReconciliationResultV1":
        if self.source_nlv <= 0 or self.normalized_nlv <= 0:
            raise ValueError("source and normalized NLV must be positive")
        if self.tolerance_base_currency != ONE_CENT:
            raise ValueError("reconciliation tolerance must be exactly one base-currency cent")
        difference = self.normalized_nlv - self.source_nlv
        if self.normalized_minus_source_nlv != difference:
            raise ValueError("declared reconciliation difference is inconsistent")
        if abs(difference) > self.tolerance_base_currency:
            raise ValueError("book reconciliation exceeds one-cent tolerance")
        expected_status = (
            ReconciliationStatus.RECONCILED
            if difference == 0
            else ReconciliationStatus.WITHIN_TOLERANCE
        )
        if self.status is not expected_status:
            raise ValueError("declared reconciliation status is inconsistent")
        return self


class CanonicalBookV1(FrozenContractBase):
    schema_version: Literal["canonical_book_v1"]
    book_id: str
    generation: int = Field(ge=0)
    accounts: tuple[AccountV1, ...]
    valuation_cut: ValuationCutV1
    base_currency: str
    instruments: tuple[InstrumentV1, ...]
    fx_quotes: tuple[FxObservationV1, ...]
    positions: tuple[PositionV1, ...]
    cash_balances: tuple[CashBalanceV1, ...]
    source_nlv: Decimal
    normalized_nlv: Decimal
    reconciliation: ReconciliationResultV1
    input_references: tuple[str, ...]

    @field_validator("book_id")
    @classmethod
    def _book_identity_is_explicit(cls, value: str) -> str:
        return _require_nonblank(value, "book_id")

    @field_validator("base_currency")
    @classmethod
    def _base_currency_is_explicit(cls, value: str) -> str:
        return _require_currency(value)

    @field_validator("source_nlv", "normalized_nlv")
    @classmethod
    def _nlv_is_positive(cls, value: Decimal, info) -> Decimal:
        _require_finite(value, info.field_name)
        if value <= 0:
            raise ValueError(f"{info.field_name} must be positive")
        return value

    @field_validator("input_references")
    @classmethod
    def _input_references_are_immutable_and_explicit(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("input references must contain nonblank immutable references")
        if len(value) != len(set(value)):
            raise ValueError("input references must be unique")
        return value

    @model_validator(mode="after")
    def _book_is_one_reconciled_truth(self) -> "CanonicalBookV1":
        if not self.accounts or not self.instruments or not self.fx_quotes:
            raise ValueError("canonical book requires accounts, instruments, and FX quotes")

        def unique(items, attribute: str, label: str) -> None:
            values = [getattr(item, attribute) for item in items]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")

        unique(self.accounts, "account_id", "account ID")
        unique(self.instruments, "instrument_id", "instrument ID")
        unique(self.fx_quotes, "fx_observation_id", "FX observation ID")
        unique(self.fx_quotes, "currency", "FX currency")
        unique(self.positions, "position_id", "position ID")
        unique(self.cash_balances, "cash_balance_id", "cash balance ID")

        accounts = {item.account_id for item in self.accounts}
        instruments = {item.instrument_id: item for item in self.instruments}
        quotes = {item.currency: item.usd_per_currency for item in self.fx_quotes}
        if quotes.get("USD") != Decimal("1"):
            raise ValueError("q[USD] must be exactly one")

        required_currencies = {self.base_currency}
        required_currencies.update(item.trading_currency for item in self.instruments)
        required_currencies.update(item.settlement_currency for item in self.instruments)
        required_currencies.update(item.currency for item in self.cash_balances)
        missing = required_currencies - quotes.keys()
        if missing:
            raise ValueError(f"missing USD-per-currency conversion for {sorted(missing)}")

        target = self.valuation_cut.target_cut_utc

        def validate_observation(observed: datetime, effective: datetime, label: str) -> None:
            if effective > target:
                raise ValueError(f"{label} effective time follows target cut")
            if observed < effective:
                raise ValueError(f"{label} observation precedes effective time")
            if observed > target and not (
                self.valuation_cut.capture_start_utc
                <= observed
                <= self.valuation_cut.capture_end_utc
            ):
                raise ValueError(f"{label} post-cut observation is outside capture window")

        for quote in self.fx_quotes:
            validate_observation(
                quote.observed_at_utc, quote.effective_at_utc, quote.fx_observation_id
            )

        for instrument in self.instruments:
            if instrument.valid_from_utc > target or (
                instrument.valid_to_utc is not None and target >= instrument.valid_to_utc
            ):
                raise ValueError(f"instrument {instrument.instrument_id} is invalid at cut")
            if instrument.option_terms is not None:
                underlying = instruments.get(instrument.option_terms.underlying_instrument_id)
                if underlying is None or underlying.asset_class is AssetClass.OPTION:
                    raise ValueError("option underlying must reference a non-option instrument")
                if instrument.risk_entity_id != underlying.risk_entity_id:
                    raise ValueError("option and underlying risk-entity IDs must agree")
                if instrument.option_terms.expiry_date < target.date():
                    raise ValueError("held option is expired at target cut")

        for position in self.positions:
            if position.account_id not in accounts:
                raise ValueError("position references unknown account")
            instrument = instruments.get(position.instrument_id)
            if instrument is None:
                raise ValueError("position references unknown instrument")
            validate_observation(
                position.mark_observed_at_utc,
                position.mark_effective_at_utc,
                position.position_id,
            )
            multiplier = (
                instrument.option_terms.contract_multiplier
                if instrument.option_terms is not None
                else Decimal("1")
            )
            expected_local = position.quantity * position.local_mark * multiplier
            if position.local_market_value != expected_local:
                raise ValueError("position local market value equation failed")
            expected_base = convert_via_usd(
                expected_local,
                instrument.trading_currency,
                self.base_currency,
                quotes,
            )
            if position.base_market_value != expected_base:
                raise ValueError("position base market value equation failed")

        for balance in self.cash_balances:
            if balance.account_id not in accounts:
                raise ValueError("cash balance references unknown account")
            validate_observation(
                balance.observed_at_utc,
                balance.effective_at_utc,
                balance.cash_balance_id,
            )
            expected_base = convert_via_usd(
                balance.local_amount, balance.currency, self.base_currency, quotes
            )
            if balance.base_amount != expected_base:
                raise ValueError("cash base conversion equation failed")

        normalized = sum(
            (item.base_market_value for item in self.positions), start=Decimal("0")
        ) + sum((item.base_amount for item in self.cash_balances), start=Decimal("0"))
        if self.normalized_nlv != normalized:
            raise ValueError("normalized NLV does not equal positions plus cash")
        if (
            self.reconciliation.source_nlv != self.source_nlv
            or self.reconciliation.normalized_nlv != self.normalized_nlv
        ):
            raise ValueError("book and reconciliation NLV fields disagree")
        return self
