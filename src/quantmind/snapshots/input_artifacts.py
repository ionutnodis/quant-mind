"""Typed, rights-aware bindings for immutable analytical input artifacts."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from quantmind.snapshots.contracts import FrozenContractBase, canonical_json_bytes


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    return value


class ArtifactRightsMode(str, Enum):
    RAW_ALLOWED = "RAW_ALLOWED"
    PROVENANCE_ONLY = "PROVENANCE_ONLY"


class InputRepresentation(str, Enum):
    RAW_RETAINED = "RAW_RETAINED"
    PROVENANCE_ENVELOPE = "PROVENANCE_ENVELOPE"
    NORMALIZED_INPUT = "NORMALIZED_INPUT"


class ReproducibilityClass(str, Enum):
    COMPLETE = "COMPLETE"
    NORMALIZED_ONLY = "NORMALIZED_ONLY"
    PROVENANCE_ONLY = "PROVENANCE_ONLY"
    NON_REPRODUCIBLE_LEGACY = "NON_REPRODUCIBLE_LEGACY"


class ArtifactRefV1(FrozenContractBase):
    """Full immutable object reference; display prefixes are never references."""

    hash_algorithm: Literal["sha256"]
    digest: str
    byte_length: int = Field(ge=0)
    media_type: str
    schema_version: str

    @field_validator("digest")
    @classmethod
    def _digest_is_full_lowercase_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("artifact digest must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("media_type", "schema_version")
    @classmethod
    def _descriptors_are_explicit(cls, value: str, info) -> str:
        return _nonblank(value, info.field_name)


class InputArtifactBindingV1(FrozenContractBase):
    logical_role: str
    logical_id: str
    representation: InputRepresentation
    object_ref: ArtifactRefV1
    source: str
    provider: str
    rights_mode: ArtifactRightsMode
    rights_manifest_version: str
    reproducibility_class: ReproducibilityClass

    @field_validator(
        "logical_role",
        "logical_id",
        "source",
        "provider",
        "rights_manifest_version",
    )
    @classmethod
    def _identifiers_are_explicit(cls, value: str, info) -> str:
        return _nonblank(value, info.field_name)

    @model_validator(mode="after")
    def _rights_and_representation_are_honest(self) -> "InputArtifactBindingV1":
        if (
            self.representation is InputRepresentation.RAW_RETAINED
            and self.rights_mode is not ArtifactRightsMode.RAW_ALLOWED
        ):
            raise ValueError("raw retained input requires RAW_ALLOWED rights mode")
        if (
            self.representation is InputRepresentation.PROVENANCE_ENVELOPE
            and self.rights_mode is not ArtifactRightsMode.PROVENANCE_ONLY
        ):
            raise ValueError(
                "provenance envelope representation requires PROVENANCE_ONLY rights mode"
            )
        if (
            self.representation is InputRepresentation.PROVENANCE_ENVELOPE
            and self.reproducibility_class is not ReproducibilityClass.PROVENANCE_ONLY
        ):
            raise ValueError(
                "provenance envelope must be labelled PROVENANCE_ONLY reproducibility"
            )
        if self.representation is InputRepresentation.RAW_RETAINED and (
            self.reproducibility_class
            in {
                ReproducibilityClass.PROVENANCE_ONLY,
                ReproducibilityClass.NON_REPRODUCIBLE_LEGACY,
            }
        ):
            raise ValueError("raw retained input has an inconsistent reproducibility class")
        return self


def canonical_input_bytes(contract: FrozenContractBase) -> bytes:
    """Return T1 canonical bytes; this module deliberately has no serializer of its own."""

    if not isinstance(contract, FrozenContractBase):
        raise TypeError("canonical input must be a frozen analytical contract")
    return canonical_json_bytes(contract)


def retained_raw_input_bytes(
    payload: bytes, *, rights_mode: ArtifactRightsMode
) -> bytes:
    """Pass through raw bytes only after an explicit retention-rights decision."""

    if not isinstance(payload, bytes):
        raise TypeError("raw input payload must be bytes")
    if rights_mode is ArtifactRightsMode.PROVENANCE_ONLY:
        raise ValueError("PROVENANCE_ONLY rights forbid retaining raw vendor bytes")
    return payload


def bind_input_artifact(
    *,
    logical_role: str,
    logical_id: str,
    representation: InputRepresentation,
    object_ref: ArtifactRefV1,
    source: str,
    provider: str,
    rights_mode: ArtifactRightsMode,
    rights_manifest_version: str,
    reproducibility_class: ReproducibilityClass,
    **extra: Any,
) -> InputArtifactBindingV1:
    """Construct a strict immutable binding after the adapter made its rights decision."""

    return InputArtifactBindingV1(
        logical_role=logical_role,
        logical_id=logical_id,
        representation=representation,
        object_ref=object_ref,
        source=source,
        provider=provider,
        rights_mode=rights_mode,
        rights_manifest_version=rights_manifest_version,
        reproducibility_class=reproducibility_class,
        **extra,
    )

