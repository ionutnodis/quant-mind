"""Verified filesystem-to-catalog publication boundary for analytical snapshots."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import Field, field_validator, model_validator

from quantmind.snapshots.contracts import (
    FrozenContractBase,
    RunOutcome,
    RunStage,
    canonical_json_bytes,
)
from quantmind.snapshots.input_artifacts import (
    ArtifactRefV1,
    InputArtifactBindingV1,
)
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestBodyV1,
    OutputArtifactBindingV1,
    create_manifest,
)
from quantmind.snapshots.run_repository import (
    ManifestPublicationV1,
    PublicationResultV1,
    RunRepository,
)
from quantmind.snapshots.store import SnapshotStore


_MAX_OUTPUTS: Final = 64
_MAX_INPUTS: Final = 256
_MAX_ARTIFACT_BYTES: Final = 64 * 1024 * 1024
_MAX_MANIFEST_BODY_BYTES: Final = 1024 * 1024


def _bounded_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be nonblank")
    if len(value.encode("utf-8")) > 256:
        raise ValueError(f"{field_name} exceeds its bounded length")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError(f"{field_name} must already be NFC normalized")
    return value


class OutputArtifactCandidateV1(FrozenContractBase):
    logical_role: str
    logical_id: str
    payload: bytes = Field(max_length=_MAX_ARTIFACT_BYTES)
    media_type: str
    schema_version: str
    model_version: str

    @field_validator(
        "logical_role",
        "logical_id",
        "media_type",
        "schema_version",
        "model_version",
    )
    @classmethod
    def _identifiers_are_bounded(cls, value: str, info) -> str:
        return _bounded_identifier(value, info.field_name)

    def artifact_ref(self) -> ArtifactRefV1:
        return ArtifactRefV1(
            hash_algorithm="sha256",
            digest=hashlib.sha256(self.payload).hexdigest(),
            byte_length=len(self.payload),
            media_type=self.media_type,
            schema_version=self.schema_version,
        )


class SnapshotCandidateV1(FrozenContractBase):
    manifest_body: AnalyticalSnapshotManifestBodyV1
    outputs: tuple[OutputArtifactCandidateV1, ...] = Field(
        min_length=1,
        max_length=_MAX_OUTPUTS,
    )

    @model_validator(mode="after")
    def _candidate_is_bounded_and_sorted(self) -> "SnapshotCandidateV1":
        body = AnalyticalSnapshotManifestBodyV1.model_validate(
            self.manifest_body.model_dump(mode="python", warnings=False)
        )
        if len(canonical_json_bytes(body)) > _MAX_MANIFEST_BODY_BYTES:
            raise ValueError("candidate manifest body exceeds its bounded length")
        keys = tuple((output.logical_role, output.logical_id) for output in self.outputs)
        if len(keys) != len(set(keys)):
            raise ValueError("candidate output role/ID pairs must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("candidate outputs must be sorted by logical role and ID")
        return self


class PublicationAuthorityV1(FrozenContractBase):
    canonical_book_ref: ArtifactRefV1
    input_artifacts: tuple[InputArtifactBindingV1, ...] = Field(
        max_length=_MAX_INPUTS
    )

    @model_validator(mode="after")
    def _inputs_are_unique_and_sorted(self) -> "PublicationAuthorityV1":
        canonical = ArtifactRefV1.model_validate(
            self.canonical_book_ref.model_dump(mode="python", warnings=False)
        )
        inputs = tuple(
            InputArtifactBindingV1.model_validate(
                binding.model_dump(mode="python", warnings=False)
            )
            for binding in self.input_artifacts
        )
        keys = tuple((binding.logical_role, binding.logical_id) for binding in inputs)
        if len(keys) != len(set(keys)):
            raise ValueError("authoritative input role/ID pairs must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("authoritative inputs must be sorted by logical role and ID")
        if canonical != self.canonical_book_ref or inputs != self.input_artifacts:
            raise ValueError("publication authority failed strict revalidation")
        return self


UtcClock = Callable[[], datetime]


class SnapshotPublisher:
    def __init__(
        self,
        *,
        repository: RunRepository,
        store: SnapshotStore,
        clock: UtcClock,
    ) -> None:
        if not isinstance(repository, RunRepository):
            raise TypeError("repository must be RunRepository")
        if not isinstance(store, SnapshotStore):
            raise TypeError("store must be SnapshotStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._repository = repository
        self._store = store
        self._clock = clock

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("publisher clock must return a datetime")
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("publisher clock must return explicit UTC")
        return now.astimezone(UTC)

    @staticmethod
    def _output_binding_by_key(
        body: AnalyticalSnapshotManifestBodyV1,
    ) -> dict[tuple[str, str], OutputArtifactBindingV1]:
        return {
            (binding.logical_role, binding.logical_id): binding
            for binding in body.outputs
        }

    def _validate_authority(
        self,
        *,
        run_id: str,
        candidate: SnapshotCandidateV1,
        authority: PublicationAuthorityV1,
    ):
        run = self._repository.get(run_id)
        if run.run_outcome is not RunOutcome.RUNNING:
            raise ValueError("only a running snapshot run may publish a new candidate")
        if run.run_stage is not RunStage.PUBLISHING:
            raise ValueError("snapshot publication requires the PUBLISHING stage")
        body = candidate.manifest_body
        if (
            run.book_id is None
            or run.captured_generation is None
            or body.book_id != run.book_id
            or body.book_generation != run.captured_generation
        ):
            raise ValueError("candidate book identity does not match its durable run")
        if body.canonical_book_ref != authority.canonical_book_ref:
            raise ValueError("candidate canonical book differs from controller authority")
        if body.input_artifacts != authority.input_artifacts:
            raise ValueError("candidate inputs differ from controller authority")

        head = self._repository.get_book_head(run.book_id)
        if head is None:
            raise ValueError("candidate book has no durable canonical head")
        if (
            head.generation == run.captured_generation
            and head.canonical_book_ref != authority.canonical_book_ref.digest
        ):
            raise ValueError("controller book authority differs from the durable head")

        bindings = self._output_binding_by_key(body)
        candidate_keys = tuple(
            (output.logical_role, output.logical_id) for output in candidate.outputs
        )
        if tuple(bindings) != candidate_keys:
            raise ValueError("candidate output set differs from the manifest body")
        for output in candidate.outputs:
            binding = bindings[(output.logical_role, output.logical_id)]
            if (
                binding.object_ref != output.artifact_ref()
                or binding.model_version != output.model_version
            ):
                raise ValueError("candidate output metadata differs from the manifest body")
        return run

    def publish(
        self,
        run_id: str,
        candidate: SnapshotCandidateV1,
        *,
        authority: PublicationAuthorityV1,
    ) -> PublicationResultV1:
        if not isinstance(candidate, SnapshotCandidateV1):
            raise TypeError("candidate must be SnapshotCandidateV1")
        if not isinstance(authority, PublicationAuthorityV1):
            raise TypeError("authority must be PublicationAuthorityV1")
        candidate = SnapshotCandidateV1.model_validate(
            candidate.model_dump(mode="python", warnings=False)
        )
        authority = PublicationAuthorityV1.model_validate(
            authority.model_dump(mode="python", warnings=False)
        )
        run = self._validate_authority(
            run_id=run_id,
            candidate=candidate,
            authority=authority,
        )
        bindings = self._output_binding_by_key(candidate.manifest_body)
        for output in candidate.outputs:
            reference = self._store.put_bytes(
                output.payload,
                media_type=output.media_type,
                schema_version=output.schema_version,
            )
            if reference != bindings[(output.logical_role, output.logical_id)].object_ref:
                raise ValueError("stored output reference differs from the manifest body")

        manifest = create_manifest(candidate.manifest_body)
        stored = self._store.put_verified_manifest(manifest)
        verified = self._store.verify_snapshot(stored.snapshot_id)
        if (
            verified.snapshot_id != stored.snapshot_id
            or verified.status is not stored.status
            or verified.manifest != stored.manifest
        ):
            raise ValueError("durably verified snapshot differs from stored metadata")

        attached = self._repository.attach_candidate(
            run_id,
            stored.snapshot_id,
            expected_version=run.version,
            now=self._now(),
        )
        publication = ManifestPublicationV1(
            snapshot_id=stored.snapshot_id,
            book_id=stored.manifest.body.book_id,
            book_generation=stored.manifest.body.book_generation,
            snapshot_status=stored.status,
            schema_version=stored.manifest.body.schema_version,
            hash_algorithm=stored.manifest.body.hash_algorithm,
            manifest_relpath=stored.manifest_relpath,
            envelope_sha256=stored.envelope_sha256,
            envelope_byte_length=stored.envelope_byte_length,
        )
        result = self._repository.commit_publication(
            run_id,
            publication,
            expected_version=attached.version,
            now=self._now(),
        )
        durable_run = self._repository.get(run_id)
        if durable_run != result.run:
            raise ValueError("publisher result differs from durable run truth")
        return result


__all__ = [
    "OutputArtifactCandidateV1",
    "PublicationAuthorityV1",
    "SnapshotCandidateV1",
    "SnapshotPublisher",
]
