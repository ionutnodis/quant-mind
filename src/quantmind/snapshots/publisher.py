"""Verified filesystem-to-catalog publication boundary for analytical snapshots."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Final

from pydantic import Field, field_validator, model_validator

from quantmind.snapshots.contracts import (
    FrozenContractBase,
    GateEvidenceV1,
    RunOutcome,
    RunStage,
    SnapshotStatus,
    ValuationCutV1,
    canonical_json_bytes,
)
from quantmind.snapshots.input_artifacts import (
    ArtifactRefV1,
    InputArtifactBindingV1,
)
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestV1,
    AnalyticalSnapshotManifestBodyV1,
    ManifestError,
    ManifestPolicyEvidenceV1,
    OutputArtifactBindingV1,
    create_manifest,
    parse_manifest,
)
from quantmind.snapshots.run_repository import (
    BookHeadV1,
    IllegalRunTransitionError,
    ManifestPublicationRecordV1,
    ManifestPublicationV1,
    PublicationResultV1,
    RunDatabaseError,
    RunErrorCode,
    RunFailureV1,
    RunRecordV1,
    RunRepository,
    StaleRunVersionError,
    TerminalRunMutationError,
)
from quantmind.snapshots.store import (
    SnapshotStore,
    SnapshotStoreError,
    SnapshotVerificationError,
    StoredManifestV1,
    VerifiedSnapshotV1,
)


_MAX_OUTPUTS: Final = 64
_MAX_INPUTS: Final = 256
_MAX_ARTIFACT_BYTES: Final = 64 * 1024 * 1024
_MAX_AGGREGATE_OUTPUT_BYTES: Final = 64 * 1024 * 1024
_MAX_MANIFEST_BODY_BYTES: Final = 1024 * 1024
_MAX_AUTHORITY_BYTES: Final = 1024 * 1024
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_TERMINAL_REJECTIONS: Final = frozenset(
    {
        RunErrorCode.CANCELLED_BY_USER,
        RunErrorCode.STALE_BOOK_GENERATION,
        RunErrorCode.STALE_ACTIVE_POINTER,
    }
)


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


def _full_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")
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
        outputs = tuple(
            OutputArtifactCandidateV1.model_validate(
                output.model_dump(mode="python", warnings=False)
            )
            for output in self.outputs
        )
        if len(canonical_json_bytes(body)) > _MAX_MANIFEST_BODY_BYTES:
            raise ValueError("candidate manifest body exceeds its bounded length")
        if sum(len(output.payload) for output in outputs) > _MAX_AGGREGATE_OUTPUT_BYTES:
            raise ValueError("candidate aggregate output payload exceeds its bound")
        keys = tuple((output.logical_role, output.logical_id) for output in outputs)
        if len(keys) != len(set(keys)):
            raise ValueError("candidate output role/ID pairs must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("candidate outputs must be sorted by logical role and ID")
        if body != self.manifest_body or outputs != self.outputs:
            raise ValueError("candidate failed strict nested revalidation")
        return self


class PublicationAuthorityV1(FrozenContractBase):
    request_fingerprint: str
    analytical_config_hash: str
    canonical_book_ref: ArtifactRefV1
    input_artifacts: tuple[InputArtifactBindingV1, ...] = Field(
        max_length=_MAX_INPUTS
    )
    valuation_cut: ValuationCutV1
    snapshot_status: SnapshotStatus
    gates: tuple[GateEvidenceV1, ...] = Field(max_length=64)
    policy_evidence: tuple[ManifestPolicyEvidenceV1, ...] = Field(max_length=128)
    warnings: tuple[str, ...] = Field(max_length=128)
    refused_outputs: tuple[str, ...] = Field(max_length=64)

    @field_validator("request_fingerprint", "analytical_config_hash")
    @classmethod
    def _identities_are_full_digests(cls, value: str, info) -> str:
        return _full_digest(value, info.field_name)

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
        valuation_cut = ValuationCutV1.model_validate(
            self.valuation_cut.model_dump(mode="python", warnings=False)
        )
        gates = tuple(
            GateEvidenceV1.model_validate(
                gate.model_dump(mode="python", warnings=False)
            )
            for gate in self.gates
        )
        policy_evidence = tuple(
            ManifestPolicyEvidenceV1.model_validate(
                evidence.model_dump(mode="python", warnings=False)
            )
            for evidence in self.policy_evidence
        )
        keys = tuple((binding.logical_role, binding.logical_id) for binding in inputs)
        if len(keys) != len(set(keys)):
            raise ValueError("authoritative input role/ID pairs must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("authoritative inputs must be sorted by logical role and ID")
        gate_codes = tuple(gate.gate_code for gate in gates)
        if len(gate_codes) != len(set(gate_codes)) or gate_codes != tuple(
            sorted(gate_codes)
        ):
            raise ValueError("authoritative gates must be unique and sorted")
        policy_keys = tuple(
            (evidence.subject_kind, evidence.subject_id, evidence.gate_code)
            for evidence in policy_evidence
        )
        if len(policy_keys) != len(set(policy_keys)) or policy_keys != tuple(
            sorted(policy_keys)
        ):
            raise ValueError("authoritative policy evidence must be unique and sorted")
        for field_name, values in (
            ("warnings", self.warnings),
            ("refused outputs", self.refused_outputs),
        ):
            if len(values) != len(set(values)) or values != tuple(sorted(values)):
                raise ValueError(f"authoritative {field_name} must be unique and sorted")
            for value in values:
                _bounded_identifier(value, field_name)
        if (
            canonical != self.canonical_book_ref
            or inputs != self.input_artifacts
            or valuation_cut != self.valuation_cut
            or gates != self.gates
            or policy_evidence != self.policy_evidence
        ):
            raise ValueError("publication authority failed strict revalidation")
        authority_payload = {
            "request_fingerprint": self.request_fingerprint,
            "analytical_config_hash": self.analytical_config_hash,
            "canonical_book_ref": canonical,
            "input_artifacts": inputs,
            "valuation_cut": valuation_cut,
            "snapshot_status": self.snapshot_status,
            "gates": gates,
            "policy_evidence": policy_evidence,
            "warnings": self.warnings,
            "refused_outputs": self.refused_outputs,
        }
        if len(canonical_json_bytes(authority_payload)) > _MAX_AUTHORITY_BYTES:
            raise ValueError("publication authority exceeds its bounded canonical bytes")
        return self


UtcClock = Callable[[], datetime]


class PublisherFaultStage(str, Enum):
    AFTER_MANIFEST_DURABLE = "after_manifest_durable"
    AFTER_SNAPSHOT_VERIFIED = "after_snapshot_verified"
    BEFORE_CANDIDATE_ATTACH = "before_candidate_attach"
    AFTER_CANDIDATE_ATTACH = "after_candidate_attach"
    BEFORE_REPOSITORY_COMMIT = "before_repository_commit"
    AFTER_REPOSITORY_COMMIT = "after_repository_commit"


class PublisherResultCode(str, Enum):
    CATALOG_RESULT = "CATALOG_RESULT"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


class SnapshotPublisherResultV1(FrozenContractBase):
    result_code: PublisherResultCode
    run: RunRecordV1
    publication_result: PublicationResultV1 | None

    @model_validator(mode="after")
    def _payload_matches_result_code(self) -> "SnapshotPublisherResultV1":
        run = RunRecordV1.model_validate(
            self.run.model_dump(mode="python", warnings=False)
        )
        publication_result = (
            None
            if self.publication_result is None
            else PublicationResultV1.model_validate(
                self.publication_result.model_dump(mode="python", warnings=False)
            )
        )
        if run != self.run or publication_result != self.publication_result:
            raise ValueError("publisher result failed strict nested revalidation")
        if self.result_code is PublisherResultCode.CATALOG_RESULT:
            if publication_result is None or publication_result.run != run:
                raise ValueError(
                    "catalog publisher results require matching publication evidence"
                )
            if publication_result.published != (
                publication_result.publication is not None
            ):
                raise ValueError(
                    "catalog published flag must match publication evidence"
                )
            if publication_result.already_published and not publication_result.published:
                raise ValueError(
                    "catalog idempotent result requires an existing publication"
                )
            active = publication_result.active
            if active is not None and (
                run.book_id is None or active.book_id != run.book_id
            ):
                raise ValueError(
                    "catalog active evidence must belong to the durable run book"
                )
            if publication_result.published != (
                run.run_outcome is RunOutcome.SUCCEEDED
            ):
                raise ValueError(
                    "catalog publication evidence must match the durable run outcome"
                )
            if publication_result.rejection_code is None:
                if run.run_outcome is not RunOutcome.SUCCEEDED:
                    raise ValueError(
                        "catalog terminal rejection requires an exact rejection code"
                    )
                publication = publication_result.publication
                if publication is None or (
                    run.book_id is None
                    or run.captured_generation is None
                    or run.candidate_snapshot_id is None
                    or run.published_snapshot_id is None
                    or run.finished_at_utc is None
                    or publication.run_id != run.run_id
                    or publication.book_id != run.book_id
                    or publication.book_generation != run.captured_generation
                    or publication.snapshot_id != run.candidate_snapshot_id
                    or publication.snapshot_id != run.published_snapshot_id
                    or publication.published_at_utc != run.finished_at_utc
                    or publication.published_at_utc != run.updated_at_utc
                ):
                    raise ValueError(
                        "successful publication evidence differs from durable run provenance"
                    )
                if active is not None:
                    publication_pointer_version = (
                        run.expected_active_pointer_version + 1
                    )
                    if (
                        active.pointer_version < publication_pointer_version
                        or active.updated_at_utc < publication.published_at_utc
                        or (
                            active.snapshot_id == publication.snapshot_id
                            and active.book_generation
                            != publication.book_generation
                        )
                        or (
                            active.snapshot_id != publication.snapshot_id
                            and active.pointer_version
                            <= publication_pointer_version
                        )
                    ):
                        raise ValueError(
                            "active snapshot lacks valid publication provenance"
                        )
            elif (
                publication_result.published
                or publication_result.publication is not None
                or publication_result.already_published
                or run.error_code is not publication_result.rejection_code
            ):
                raise ValueError(
                    "catalog rejection code must match unpublished terminal evidence"
                )
        elif publication_result is not None or run.run_outcome not in {
            RunOutcome.FAILED,
            RunOutcome.CANCELLED,
        }:
            raise ValueError(
                "terminal publisher failures require failed or cancelled run evidence"
            )
        return self

    @property
    def publication(self):
        return (
            None
            if self.publication_result is None
            else self.publication_result.publication
        )

    @property
    def active(self):
        return (
            None if self.publication_result is None else self.publication_result.active
        )

    @property
    def published(self) -> bool:
        return (
            False
            if self.publication_result is None
            else self.publication_result.published
        )

    @property
    def already_published(self) -> bool:
        return (
            False
            if self.publication_result is None
            else self.publication_result.already_published
        )

    @property
    def rejection_code(self) -> RunErrorCode | None:
        return (
            self.run.error_code
            if self.publication_result is None
            else self.publication_result.rejection_code
        )


PublisherFaultInjector = Callable[[PublisherFaultStage], None]


class _PublisherSerializationError(ValueError):
    """A candidate/result contract failure safe to record as serialization evidence."""


class SnapshotPublisher:
    def __init__(
        self,
        *,
        repository: RunRepository,
        store: SnapshotStore,
        clock: UtcClock,
        fault_injector: PublisherFaultInjector | None = None,
    ) -> None:
        if not isinstance(repository, RunRepository):
            raise TypeError("repository must be RunRepository")
        if not isinstance(store, SnapshotStore):
            raise TypeError("store must be SnapshotStore")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._repository = repository
        self._catalog_repository = RunRepository(
            repository.root,
            fault_injector=RunRepository.fault_injector.__get__(
                repository,
                RunRepository,
            ),
        )
        self._store = store
        self._clock = clock
        self._fault_injector = fault_injector

    def _inject(self, stage: PublisherFaultStage) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("publisher clock must return a datetime")
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("publisher clock must return explicit UTC")
        return now.astimezone(UTC)

    @staticmethod
    def _canonical_manifest(
        body: AnalyticalSnapshotManifestBodyV1,
    ) -> AnalyticalSnapshotManifestV1:
        """Reparse the exact canonical envelope used by the durable store."""

        return parse_manifest(canonical_json_bytes(create_manifest(body)))

    @staticmethod
    def _publication_projection(
        publication_record: ManifestPublicationRecordV1,
    ) -> ManifestPublicationV1:
        return ManifestPublicationV1(
            snapshot_id=publication_record.snapshot_id,
            book_id=publication_record.book_id,
            book_generation=publication_record.book_generation,
            snapshot_status=publication_record.snapshot_status,
            schema_version=publication_record.schema_version,
            hash_algorithm=publication_record.hash_algorithm,
            manifest_relpath=publication_record.manifest_relpath,
            envelope_sha256=publication_record.envelope_sha256,
            envelope_byte_length=publication_record.envelope_byte_length,
        )

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
        run: RunRecordV1,
        head: BookHeadV1 | None,
        candidate: SnapshotCandidateV1,
        authority: PublicationAuthorityV1,
    ) -> None:
        body = candidate.manifest_body
        if (
            run.book_id is None
            or run.captured_generation is None
            or body.book_id != run.book_id
            or body.book_generation != run.captured_generation
        ):
            raise _PublisherSerializationError(
                "candidate book identity does not match its durable run"
            )
        if body.canonical_book_ref != authority.canonical_book_ref:
            raise _PublisherSerializationError(
                "candidate canonical book differs from controller authority"
            )
        if body.input_artifacts != authority.input_artifacts:
            raise _PublisherSerializationError(
                "candidate inputs differ from controller authority"
            )
        if run.request_fingerprint != authority.request_fingerprint:
            raise _PublisherSerializationError(
                "controller request fingerprint differs from the durable run"
            )
        if body.analytical_config_hash != authority.analytical_config_hash:
            raise _PublisherSerializationError(
                "candidate config identity differs from controller authority"
            )
        if run.target_cut_utc != authority.valuation_cut.target_cut_utc:
            raise _PublisherSerializationError(
                "controller target cut differs from the durable run request"
            )
        if body.valuation_cut != authority.valuation_cut:
            raise _PublisherSerializationError(
                "candidate valuation cut differs from controller authority"
            )
        if (
            body.snapshot_status is not authority.snapshot_status
            or body.gates != authority.gates
            or body.policy_evidence != authority.policy_evidence
            or body.warnings != authority.warnings
            or body.refused_outputs != authority.refused_outputs
        ):
            raise _PublisherSerializationError(
                "candidate validation decision differs from controller authority"
            )

        if head is None:
            raise IllegalRunTransitionError(
                "candidate book has no durable canonical head"
            )
        if (
            head.generation == run.captured_generation
            and head.canonical_book_ref != authority.canonical_book_ref.digest
        ):
            raise _PublisherSerializationError(
                "controller book authority differs from the durable head"
            )

        bindings = self._output_binding_by_key(body)
        candidate_keys = tuple(
            (output.logical_role, output.logical_id) for output in candidate.outputs
        )
        if tuple(bindings) != candidate_keys:
            raise _PublisherSerializationError(
                "candidate output set differs from the manifest body"
            )
        for output in candidate.outputs:
            binding = bindings[(output.logical_role, output.logical_id)]
            if (
                binding.object_ref != output.artifact_ref()
                or binding.model_version != output.model_version
            ):
                raise _PublisherSerializationError(
                    "candidate output metadata differs from the manifest body"
                )

    @staticmethod
    def _revalidate_publication_result(
        result: PublicationResultV1,
    ) -> PublicationResultV1:
        if not isinstance(result, PublicationResultV1):
            raise TypeError("repository must return PublicationResultV1")
        return PublicationResultV1.model_validate(
            result.model_dump(mode="python", warnings=False)
        )

    @staticmethod
    def _catalog_result(result: PublicationResultV1) -> SnapshotPublisherResultV1:
        result = SnapshotPublisher._revalidate_publication_result(result)
        return SnapshotPublisherResultV1(
            result_code=PublisherResultCode.CATALOG_RESULT,
            run=result.run,
            publication_result=result,
        )

    def _resolve_publication_result(
        self,
        run_id: str,
        *,
        already_published: bool,
    ) -> PublicationResultV1:
        return self._revalidate_publication_result(
            RunRepository.resolve_publication_result(
                self._catalog_repository,
                run_id,
                already_published=already_published,
            )
        )

    def _terminalize_failure(
        self,
        run_id: str,
        code: RunErrorCode,
    ) -> SnapshotPublisherResultV1:
        current = self._repository.get(run_id)
        while True:
            if current.run_outcome in {RunOutcome.FAILED, RunOutcome.CANCELLED}:
                return self._terminal_result(current)
            if current.run_outcome is RunOutcome.SUCCEEDED:
                raise TerminalRunMutationError(
                    "run succeeded while publisher failure evidence was being recorded"
                )
            terminal_time = max(
                value
                for value in (
                    self._now(),
                    current.updated_at_utc,
                    current.cancel_requested_at_utc,
                )
                if value is not None
            )
            try:
                if current.cancel_requested_at_utc is not None:
                    terminal = self._repository.acknowledge_cancel(
                        run_id,
                        expected_version=current.version,
                        now=terminal_time,
                    )
                else:
                    terminal = self._repository.mark_failed(
                        run_id,
                        RunFailureV1(code=code),
                        expected_version=current.version,
                        now=terminal_time,
                    )
            except StaleRunVersionError as error:
                durable = self._repository.get(run_id)
                if durable.run_outcome in {
                    RunOutcome.FAILED,
                    RunOutcome.CANCELLED,
                }:
                    return self._terminal_result(durable)
                if durable.run_outcome is RunOutcome.SUCCEEDED:
                    raise TerminalRunMutationError(
                        "run succeeded while publisher failure evidence was being recorded"
                    ) from error
                old_state = current.model_dump(
                    mode="python",
                    warnings=False,
                    exclude={
                        "candidate_snapshot_id",
                        "cancel_requested_at_utc",
                        "updated_at_utc",
                        "version",
                    },
                )
                new_state = durable.model_dump(
                    mode="python",
                    warnings=False,
                    exclude={
                        "candidate_snapshot_id",
                        "cancel_requested_at_utc",
                        "updated_at_utc",
                        "version",
                    },
                )
                candidate_advanced = (
                    current.candidate_snapshot_id is None
                    and durable.candidate_snapshot_id is not None
                )
                cancellation_advanced = (
                    current.cancel_requested_at_utc is None
                    and durable.cancel_requested_at_utc is not None
                )
                expected_version = current.version + int(candidate_advanced) + int(
                    cancellation_advanced
                )
                if (
                    old_state != new_state
                    or durable.updated_at_utc < current.updated_at_utc
                    or durable.version != expected_version
                    or (
                        current.candidate_snapshot_id is not None
                        and durable.candidate_snapshot_id
                        != current.candidate_snapshot_id
                    )
                    or (
                        current.cancel_requested_at_utc is not None
                        and durable.cancel_requested_at_utc
                        != current.cancel_requested_at_utc
                    )
                    or not (candidate_advanced or cancellation_advanced)
                ):
                    raise error
                current = durable
                continue
            except TerminalRunMutationError:
                durable = self._repository.get(run_id)
                if durable.run_outcome in {
                    RunOutcome.FAILED,
                    RunOutcome.CANCELLED,
                }:
                    return self._terminal_result(durable)
                raise
            return self._terminal_result(terminal)

    @staticmethod
    def _terminal_result(run: RunRecordV1) -> SnapshotPublisherResultV1:
        return SnapshotPublisherResultV1(
            result_code=PublisherResultCode.TERMINAL_FAILURE,
            run=run,
            publication_result=None,
        )

    def _completed_run_publication(
        self,
        *,
        run: RunRecordV1,
        candidate: SnapshotCandidateV1,
    ) -> ManifestPublicationV1:
        expected_manifest = self._canonical_manifest(candidate.manifest_body)
        if (
            run.candidate_snapshot_id != expected_manifest.snapshot_id
            or run.published_snapshot_id != expected_manifest.snapshot_id
        ):
            raise TerminalRunMutationError(
                "completed run is bound to a different snapshot candidate"
            )
        stored = self._inspect_existing_candidate(expected_manifest)
        return ManifestPublicationV1(
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

    def _inspect_existing_candidate(self, expected_manifest) -> StoredManifestV1:
        stored = self._store.inspect_verified_manifest(expected_manifest.snapshot_id)
        if not isinstance(stored, StoredManifestV1):
            raise TypeError("store must return StoredManifestV1")
        stored = StoredManifestV1.model_validate(
            stored.model_dump(mode="python", warnings=False)
        )
        if (
            stored.snapshot_id != expected_manifest.snapshot_id
            or stored.status is not expected_manifest.body.snapshot_status
            or stored.manifest != expected_manifest
        ):
            raise SnapshotVerificationError(
                "completed run snapshot differs from its retried candidate"
            )
        self._inject(PublisherFaultStage.AFTER_SNAPSHOT_VERIFIED)
        return stored

    def _terminal_rejection_retry(
        self,
        *,
        run: RunRecordV1,
        candidate: SnapshotCandidateV1,
    ) -> SnapshotPublisherResultV1:
        expected_manifest = self._canonical_manifest(candidate.manifest_body)
        if (
            run.error_code not in _RETRYABLE_TERMINAL_REJECTIONS
            or run.candidate_snapshot_id != expected_manifest.snapshot_id
            or run.published_snapshot_id is not None
        ):
            raise TerminalRunMutationError(
                "terminal run is not an exact publication-rejection retry"
            )
        self._inspect_existing_candidate(expected_manifest)
        return self._catalog_result(
            self._resolve_publication_result(
                run.run_id,
                already_published=False,
            )
        )

    def _converge_attachment_race(
        self,
        *,
        run_id: str,
        snapshot_id: str,
        candidate: SnapshotCandidateV1,
        error: StaleRunVersionError | TerminalRunMutationError,
        allow_running: bool,
    ) -> RunRecordV1 | SnapshotPublisherResultV1:
        durable = self._repository.get(run_id)
        if durable.run_outcome in {RunOutcome.FAILED, RunOutcome.CANCELLED}:
            return self._terminal_result(durable)
        if (
            durable.run_outcome is RunOutcome.SUCCEEDED
            and durable.candidate_snapshot_id == snapshot_id
            and durable.published_snapshot_id == snapshot_id
        ):
            publication = self._completed_run_publication(
                run=durable,
                candidate=candidate,
            )
            return self._commit_verified_publication(
                run_id=run_id,
                publication=publication,
                expected_version=durable.version,
            )
        if (
            allow_running
            and durable.run_outcome is RunOutcome.RUNNING
            and (
                durable.cancel_requested_at_utc is not None
                or durable.candidate_snapshot_id == snapshot_id
            )
        ):
            return durable
        raise error

    def _commit_verified_publication(
        self,
        *,
        run_id: str,
        publication: ManifestPublicationV1,
        expected_version: int,
    ) -> SnapshotPublisherResultV1:
        before_commit = RunRepository.get(self._catalog_repository, run_id)
        commit_time = max(self._now(), before_commit.updated_at_utc)
        self._inject(PublisherFaultStage.BEFORE_REPOSITORY_COMMIT)
        try:
            committed = self._revalidate_publication_result(
                RunRepository.commit_publication(
                    self._catalog_repository,
                    run_id,
                    publication,
                    expected_version=expected_version,
                    now=commit_time,
                )
            )
        except TerminalRunMutationError:
            durable = RunRepository.get(self._catalog_repository, run_id)
            if durable.run_outcome in {
                RunOutcome.FAILED,
                RunOutcome.CANCELLED,
            }:
                return self._terminal_result(durable)
            raise
        self._inject(PublisherFaultStage.AFTER_REPOSITORY_COMMIT)
        if committed.publication is not None and (
            self._publication_projection(committed.publication) != publication
        ):
            raise RunDatabaseError(
                "durable publication differs from the locally verified manifest"
            )
        return self._catalog_result(committed)

    def publish(
        self,
        run_id: str,
        candidate: SnapshotCandidateV1,
        *,
        authority: PublicationAuthorityV1,
    ) -> SnapshotPublisherResultV1:
        if not isinstance(candidate, SnapshotCandidateV1):
            raise TypeError("candidate must be SnapshotCandidateV1")
        if not isinstance(authority, PublicationAuthorityV1):
            raise TypeError("authority must be PublicationAuthorityV1")

        initial = self._repository.get(run_id)
        if initial.run_stage is not RunStage.PUBLISHING:
            raise IllegalRunTransitionError(
                "snapshot publication requires the PUBLISHING stage"
            )
        terminal_rejection_retry = initial.run_outcome in {
            RunOutcome.FAILED,
            RunOutcome.CANCELLED,
        }
        if terminal_rejection_retry and (
            initial.error_code not in _RETRYABLE_TERMINAL_REJECTIONS
            or initial.published_snapshot_id is not None
        ):
            raise TerminalRunMutationError(
                "failed and cancelled runs cannot publish snapshot candidates"
            )
        if (
            initial.run_outcome is RunOutcome.RUNNING
            and initial.cancel_requested_at_utc is not None
        ):
            return self._terminalize_failure(
                run_id,
                RunErrorCode.SERIALIZATION_FAILED,
            )

        try:
            candidate = SnapshotCandidateV1.model_validate(
                candidate.model_dump(mode="python", warnings=False)
            )
            authority = PublicationAuthorityV1.model_validate(
                authority.model_dump(mode="python", warnings=False)
            )
        except (TypeError, ValueError) as error:
            if terminal_rejection_retry:
                raise TerminalRunMutationError(
                    "terminal publication retry has invalid candidate authority"
                ) from error
            return self._terminalize_failure(run_id, RunErrorCode.SERIALIZATION_FAILED)

        head = self._repository.get_book_head(initial.book_id)
        try:
            self._validate_authority(
                run=initial,
                head=head,
                candidate=candidate,
                authority=authority,
            )
        except _PublisherSerializationError as error:
            if terminal_rejection_retry:
                raise TerminalRunMutationError(
                    "terminal publication retry differs from controller authority"
                ) from error
            return self._terminalize_failure(run_id, RunErrorCode.SERIALIZATION_FAILED)

        if terminal_rejection_retry:
            return self._terminal_rejection_retry(
                run=initial,
                candidate=candidate,
            )

        if initial.run_outcome is RunOutcome.SUCCEEDED:
            publication = self._completed_run_publication(
                run=initial,
                candidate=candidate,
            )
            return self._commit_verified_publication(
                run_id=run_id,
                publication=publication,
                expected_version=initial.version,
            )

        bindings = self._output_binding_by_key(candidate.manifest_body)
        for output in candidate.outputs:
            try:
                reference = self._store.put_bytes(
                    output.payload,
                    media_type=output.media_type,
                    schema_version=output.schema_version,
                )
            except (SnapshotStoreError, OSError):
                return self._terminalize_failure(
                    run_id,
                    RunErrorCode.DISK_WRITE_FAILED,
                )
            if reference != bindings[(output.logical_role, output.logical_id)].object_ref:
                return self._terminalize_failure(
                    run_id,
                    RunErrorCode.SERIALIZATION_FAILED,
                )

        try:
            manifest = self._canonical_manifest(candidate.manifest_body)
        except (TypeError, ValueError):
            return self._terminalize_failure(run_id, RunErrorCode.SERIALIZATION_FAILED)
        try:
            stored = self._store.put_verified_manifest(manifest)
        except (SnapshotStoreError, OSError):
            return self._terminalize_failure(run_id, RunErrorCode.DISK_WRITE_FAILED)
        try:
            if not isinstance(stored, StoredManifestV1):
                raise TypeError("store must return StoredManifestV1")
            stored = StoredManifestV1.model_validate(
                stored.model_dump(mode="python", warnings=False)
            )
        except (TypeError, ValueError):
            return self._terminalize_failure(run_id, RunErrorCode.SERIALIZATION_FAILED)
        if (
            stored.snapshot_id != manifest.snapshot_id
            or stored.manifest != manifest
            or stored.status is not manifest.body.snapshot_status
        ):
            return self._terminalize_failure(
                run_id,
                RunErrorCode.SERIALIZATION_FAILED,
            )

        self._inject(PublisherFaultStage.AFTER_MANIFEST_DURABLE)
        try:
            verified = self._store.verify_snapshot(stored.snapshot_id)
        except (SnapshotStoreError, ManifestError, OSError):
            return self._terminalize_failure(run_id, RunErrorCode.DISK_WRITE_FAILED)
        try:
            if not isinstance(verified, VerifiedSnapshotV1):
                raise TypeError("store must return VerifiedSnapshotV1")
            verified = VerifiedSnapshotV1.model_validate(
                verified.model_dump(mode="python", warnings=False)
            )
        except (TypeError, ValueError):
            return self._terminalize_failure(run_id, RunErrorCode.SERIALIZATION_FAILED)
        if (
            verified.snapshot_id != manifest.snapshot_id
            or verified.status is not manifest.body.snapshot_status
            or verified.manifest != manifest
        ):
            return self._terminalize_failure(
                run_id,
                RunErrorCode.SERIALIZATION_FAILED,
            )
        self._inject(PublisherFaultStage.AFTER_SNAPSHOT_VERIFIED)

        try:
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
        except (TypeError, ValueError):
            return self._terminalize_failure(run_id, RunErrorCode.SERIALIZATION_FAILED)

        current = self._repository.get(run_id)
        if current.run_outcome is RunOutcome.SUCCEEDED:
            if (
                current.candidate_snapshot_id != stored.snapshot_id
                or current.published_snapshot_id != stored.snapshot_id
            ):
                raise TerminalRunMutationError(
                    "completed run is bound to a different snapshot candidate"
                )
            attached = current
        elif current.run_outcome in {RunOutcome.FAILED, RunOutcome.CANCELLED}:
            return self._terminal_result(current)
        elif current.candidate_snapshot_id == stored.snapshot_id:
            attached = current
        elif current.cancel_requested_at_utc is None:
            self._inject(PublisherFaultStage.BEFORE_CANDIDATE_ATTACH)
            try:
                attached = self._repository.attach_candidate(
                    run_id,
                    stored.snapshot_id,
                    expected_version=current.version,
                    now=max(self._now(), current.updated_at_utc),
                )
            except StaleRunVersionError as error:
                convergence = self._converge_attachment_race(
                    run_id=run_id,
                    snapshot_id=stored.snapshot_id,
                    candidate=candidate,
                    error=error,
                    allow_running=True,
                )
                if isinstance(convergence, SnapshotPublisherResultV1):
                    return convergence
                attached = convergence
            except TerminalRunMutationError as error:
                convergence = self._converge_attachment_race(
                    run_id=run_id,
                    snapshot_id=stored.snapshot_id,
                    candidate=candidate,
                    error=error,
                    allow_running=False,
                )
                if isinstance(convergence, SnapshotPublisherResultV1):
                    return convergence
                attached = convergence
            self._inject(PublisherFaultStage.AFTER_CANDIDATE_ATTACH)
        else:
            attached = current
        return self._commit_verified_publication(
            run_id=run_id,
            publication=publication,
            expected_version=attached.version,
        )


__all__ = [
    "OutputArtifactCandidateV1",
    "PublicationAuthorityV1",
    "PublisherFaultStage",
    "PublisherResultCode",
    "SnapshotCandidateV1",
    "SnapshotPublisher",
    "SnapshotPublisherResultV1",
]
