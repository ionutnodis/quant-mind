"""Strict analytical snapshot identity and manifest parsing."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from quantmind.snapshots.contracts import (
    FrozenContractBase,
    GateEvidenceV1,
    GateStatus,
    SnapshotStatus,
    ValuationCutV1,
    canonical_json_bytes,
)
from quantmind.snapshots.input_artifacts import ArtifactRefV1, InputArtifactBindingV1


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_BOOK_REF_RE = re.compile(r"^[0-9a-f]{12}$")


class ManifestError(ValueError):
    """Base class for analytical manifest identity and parse failures."""


class ManifestIdentityError(ManifestError):
    pass


class DuplicateJSONKeyError(ManifestError):
    pass


class NonFiniteJSONConstantError(ManifestError):
    pass


class UnsupportedManifestSchemaError(ManifestError):
    pass


class NonCanonicalManifestError(ManifestError):
    pass


def _nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    return value


def _full_digest(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return value


def _unique_sorted_strings(
    values: tuple[str, ...], field_name: str
) -> tuple[str, ...]:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} values must be nonblank")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} values must be unique")
    if values != tuple(sorted(values)):
        raise ValueError(f"{field_name} values must be sorted")
    return values


class OutputArtifactBindingV1(FrozenContractBase):
    logical_role: str
    logical_id: str
    object_ref: ArtifactRefV1
    model_version: str

    @field_validator("logical_role", "logical_id", "model_version")
    @classmethod
    def _identifiers_are_explicit(cls, value: str, info) -> str:
        return _nonblank(value, info.field_name)


class AnalyticalSnapshotManifestBodyV1(FrozenContractBase):
    """The complete analytical identity preimage, with no publication metadata."""

    schema_version: Literal["analytical_snapshot_manifest_v1"]
    canonicalization_version: Literal["quantmind_canonical_json_v1"]
    hash_algorithm: Literal["sha256"]
    book_id: str
    book_generation: int = Field(ge=0)
    legacy_book_ref: str | None
    valuation_cut: ValuationCutV1
    base_currency: str
    normalized_nlv: Decimal
    included_account_ids: tuple[str, ...]
    canonical_book_ref: ArtifactRefV1
    canonical_book_hash: str
    position_hash: str
    input_artifacts: tuple[InputArtifactBindingV1, ...]
    security_master_mapping_version: str
    rights_manifest_versions: tuple[str, ...]
    factor_taxonomy_version: str
    return_series_version: str
    production_covariance_model_version: str
    residual_model_version: str
    latent_factor_model_version: str | None
    option_pricer_version: str | None
    surface_model_version: str | None
    scenario_library_version: str | None
    analytical_config_hash: str
    application_commit: str
    application_build_id: str
    snapshot_status: SnapshotStatus
    gates: tuple[GateEvidenceV1, ...]
    warnings: tuple[str, ...]
    refused_outputs: tuple[str, ...]
    outputs: tuple[OutputArtifactBindingV1, ...]

    @field_validator(
        "book_id",
        "security_master_mapping_version",
        "factor_taxonomy_version",
        "return_series_version",
        "production_covariance_model_version",
        "residual_model_version",
        "application_commit",
        "application_build_id",
    )
    @classmethod
    def _required_strings_are_explicit(cls, value: str, info) -> str:
        return _nonblank(value, info.field_name)

    @field_validator(
        "latent_factor_model_version",
        "option_pricer_version",
        "surface_model_version",
        "scenario_library_version",
    )
    @classmethod
    def _optional_strings_are_null_or_explicit(
        cls, value: str | None, info
    ) -> str | None:
        return None if value is None else _nonblank(value, info.field_name)

    @field_validator("base_currency")
    @classmethod
    def _base_currency_is_ascii_code(cls, value: str) -> str:
        if len(value) != 3 or any(character < "A" or character > "Z" for character in value):
            raise ValueError("base currency must be an uppercase three-letter code")
        return value

    @field_validator("normalized_nlv")
    @classmethod
    def _normalized_nlv_is_finite_positive(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("normalized NLV must be finite and positive")
        return value

    @field_validator("legacy_book_ref")
    @classmethod
    def _legacy_ref_is_exactly_legacy_shape(cls, value: str | None) -> str | None:
        if value is not None and not _LEGACY_BOOK_REF_RE.fullmatch(value):
            raise ValueError("legacy book ref must be 12 lowercase hexadecimal characters")
        return value

    @field_validator("canonical_book_hash", "position_hash", "analytical_config_hash")
    @classmethod
    def _identity_hashes_are_full(cls, value: str, info) -> str:
        return _full_digest(value, info.field_name)

    @field_validator("included_account_ids")
    @classmethod
    def _account_ids_are_nonempty_unique(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not values or any(not value.strip() for value in values):
            raise ValueError("included account IDs must be nonempty and nonblank")
        if len(values) != len(set(values)):
            raise ValueError("included account IDs must be unique")
        return values

    @field_validator("rights_manifest_versions", "warnings", "refused_outputs")
    @classmethod
    def _string_collections_are_stable(cls, values: tuple[str, ...], info) -> tuple[str, ...]:
        return _unique_sorted_strings(values, info.field_name)

    @model_validator(mode="after")
    def _manifest_is_publishable_and_stably_ordered(
        self,
    ) -> "AnalyticalSnapshotManifestBodyV1":
        if self.canonical_book_hash != self.canonical_book_ref.digest:
            raise ValueError("canonical book hash must match its immutable object reference")

        def binding_key(binding) -> tuple[str, str]:
            return binding.logical_role, binding.logical_id

        input_keys = tuple(binding_key(binding) for binding in self.input_artifacts)
        if len(input_keys) != len(set(input_keys)):
            raise ValueError("input artifact role/ID pairs must be unique")
        if input_keys != tuple(sorted(input_keys)):
            raise ValueError("input artifacts must be sorted by logical role and ID")
        binding_rights_versions = {
            binding.rights_manifest_version for binding in self.input_artifacts
        }
        undeclared_rights = binding_rights_versions - set(self.rights_manifest_versions)
        if undeclared_rights:
            raise ValueError(
                "input binding rights manifest version is absent from the manifest body"
            )

        output_keys = tuple(binding_key(binding) for binding in self.outputs)
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("output artifact role/ID pairs must be unique")
        if output_keys != tuple(sorted(output_keys)):
            raise ValueError("outputs must be sorted by logical role and ID")

        gate_codes = tuple(gate.gate_code for gate in self.gates)
        if len(gate_codes) != len(set(gate_codes)):
            raise ValueError("gate codes must be unique")
        if gate_codes != tuple(sorted(gate_codes)):
            raise ValueError("gates must be sorted by gate code")
        if any(gate.status is GateStatus.FAILED for gate in self.gates):
            raise ValueError("a published manifest cannot contain failed gate evidence")

        if self.snapshot_status is SnapshotStatus.BLESSED and self.refused_outputs:
            raise ValueError("a blessed manifest cannot contain refused outputs")
        if self.snapshot_status is SnapshotStatus.DEGRADED and not self.refused_outputs:
            raise ValueError("a degraded manifest must name at least one refused output")
        return self


class AnalyticalSnapshotManifestV1(FrozenContractBase):
    snapshot_id: str
    body: AnalyticalSnapshotManifestBodyV1

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id_is_full(cls, value: str) -> str:
        return _full_digest(value, "snapshot ID")

    @model_validator(mode="after")
    def _snapshot_id_matches_body(self) -> "AnalyticalSnapshotManifestV1":
        expected = hashlib.sha256(canonical_json_bytes(self.body)).hexdigest()
        if self.snapshot_id != expected:
            raise ValueError("snapshot ID does not match canonical manifest body")
        return self


def create_manifest(
    body: AnalyticalSnapshotManifestBodyV1,
) -> AnalyticalSnapshotManifestV1:
    if not isinstance(body, AnalyticalSnapshotManifestBodyV1):
        raise TypeError("manifest body must be AnalyticalSnapshotManifestBodyV1")
    snapshot_id = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return AnalyticalSnapshotManifestV1(snapshot_id=snapshot_id, body=body)


def verify_manifest(manifest: AnalyticalSnapshotManifestV1) -> None:
    if not isinstance(manifest, AnalyticalSnapshotManifestV1):
        raise TypeError("manifest must be AnalyticalSnapshotManifestV1")
    expected = hashlib.sha256(canonical_json_bytes(manifest.body)).hexdigest()
    if manifest.snapshot_id != expected:
        raise ManifestIdentityError("snapshot ID does not match canonical manifest body")


def snapshot_display_prefix(snapshot_id: str, length: int = 12) -> str:
    _full_digest(snapshot_id, "snapshot ID")
    if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 64:
        raise ValueError("display prefix length must be an integer from 1 through 64")
    return snapshot_id[:length]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise NonFiniteJSONConstantError(f"non-finite JSON constant: {value}")


def _load_unambiguous_json(payload: bytes) -> Any:
    if not isinstance(payload, bytes):
        raise TypeError("manifest payload must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestError("manifest is not valid UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (DuplicateJSONKeyError, NonFiniteJSONConstantError):
        raise
    except json.JSONDecodeError as error:
        raise ManifestError("manifest is not valid JSON") from error


def parse_manifest(payload: bytes) -> AnalyticalSnapshotManifestV1:
    parsed = _load_unambiguous_json(payload)
    body = parsed.get("body") if isinstance(parsed, dict) else None
    schema = body.get("schema_version") if isinstance(body, dict) else None
    if schema is None:
        raise UnsupportedManifestSchemaError("manifest schema is missing")
    if schema != "analytical_snapshot_manifest_v1":
        raise UnsupportedManifestSchemaError(f"unsupported manifest schema: {schema}")

    try:
        manifest = AnalyticalSnapshotManifestV1.model_validate_json(payload)
    except ValueError as error:
        raise ManifestError("manifest violates the analytical v1 schema") from error
    if canonical_json_bytes(manifest) != payload:
        raise NonCanonicalManifestError("manifest envelope bytes are not canonical")
    verify_manifest(manifest)
    return manifest


__all__ = [
    "AnalyticalSnapshotManifestBodyV1",
    "AnalyticalSnapshotManifestV1",
    "ArtifactRefV1",
    "DuplicateJSONKeyError",
    "ManifestError",
    "ManifestIdentityError",
    "NonCanonicalManifestError",
    "NonFiniteJSONConstantError",
    "OutputArtifactBindingV1",
    "UnsupportedManifestSchemaError",
    "create_manifest",
    "parse_manifest",
    "snapshot_display_prefix",
    "verify_manifest",
]
