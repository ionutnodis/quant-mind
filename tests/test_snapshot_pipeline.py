from __future__ import annotations

import hashlib
import importlib
import multiprocessing
import os
import pickle
import sqlite3
import threading
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MethodType

import pytest

from quantmind.snapshots.contracts import (
    GateEvidenceV1,
    GateStatus,
    RecoveryClass,
    RunOutcome,
    RunStage,
    SnapshotStatus,
    ValuationCutV1,
    canonical_json_bytes,
)
from quantmind.snapshots.input_artifacts import (
    ArtifactRefV1,
    ArtifactRightsMode,
    InputArtifactBindingV1,
    InputRepresentation,
    ReproducibilityClass,
    bind_input_artifact,
)
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestBodyV1,
    ManifestPolicyEvidenceV1,
    OutputArtifactBindingV1,
    create_manifest,
)
from quantmind.snapshots.run_repository import (
    ManifestPublicationV1,
    NewRunV1,
    PublicationResultV1,
    RunDatabaseError,
    RunErrorCode,
    RunFailureV1,
    RunRepository,
    StaleRunVersionError,
    TerminalRunMutationError,
)
from quantmind.snapshots.store import (
    ArtifactNotFoundError,
    SnapshotStore,
    SnapshotStoreError,
    SnapshotVerificationError,
    StoredManifestV1,
    VerifiedSnapshotV1,
)


T0 = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 20, 8, 1, tzinfo=UTC)
T2 = datetime(2026, 8, 20, 8, 2, tzinfo=UTC)
T3 = datetime(2026, 8, 20, 8, 3, tzinfo=UTC)
T4 = datetime(2026, 8, 20, 8, 4, tzinfo=UTC)
T5 = datetime(2026, 8, 20, 8, 5, tzinfo=UTC)
T6 = datetime(2026, 8, 20, 8, 6, tzinfo=UTC)


def _artifact_ref(
    payload: bytes,
    *,
    media_type: str,
    schema_version: str,
) -> ArtifactRefV1:
    return ArtifactRefV1(
        hash_algorithm="sha256",
        digest=hashlib.sha256(payload).hexdigest(),
        byte_length=len(payload),
        media_type=media_type,
        schema_version=schema_version,
    )


def _input_binding(reference: ArtifactRefV1) -> InputArtifactBindingV1:
    return bind_input_artifact(
        logical_role="NORMALIZED_MARKS",
        logical_id="marks",
        representation=InputRepresentation.NORMALIZED_INPUT,
        object_ref=reference,
        source="synthetic",
        provider="quantmind",
        entitlement_reference=None,
        entitlement_version=None,
        rights_mode=ArtifactRightsMode.RAW_ALLOWED,
        rights_manifest_version="synthetic-rights-v1",
        reproducibility_class=ReproducibilityClass.NORMALIZED_ONLY,
    )


def _manifest_body(
    *,
    canonical_book_ref: ArtifactRefV1,
    input_binding: InputArtifactBindingV1,
    output_ref: ArtifactRefV1,
) -> AnalyticalSnapshotManifestBodyV1:
    gate = GateEvidenceV1(
        gate_code="BOOK_RECONCILIATION",
        status=GateStatus.PASSED,
        recovery_class=RecoveryClass.USER_RESOLVABLE,
        evidence=("book reconciles",),
        recovery_action="Resolve the book mismatch",
    )
    return AnalyticalSnapshotManifestBodyV1(
        schema_version="analytical_snapshot_manifest_v1",
        canonicalization_version="quantmind_canonical_json_v1",
        hash_algorithm="sha256",
        book_id="book-alpha",
        book_generation=1,
        legacy_book_ref=None,
        valuation_cut=ValuationCutV1(
            target_cut_utc=T0,
            display_timezone="Europe/London",
            capture_start_utc=T0,
            capture_end_utc=T1,
        ),
        base_currency="USD",
        normalized_nlv=Decimal("1000.00"),
        included_account_ids=("account-a",),
        canonical_book_ref=canonical_book_ref,
        canonical_book_hash=canonical_book_ref.digest,
        position_hash="1" * 64,
        input_artifacts=(input_binding,),
        security_master_mapping_version="security-master-v1",
        corporate_action_version=None,
        calendar_version=None,
        rights_manifest_versions=("synthetic-rights-v1",),
        factor_taxonomy_version="factor-taxonomy-v1",
        return_series_version="returns-v1",
        production_covariance_model_version="covariance-v1",
        residual_model_version="residual-v1",
        latent_factor_model_version=None,
        option_pricer_version=None,
        surface_model_version=None,
        scenario_library_version=None,
        analytical_config_hash="2" * 64,
        application_commit="fad089d",
        application_build_id="t3b-test",
        snapshot_status=SnapshotStatus.BLESSED,
        gates=(gate,),
        policy_evidence=(
            ManifestPolicyEvidenceV1(
                subject_kind="OUTPUT",
                subject_id="xray",
                gate_code=gate.gate_code,
            ),
        ),
        warnings=(),
        refused_outputs=(),
        outputs=(
            OutputArtifactBindingV1(
                logical_role="XRAY_READ_MODEL",
                logical_id="xray",
                object_ref=output_ref,
                model_version="xray-v1",
            ),
        ),
    )


def _publishing_run(
    repository: RunRepository,
    *,
    run_id: str = "run_01J5X5S8J5J8P7KQ4Y0T3T3B0A",
    requested_at: datetime = T1,
    stage_at: datetime = T2,
):
    run = repository.create_or_join(
        NewRunV1(
            run_id=run_id,
            run_kind="ANALYTICAL_SNAPSHOT",
            request_fingerprint="a" * 64,
            client_idempotency_key="refresh",
            book_id="book-alpha",
            target_cut_utc=T0,
        ),
        now=requested_at,
    ).record
    run = repository.claim_start(
        run.run_id,
        expected_version=run.version,
        now=requested_at,
    )
    for stage in (
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
        RunStage.PUBLISHING,
    ):
        run = repository.advance_stage(
            run.run_id,
            stage,
            expected_version=run.version,
            now=stage_at,
        )
    return run


def _publisher_case(
    tmp_path: Path,
    *,
    run_id: str,
    repository: RunRepository | None = None,
):
    publisher_module = importlib.import_module("quantmind.snapshots.publisher")
    repository = RunRepository(tmp_path) if repository is None else repository
    repository.initialize()
    store = SnapshotStore(tmp_path)
    canonical_book_ref = store.put_bytes(
        b'{"schema_version":"canonical_book_v1"}',
        media_type="application/json",
        schema_version="canonical_book_v1",
    )
    input_ref = store.put_bytes(
        b'{"marks":"synthetic"}',
        media_type="application/json",
        schema_version="normalized_marks_v1",
    )
    input_binding = _input_binding(input_ref)
    output_payload = b'{"xray":"published"}'
    output_ref = _artifact_ref(
        output_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    body = _manifest_body(
        canonical_book_ref=canonical_book_ref,
        input_binding=input_binding,
        output_ref=output_ref,
    )
    repository.advance_book_head(
        "book-alpha", 1, canonical_book_ref.digest, now=T0
    )
    run = _publishing_run(repository, run_id=run_id)
    authority = publisher_module.PublicationAuthorityV1(
        request_fingerprint=run.request_fingerprint,
        analytical_config_hash=body.analytical_config_hash,
        canonical_book_ref=canonical_book_ref,
        input_artifacts=(input_binding,),
        valuation_cut=body.valuation_cut,
        snapshot_status=body.snapshot_status,
        gates=body.gates,
        policy_evidence=body.policy_evidence,
        warnings=body.warnings,
        refused_outputs=body.refused_outputs,
    )
    candidate = publisher_module.SnapshotCandidateV1(
        manifest_body=body,
        outputs=(
            publisher_module.OutputArtifactCandidateV1(
                logical_role="XRAY_READ_MODEL",
                logical_id="xray",
                payload=output_payload,
                media_type="application/json",
                schema_version="xray_read_model_v1",
                model_version="xray-v1",
            ),
        ),
    )
    return publisher_module, repository, store, run, authority, candidate


def _candidate_storage_path(
    root: Path,
    candidate,
    target: str,
) -> Path:
    manifest = create_manifest(candidate.manifest_body)
    if target == "manifest":
        return (
            root
            / "snapshots"
            / "manifests"
            / "analytical_snapshot_manifest_v1"
            / manifest.snapshot_id[:2]
            / f"{manifest.snapshot_id}.json"
        )
    references = {
        "canonical_book": manifest.body.canonical_book_ref,
        "input": manifest.body.input_artifacts[0].object_ref,
        "output": manifest.body.outputs[0].object_ref,
    }
    reference = references[target]
    return (
        root
        / "snapshots"
        / "objects"
        / "sha256"
        / reference.digest[:2]
        / reference.digest
    )


def _cancelled_publication_rejection_case(tmp_path: Path, *, run_id: str):
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id=run_id,
    )

    def cancel_before_commit(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            repository.request_cancel(run.run_id, now=T3)

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=cancel_before_commit,
    ).publish(run.run_id, candidate, authority=authority)
    assert result.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    return module, repository, store, result.run, authority, candidate


def _publish_then_exit_before_catalog_commit(
    root: str,
    run_id: str,
    candidate,
    authority,
) -> None:
    publisher_module = importlib.import_module("quantmind.snapshots.publisher")

    def exit_after_attachment(stage) -> None:
        if stage is publisher_module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            os._exit(73)

    publisher_module.SnapshotPublisher(
        repository=RunRepository(Path(root)),
        store=SnapshotStore(Path(root)),
        clock=lambda: T6,
        fault_injector=exit_after_attachment,
    ).publish(run_id, candidate, authority=authority)
    os._exit(74)


def _recover_interrupted_in_new_process(root: str, expected_run_id: str) -> None:
    recovered = RunRepository(Path(root)).recover_interrupted(now=T6)
    if recovered != (expected_run_id,):
        os._exit(75)


def _payload_for_stage(payload, stage: RunStage):
    pipeline_module = importlib.import_module("quantmind.snapshots.pipeline")
    return pipeline_module.StagePayloadV1(
        **{
            **payload.model_dump(mode="python"),
            "stage": stage,
        }
    )


def _pipeline_ingest(payload):
    return _payload_for_stage(payload, RunStage.INGESTING)


def _pipeline_reconcile(payload):
    return _payload_for_stage(payload, RunStage.RECONCILING)


def _pipeline_validate(payload):
    pipeline_module = importlib.import_module("quantmind.snapshots.pipeline")
    publisher_module = importlib.import_module("quantmind.snapshots.publisher")
    candidate = publisher_module.SnapshotCandidateV1.model_validate_json(payload.payload)
    body = candidate.manifest_body
    decision = (
        pipeline_module.ValidationDecision.PASS
        if body.snapshot_status is SnapshotStatus.BLESSED
        else pipeline_module.ValidationDecision.DEGRADED
    )
    return pipeline_module.ValidationResultV1(
        decision=decision,
        validated_payload=_payload_for_stage(payload, RunStage.VALIDATING),
        gates=body.gates,
        policy_evidence=body.policy_evidence,
        warnings=body.warnings,
        refused_outputs=body.refused_outputs,
    )


def _pipeline_model(model_input):
    publisher_module = importlib.import_module("quantmind.snapshots.publisher")
    return publisher_module.SnapshotCandidateV1.model_validate_json(
        model_input.validated_payload.payload
    )


def _queued_pipeline_case(
    tmp_path: Path,
    *,
    run_id: str,
    body_transform=None,
    repository: RunRepository | None = None,
):
    pipeline_module = importlib.import_module("quantmind.snapshots.pipeline")
    publisher_module = importlib.import_module("quantmind.snapshots.publisher")
    repository = RunRepository(tmp_path) if repository is None else repository
    repository.initialize()
    store = SnapshotStore(tmp_path)
    canonical_book_ref = store.put_bytes(
        b'{"schema_version":"canonical_book_v1"}',
        media_type="application/json",
        schema_version="canonical_book_v1",
    )
    input_ref = store.put_bytes(
        b'{"marks":"synthetic"}',
        media_type="application/json",
        schema_version="normalized_marks_v1",
    )
    input_binding = _input_binding(input_ref)
    output_payload = b'{"xray":"published"}'
    output_ref = _artifact_ref(
        output_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    body = _manifest_body(
        canonical_book_ref=canonical_book_ref,
        input_binding=input_binding,
        output_ref=output_ref,
    )
    if body_transform is not None:
        body = body_transform(body)
    repository.advance_book_head(
        "book-alpha",
        1,
        canonical_book_ref.digest,
        now=T0,
    )
    run = repository.create_or_join(
        NewRunV1(
            run_id=run_id,
            run_kind="ANALYTICAL_SNAPSHOT",
            request_fingerprint="a" * 64,
            client_idempotency_key="pipeline-refresh",
            book_id="book-alpha",
            target_cut_utc=T0,
        ),
        now=T1,
    ).record
    candidate = publisher_module.SnapshotCandidateV1(
        manifest_body=body,
        outputs=(
            publisher_module.OutputArtifactCandidateV1(
                logical_role="XRAY_READ_MODEL",
                logical_id="xray",
                payload=output_payload,
                media_type="application/json",
                schema_version="xray_read_model_v1",
                model_version="xray-v1",
            ),
        ),
    )
    candidate_payload = candidate.model_dump_json().encode("utf-8")
    initial_payload = pipeline_module.StagePayloadV1(
        stage=RunStage.QUEUED,
        run_id=run.run_id,
        request_fingerprint=run.request_fingerprint,
        book_id=run.book_id,
        book_generation=run.captured_generation,
        target_cut_utc=run.target_cut_utc,
        analytical_config_hash=body.analytical_config_hash,
        schema_version="snapshot_candidate_fixture_v1",
        payload_sha256=hashlib.sha256(candidate_payload).hexdigest(),
        payload=candidate_payload,
    )
    request = pipeline_module.SnapshotPipelineRequestV1(
        run_id=run.run_id,
        request_fingerprint=run.request_fingerprint,
        analytical_config_hash=body.analytical_config_hash,
        canonical_book_ref=canonical_book_ref,
        input_artifacts=(input_binding,),
        valuation_cut=body.valuation_cut,
        initial_payload=initial_payload,
    )
    publisher = publisher_module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    )
    return (
        pipeline_module,
        repository,
        store,
        run,
        request,
        candidate,
        publisher,
    )


def test_publisher_happy_path_persists_verified_bytes_and_one_atomic_publication(
    tmp_path: Path,
) -> None:
    # Break caught: publishing a process-local candidate before its exact bytes,
    # canonical manifest envelope, terminal run, and active pointer agree durably.
    publisher_module = importlib.import_module("quantmind.snapshots.publisher")
    repository = RunRepository(tmp_path)
    repository.initialize()
    store = SnapshotStore(tmp_path)
    canonical_book_ref = store.put_bytes(
        b'{"schema_version":"canonical_book_v1"}',
        media_type="application/json",
        schema_version="canonical_book_v1",
    )
    input_ref = store.put_bytes(
        b'{"marks":"synthetic"}',
        media_type="application/json",
        schema_version="normalized_marks_v1",
    )
    input_binding = _input_binding(input_ref)
    output_payload = b'{"xray":"published"}'
    output_ref = _artifact_ref(
        output_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    body = _manifest_body(
        canonical_book_ref=canonical_book_ref,
        input_binding=input_binding,
        output_ref=output_ref,
    )
    expected_manifest = create_manifest(body)
    expected_envelope = canonical_json_bytes(expected_manifest)
    expected_relpath = (
        "snapshots/manifests/analytical_snapshot_manifest_v1/"
        f"{expected_manifest.snapshot_id[:2]}/{expected_manifest.snapshot_id}.json"
    )
    repository.advance_book_head(
        "book-alpha", 1, canonical_book_ref.digest, now=T0
    )
    run = _publishing_run(repository)
    authority = publisher_module.PublicationAuthorityV1(
        request_fingerprint=run.request_fingerprint,
        analytical_config_hash=body.analytical_config_hash,
        canonical_book_ref=canonical_book_ref,
        input_artifacts=(input_binding,),
        valuation_cut=body.valuation_cut,
        snapshot_status=body.snapshot_status,
        gates=body.gates,
        policy_evidence=body.policy_evidence,
        warnings=body.warnings,
        refused_outputs=body.refused_outputs,
    )
    candidate = publisher_module.SnapshotCandidateV1(
        manifest_body=body,
        outputs=(
            publisher_module.OutputArtifactCandidateV1(
                logical_role="XRAY_READ_MODEL",
                logical_id="xray",
                payload=output_payload,
                media_type="application/json",
                schema_version="xray_read_model_v1",
                model_version="xray-v1",
            ),
        ),
    )

    result = publisher_module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.run.run_outcome is RunOutcome.SUCCEEDED
    assert result.published is True
    assert result.already_published is False
    assert result.publication is not None
    assert result.publication.publication_sequence == 1
    assert result.publication.snapshot_id == expected_manifest.snapshot_id
    assert result.publication.manifest_relpath == expected_relpath
    assert result.publication.snapshot_status is SnapshotStatus.BLESSED
    assert result.publication.book_id == "book-alpha"
    assert result.publication.book_generation == 1
    assert result.publication.envelope_sha256 != result.publication.snapshot_id
    assert result.run.candidate_snapshot_id == expected_manifest.snapshot_id
    assert result.run.published_snapshot_id == expected_manifest.snapshot_id
    assert result.active is not None
    assert result.active.snapshot_id == result.publication.snapshot_id
    assert result.active.pointer_version == 1
    assert store.read_verified_artifact(output_ref) == output_payload
    verified = SnapshotStore(tmp_path).verify_snapshot(result.publication.snapshot_id)
    assert verified.manifest == expected_manifest
    assert verified.manifest.body.outputs[0].object_ref == output_ref
    assert result.publication.envelope_sha256 == hashlib.sha256(
        expected_envelope
    ).hexdigest()
    assert result.publication.envelope_byte_length == len(expected_envelope)
    reopened = RunRepository(tmp_path)
    assert reopened.get(run.run_id) == result.run
    assert reopened.list_publications("book-alpha") == (result.publication,)
    assert reopened.get_active("book-alpha") == result.active


@pytest.mark.parametrize(
    "fault_stage_name",
    ["AFTER_MANIFEST_DURABLE", "BEFORE_CANDIDATE_ATTACH"],
)
def test_publisher_cancellation_races_use_cancellation_first_catalog_cas(
    tmp_path: Path,
    fault_stage_name: str,
) -> None:
    # Break caught: a cancel request racing durable manifest bytes or candidate attach
    # becoming a generic stale/serialization failure, publication, or pointer move.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0B",
    )
    fault_stage = getattr(module.PublisherFaultStage, fault_stage_name)

    def request_cancel(stage) -> None:
        if stage is fault_stage:
            repository.request_cancel(run.run_id, now=T3)

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=request_cancel,
    ).publish(run.run_id, candidate, authority=authority)

    expected_manifest = create_manifest(candidate.manifest_body)
    assert store.verify_snapshot(expected_manifest.snapshot_id).manifest == expected_manifest
    assert result.published is False
    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.candidate_snapshot_id is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None
    assert RunRepository(tmp_path).get(run.run_id) == result.run


@pytest.mark.parametrize(
    "forgery",
    ["worker_valuation_cut", "durable_target_cut", "gate_decision"],
)
def test_publisher_refuses_candidate_that_diverges_from_controller_authority(
    tmp_path: Path,
    forgery: str,
) -> None:
    # Break caught: trusting a worker-produced, internally valid valuation cut or
    # gate decision without comparing controller-owned request/validation authority.
    module, repository, store, run, old_authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0C",
    )
    authoritative_body = candidate.manifest_body
    candidate_body = authoritative_body
    authority_body = authoritative_body
    if forgery in {"worker_valuation_cut", "durable_target_cut"}:
        forged_cut = ValuationCutV1(
            target_cut_utc=T1,
            display_timezone="Europe/London",
            capture_start_utc=T0,
            capture_end_utc=T1,
        )
        candidate_body = authoritative_body.model_copy(
            update={"valuation_cut": forged_cut}
        )
        if forgery == "durable_target_cut":
            authority_body = candidate_body
    else:
        warned_gate = authoritative_body.gates[0].model_copy(
            update={"status": GateStatus.WARNED}
        )
        candidate_body = authoritative_body.model_copy(update={"gates": (warned_gate,)})

    authority = module.PublicationAuthorityV1(
        request_fingerprint=old_authority.request_fingerprint,
        analytical_config_hash=old_authority.analytical_config_hash,
        canonical_book_ref=old_authority.canonical_book_ref,
        input_artifacts=old_authority.input_artifacts,
        valuation_cut=authority_body.valuation_cut,
        snapshot_status=authority_body.snapshot_status,
        gates=authority_body.gates,
        policy_evidence=authority_body.policy_evidence,
        warnings=authority_body.warnings,
        refused_outputs=authority_body.refused_outputs,
    )
    forged_candidate = module.SnapshotCandidateV1(
        manifest_body=candidate_body,
        outputs=candidate.outputs,
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, forged_candidate, authority=authority)

    durable = repository.get(run.run_id)
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert durable == result.run
    assert durable.run_outcome is RunOutcome.FAILED
    assert durable.error_code is RunErrorCode.SERIALIZATION_FAILED
    assert durable.candidate_snapshot_id is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize(
    "bypass",
    ["stored_path", "stored_digest", "stored_status", "verified_snapshot"],
)
def test_publisher_ignores_untrusted_store_result_overrides(
    tmp_path: Path,
    bypass: str,
) -> None:
    # Break caught: dispatching writes or verification through an injected store adapter
    # that can fabricate otherwise plausible result contracts.
    override_calls: list[str] = []

    class BypassingStore(SnapshotStore):
        def put_verified_manifest(self, manifest):
            override_calls.append("put_verified_manifest")
            stored = super().put_verified_manifest(manifest)
            if bypass == "stored_path":
                return stored.model_copy(
                    update={"manifest_relpath": "snapshots/manifests/wrong.json"}
                )
            if bypass == "stored_digest":
                return stored.model_copy(update={"envelope_sha256": "f" * 64})
            if bypass == "stored_status":
                return stored.model_copy(update={"status": SnapshotStatus.DEGRADED})
            return stored

        def verify_snapshot(self, snapshot_id, *, required_output_roles=()):
            override_calls.append("verify_snapshot")
            verified = super().verify_snapshot(
                snapshot_id,
                required_output_roles=required_output_roles,
            )
            if bypass == "verified_snapshot":
                return object()
            return verified

    module, repository, _store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0D",
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=BypassingStore(tmp_path),
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    durable = repository.get(run.run_id)
    expected_manifest = create_manifest(candidate.manifest_body)
    assert override_calls == []
    assert result.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert result.published is True
    assert durable == result.run
    assert durable.run_outcome is RunOutcome.SUCCEEDED
    assert durable.published_snapshot_id == expected_manifest.snapshot_id
    assert SnapshotStore(tmp_path).verify_snapshot(
        expected_manifest.snapshot_id
    ).manifest == expected_manifest
    assert repository.get_active("book-alpha").snapshot_id == expected_manifest.snapshot_id


def test_publisher_prevents_valid_manifest_substitution_by_store_adapter(
    tmp_path: Path,
) -> None:
    # Break caught: letting an injected store adapter substitute a different, internally
    # valid manifest for the publisher's locally created candidate identity.
    substituted_manifest = None
    override_called = False

    class ValidManifestSubstitutingStore(SnapshotStore):
        def put_verified_manifest(self, manifest):
            nonlocal override_called, substituted_manifest
            override_called = True
            substituted_manifest = create_manifest(
                manifest.body.model_copy(
                    update={"application_build_id": "hostile-store-substitution"}
                )
            )
            assert substituted_manifest.snapshot_id != manifest.snapshot_id
            return super().put_verified_manifest(substituted_manifest)

    module, repository, _store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B4A",
    )
    local_manifest = create_manifest(candidate.manifest_body)

    result = module.SnapshotPublisher(
        repository=repository,
        store=ValidManifestSubstitutingStore(tmp_path),
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert override_called is False
    assert substituted_manifest is None
    assert SnapshotStore(tmp_path).verify_snapshot(
        local_manifest.snapshot_id
    ).manifest == local_manifest
    assert result.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert result.published is True
    assert result.run.run_outcome is RunOutcome.SUCCEEDED
    assert result.run.candidate_snapshot_id == local_manifest.snapshot_id
    assert result.run.published_snapshot_id == local_manifest.snapshot_id
    assert repository.get(run.run_id) == result.run
    assert tuple(
        publication.snapshot_id
        for publication in repository.list_publications("book-alpha")
    ) == (local_manifest.snapshot_id,)
    assert repository.get_active("book-alpha").snapshot_id == local_manifest.snapshot_id


@pytest.mark.parametrize("forgery", ["request_fingerprint", "analytical_config"])
def test_publisher_binds_controller_request_and_config_identity(
    tmp_path: Path,
    forgery: str,
) -> None:
    # Break caught: a worker/controller candidate retaining valid book/gate contracts
    # while changing the durable request or analytical-configuration identity.
    module, repository, store, run, old_authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0E",
    )
    request_fingerprint = run.request_fingerprint
    analytical_config_hash = candidate.manifest_body.analytical_config_hash
    if forgery == "request_fingerprint":
        request_fingerprint = "b" * 64
    else:
        analytical_config_hash = "3" * 64
    authority = module.PublicationAuthorityV1(
        canonical_book_ref=old_authority.canonical_book_ref,
        input_artifacts=old_authority.input_artifacts,
        valuation_cut=old_authority.valuation_cut,
        snapshot_status=old_authority.snapshot_status,
        gates=old_authority.gates,
        policy_evidence=old_authority.policy_evidence,
        warnings=old_authority.warnings,
        refused_outputs=old_authority.refused_outputs,
        request_fingerprint=request_fingerprint,
        analytical_config_hash=analytical_config_hash,
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    durable = repository.get(run.run_id)
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert durable == result.run
    assert durable.run_outcome is RunOutcome.FAILED
    assert durable.error_code is RunErrorCode.SERIALIZATION_FAILED
    assert durable.candidate_snapshot_id is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_publication_authority_caps_total_nested_canonical_bytes(tmp_path: Path) -> None:
    # Break caught: individually valid nested input metadata expanding controller
    # authority to an unbounded serialized payload.
    module, _repository, _store, _run, authority, _candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0F",
    )
    oversized_binding = authority.input_artifacts[0].model_copy(
        update={"source": "x" * (2 * 1024 * 1024)}
    )

    with pytest.raises(ValueError, match="bounded|bytes"):
        module.PublicationAuthorityV1(
            **{
                **authority.model_dump(mode="python"),
                "input_artifacts": (oversized_binding,),
            }
        )


def test_publication_authority_caps_aggregate_input_artifact_bytes(
    tmp_path: Path,
) -> None:
    # Break caught: individually valid input references claiming an unbounded total
    # read budget immediately before publication acquires its SQLite write lock.
    module, _repository, _store, _run, authority, _candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0L",
    )
    base = authority.input_artifacts[0]

    def input_with_size(logical_id: str, byte_length: int) -> InputArtifactBindingV1:
        return base.model_copy(
            update={
                "logical_id": logical_id,
                "object_ref": base.object_ref.model_copy(
                    update={"byte_length": byte_length}
                ),
            }
        )

    bounded_inputs = (
        input_with_size("marks", 32 * 1024 * 1024),
        input_with_size("marks-2", 31 * 1024 * 1024),
    )
    bounded_canonical = authority.canonical_book_ref.model_copy(
        update={"byte_length": 1 * 1024 * 1024}
    )
    accepted = module.PublicationAuthorityV1(
        **{
            **authority.model_dump(mode="python"),
            "canonical_book_ref": bounded_canonical,
            "input_artifacts": bounded_inputs,
        }
    )
    assert accepted.canonical_book_ref.byte_length + sum(
        binding.object_ref.byte_length for binding in accepted.input_artifacts
    ) == 64 * 1024 * 1024

    with pytest.raises(ValueError, match="artifact read bytes.*bound"):
        module.PublicationAuthorityV1(
            **{
                **authority.model_dump(mode="python"),
                "canonical_book_ref": bounded_canonical,
                "input_artifacts": (
                    input_with_size("marks", 33 * 1024 * 1024),
                    input_with_size("marks-2", 31 * 1024 * 1024),
                ),
            }
        )


def test_snapshot_candidate_caps_aggregate_output_payload_bytes(tmp_path: Path) -> None:
    # Break caught: many individually bounded artifacts creating an unbounded worker
    # candidate through aggregate payload size.
    module, _repository, _store, _run, _authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0G",
    )
    payload = b"x" * (33 * 1024 * 1024)
    outputs = tuple(
        module.OutputArtifactCandidateV1(
            logical_role=role,
            logical_id=logical_id,
            payload=payload,
            media_type="application/octet-stream",
            schema_version="bounded_test_v1",
            model_version="bounded-test-v1",
        )
        for role, logical_id in (("A", "a"), ("B", "b"))
    )

    with pytest.raises(ValueError, match="aggregate|payload"):
        module.SnapshotCandidateV1(
            manifest_body=candidate.manifest_body,
            outputs=outputs,
        )


@pytest.mark.parametrize("ordering_defect", ["duplicate", "unsorted"])
def test_snapshot_candidate_rejects_duplicate_or_unsorted_output_keys(
    tmp_path: Path,
    ordering_defect: str,
) -> None:
    # Break caught: nondeterministic or ambiguous output identity reaching the
    # filesystem publication loop.
    module, _repository, _store, _run, _authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1A",
    )
    original = candidate.outputs[0]
    if ordering_defect == "duplicate":
        outputs = (original, original)
    else:
        outputs = (
            original.model_copy(
                update={"logical_role": "ZZZ_ROLE", "logical_id": "z"}
            ),
            original.model_copy(
                update={"logical_role": "AAA_ROLE", "logical_id": "a"}
            ),
        )

    with pytest.raises(ValueError, match="unique|sorted"):
        module.SnapshotCandidateV1(
            manifest_body=candidate.manifest_body,
            outputs=outputs,
        )


@pytest.mark.parametrize(
    "bypass",
    ["output_model_copy", "candidate_model_construct", "authority_model_copy"],
)
def test_publisher_revalidates_bypassed_public_input_models(
    tmp_path: Path,
    bypass: str,
) -> None:
    # Break caught: accepting isinstance-compatible Pydantic model_copy/model_construct
    # values that bypassed the public contract validators.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1B",
    )
    if bypass == "output_model_copy":
        invalid_output = candidate.outputs[0].model_copy(update={"logical_id": ""})
        candidate = module.SnapshotCandidateV1.model_construct(
            manifest_body=candidate.manifest_body,
            outputs=(invalid_output,),
        )
    elif bypass == "candidate_model_construct":
        candidate = module.SnapshotCandidateV1.model_construct(
            manifest_body=candidate.manifest_body,
            outputs=(),
        )
    else:
        authority = authority.model_copy(update={"request_fingerprint": "short"})

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is RunErrorCode.SERIALIZATION_FAILED
    assert repository.get(run.run_id) == result.run
    assert repository.list_publications("book-alpha") == ()


def test_publisher_public_contracts_pickle_round_trip(tmp_path: Path) -> None:
    # Break caught: process-boundary contracts retaining process-local state or losing
    # frozen identity when passed to or returned from a spawned worker.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1C",
    )
    for contract in (candidate.outputs[0], candidate, authority):
        assert pickle.loads(pickle.dumps(contract)) == contract

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert pickle.loads(pickle.dumps(result)) == result


@pytest.mark.parametrize(
    "bypass",
    [
        "model_copy",
        "model_construct",
        "nested_model_copy",
        "publication_run_id",
        "publication_book_id",
        "publication_generation",
        "publication_snapshot_id",
        "publication_published_at",
        "active_book_id",
        "active_snapshot_provenance",
    ],
)
def test_publisher_result_rejects_bypassed_tag_payload_combinations(
    tmp_path: Path,
    bypass: str,
) -> None:
    # Break caught: a validation-bypassed publisher result claiming success without
    # catalog evidence or a terminal failure over a succeeded durable run.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1D",
    )
    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)
    if bypass == "model_copy":
        invalid = result.model_copy(update={"publication_result": None})
    elif bypass == "model_construct":
        invalid = module.SnapshotPublisherResultV1.model_construct(
            result_code=module.PublisherResultCode.TERMINAL_FAILURE,
            run=result.run,
            publication_result=None,
        )
    elif bypass == "nested_model_copy":
        invalid = result.model_copy(
            update={
                "publication_result": result.publication_result.model_copy(
                    update={"published": False}
                )
            }
        )
    elif bypass.startswith("publication_"):
        publication = result.publication
        assert publication is not None
        if bypass == "publication_run_id":
            update = {"run_id": "run_01J5X5S8J5J8P7KQ4Y0T3T3B9Z"}
        elif bypass == "publication_book_id":
            update = {"book_id": "book-beta"}
        elif bypass == "publication_generation":
            update = {"book_generation": publication.book_generation + 1}
        elif bypass == "publication_snapshot_id":
            substituted_id = "f" * 64
            update = {
                "snapshot_id": substituted_id,
                "manifest_relpath": (
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{substituted_id[:2]}/{substituted_id}.json"
                ),
            }
        else:
            update = {"published_at_utc": T4}
        invalid = result.model_copy(
            update={
                "publication_result": result.publication_result.model_copy(
                    update={"publication": publication.model_copy(update=update)}
                )
            }
        )
    else:
        active = result.active
        assert active is not None
        if bypass == "active_book_id":
            update = {"book_id": "book-beta"}
        else:
            update = {
                "snapshot_id": "e" * 64,
                "book_generation": active.book_generation + 1,
            }
        invalid = result.model_copy(
            update={
                "publication_result": result.publication_result.model_copy(
                    update={"active": active.model_copy(update=update)}
                )
            }
        )

    with pytest.raises(ValueError, match="catalog|terminal|publication|active|provenance"):
        module.SnapshotPublisherResultV1.model_validate(
            invalid.model_dump(mode="python", warnings=False)
        )


@pytest.mark.parametrize(
    "bypass",
    ["publication_evidence", "already_published", "active_book_id"],
)
def test_publisher_rejection_result_refuses_incoherent_nested_evidence(
    tmp_path: Path,
    bypass: str,
) -> None:
    # Break caught: a valid-looking nested publication, idempotency claim, or active
    # pointer from another book being grafted onto a durable catalog rejection.
    module, repository, store, first_run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B4B",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(first_run.run_id, candidate, authority=authority)
    assert first.publication is not None
    assert first.active is not None

    rejected_run = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B4C",
        requested_at=T3,
        stage_at=T3,
    )

    def cancel_before_commit(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            repository.request_cancel(rejected_run.run_id, now=T3)

    rejected = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=cancel_before_commit,
    ).publish(rejected_run.run_id, candidate, authority=authority)
    assert rejected.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert rejected.publication_result is not None
    assert rejected.active is not None

    if bypass == "publication_evidence":
        update = {"publication": first.publication}
    elif bypass == "already_published":
        update = {"already_published": True}
    else:
        update = {
            "active": rejected.active.model_copy(update={"book_id": "book-beta"})
        }
    invalid = rejected.model_copy(
        update={"publication_result": rejected.publication_result.model_copy(update=update)}
    )

    with pytest.raises(ValueError, match="catalog|rejection|publication|active"):
        module.SnapshotPublisherResultV1.model_validate(
            invalid.model_dump(mode="python", warnings=False)
        )


def test_publisher_result_allows_active_pointer_that_advanced_after_publication(
    tmp_path: Path,
) -> None:
    # Protective regression: an idempotent response for the first publication may carry
    # the same-book active pointer advanced by a later valid publication.
    module, repository, store, first_run, authority, first_candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B4D",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(first_run.run_id, first_candidate, authority=authority)
    assert first.publication is not None

    replacement_payload = b'{"xray":"later-publication"}'
    replacement_ref = _artifact_ref(
        replacement_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    replacement_candidate = module.SnapshotCandidateV1(
        manifest_body=first_candidate.manifest_body.model_copy(
            update={
                "outputs": (
                    first_candidate.manifest_body.outputs[0].model_copy(
                        update={"object_ref": replacement_ref}
                    ),
                )
            }
        ),
        outputs=(
            first_candidate.outputs[0].model_copy(
                update={"payload": replacement_payload}
            ),
        ),
    )
    second_run = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B4E",
        requested_at=T3,
        stage_at=T3,
    )
    second = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T4,
    ).publish(second_run.run_id, replacement_candidate, authority=authority)
    assert second.publication is not None
    assert second.active is not None

    retried = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T4,
    ).publish(first_run.run_id, first_candidate, authority=authority)

    assert retried.already_published is True
    assert retried.publication == first.publication
    assert retried.active == second.active
    assert retried.active.snapshot_id != retried.publication.snapshot_id
    assert retried.active.pointer_version > (
        retried.run.expected_active_pointer_version + 1
    )


def test_terminal_succeeded_publisher_retry_reverifies_and_skips_attachment(
    tmp_path: Path,
) -> None:
    # Break caught: a response-loss retry either refusing a matching durable success,
    # reattaching the immutable candidate, or advancing publication/pointer identity twice.
    class NoAttachRepository(RunRepository):
        def attach_candidate(self, *args, **kwargs):
            raise AssertionError("terminal retry must skip candidate attachment")

    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0H",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    repeated = module.SnapshotPublisher(
        repository=NoAttachRepository(tmp_path),
        store=SnapshotStore(tmp_path),
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert repeated.published is True
    assert repeated.already_published is True
    assert repeated.run == first.run
    assert repeated.publication == first.publication
    assert repeated.active == first.active
    assert len(repository.list_publications("book-alpha")) == 1
    assert repository.get_active("book-alpha").pointer_version == 1


@pytest.mark.parametrize("stored_target", ["artifact", "manifest"])
@pytest.mark.parametrize("stored_state", ["missing", "tampered"])
def test_terminal_succeeded_retry_reverifies_existing_snapshot_bytes(
    tmp_path: Path,
    stored_target: str,
    stored_state: str,
) -> None:
    # Break caught: terminal retry trusting catalog success without touching bytes,
    # or rewriting history when re-verification detects immutable corruption.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1E",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)
    output = candidate.outputs[0]
    reference = output.artifact_ref()
    if stored_target == "artifact":
        target_path = (
            tmp_path
            / "snapshots"
            / "objects"
            / "sha256"
            / reference.digest[:2]
            / reference.digest
        )
    else:
        target_path = tmp_path / first.publication.manifest_relpath
    if stored_state == "missing":
        target_path.unlink()
    else:
        target_path.write_bytes(b"tampered immutable bytes")

    publisher = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    )
    with pytest.raises(SnapshotVerificationError):
        publisher.publish(run.run_id, candidate, authority=authority)

    assert repository.get(run.run_id) == first.run
    assert repository.get_active("book-alpha") == first.active
    assert repository.list_publications("book-alpha") == (first.publication,)


def test_repository_active_cas_fault_rolls_back_catalog_and_retains_orphan_candidate(
    tmp_path: Path,
) -> None:
    # Break caught: a precommit repository fault leaving a manifest row, succeeded run,
    # or active pointer after the transaction reports failure.
    armed = False

    def fail_after_active_cas(stage: str) -> None:
        if armed and stage == "db.after_active_cas":
            raise OSError("injected precommit database fault")

    repository = RunRepository(tmp_path, fault_injector=fail_after_active_cas)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0J",
        repository=repository,
    )
    armed = True

    with pytest.raises(RunDatabaseError):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
        ).publish(run.run_id, candidate, authority=authority)

    expected_manifest = create_manifest(candidate.manifest_body)
    assert SnapshotStore(tmp_path).verify_snapshot(
        expected_manifest.snapshot_id
    ).manifest == expected_manifest
    durable = RunRepository(tmp_path).get(run.run_id)
    assert durable.run_outcome is RunOutcome.RUNNING
    assert durable.candidate_snapshot_id == expected_manifest.snapshot_id
    assert RunRepository(tmp_path).list_publications("book-alpha") == ()
    assert RunRepository(tmp_path).get_active("book-alpha") is None


@pytest.mark.parametrize(
    ("stored_target", "corruption"),
    [
        ("manifest", "missing"),
        ("canonical_book", "tampered"),
        ("input", "missing"),
        ("output", "tampered"),
    ],
)
def test_publisher_precommit_reopens_manifest_and_every_reference_before_success(
    tmp_path: Path,
    stored_target: str,
    corruption: str,
) -> None:
    # Break caught: the publisher's final callback changing already-verified canonical
    # bytes and the catalog still committing a false SUCCEEDED/active publication.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0G",
    )

    def corrupt_after_publisher_verification(stage) -> None:
        if stage is not module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            return
        target_path = _candidate_storage_path(tmp_path, candidate, stored_target)
        if corruption == "missing":
            target_path.unlink()
        else:
            target_path.write_bytes(b"tampered after publisher verification")

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=corrupt_after_publisher_verification,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is RunErrorCode.DISK_WRITE_FAILED
    assert repository.get(run.run_id) == result.run
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize("stored_target", ["input", "output"])
def test_publisher_precommit_reopens_nonfirst_manifest_references(
    tmp_path: Path,
    stored_target: str,
) -> None:
    # Break caught: final verification checking only the first input/output reference
    # and allowing corruption of a later manifest-bound object before catalog success.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0R",
    )
    second_input_ref = store.put_bytes(
        b'{"marks":"secondary"}',
        media_type="application/json",
        schema_version="normalized_marks_v1",
    )
    second_input = candidate.manifest_body.input_artifacts[0].model_copy(
        update={
            "logical_id": "marks-2",
            "object_ref": second_input_ref,
        }
    )
    second_output_payload = b'{"xray":"secondary"}'
    second_output_ref = _artifact_ref(
        second_output_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    second_output_binding = candidate.manifest_body.outputs[0].model_copy(
        update={
            "logical_id": "xray-2",
            "object_ref": second_output_ref,
        }
    )
    second_policy_evidence = candidate.manifest_body.policy_evidence[0].model_copy(
        update={"subject_id": "xray-2"}
    )
    candidate = module.SnapshotCandidateV1(
        manifest_body=candidate.manifest_body.model_copy(
            update={
                "input_artifacts": (
                    candidate.manifest_body.input_artifacts[0],
                    second_input,
                ),
                "outputs": (
                    candidate.manifest_body.outputs[0],
                    second_output_binding,
                ),
                "policy_evidence": (
                    candidate.manifest_body.policy_evidence[0],
                    second_policy_evidence,
                ),
            }
        ),
        outputs=(
            candidate.outputs[0],
            module.OutputArtifactCandidateV1(
                logical_role="XRAY_READ_MODEL",
                logical_id="xray-2",
                payload=second_output_payload,
                media_type="application/json",
                schema_version="xray_read_model_v1",
                model_version="xray-v1",
            ),
        ),
    )
    authority = authority.model_copy(
        update={
            "input_artifacts": candidate.manifest_body.input_artifacts,
            "policy_evidence": candidate.manifest_body.policy_evidence,
        }
    )
    target_ref = (
        second_input_ref if stored_target == "input" else second_output_ref
    )
    target_path = (
        tmp_path
        / "snapshots"
        / "objects"
        / "sha256"
        / target_ref.digest[:2]
        / target_ref.digest
    )

    def corrupt_nonfirst_reference(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            target_path.write_bytes(b"tampered nonfirst reference")

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=corrupt_nonfirst_reference,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is RunErrorCode.DISK_WRITE_FAILED
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_final_filesystem_validation_precedes_sqlite_write_transaction(
    tmp_path: Path,
) -> None:
    # Break caught: acquiring a SQLite write transaction before final filesystem
    # validation, which can hold the catalog writer while large inputs are read.
    transaction_stages: list[str] = []

    def record_database_stage(stage: str) -> None:
        transaction_stages.append(stage)

    repository = RunRepository(tmp_path, fault_injector=record_database_stage)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0H",
        repository=repository,
    )
    def corrupt_before_final_validation(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            _candidate_storage_path(tmp_path, candidate, "output").unlink()

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=corrupt_before_final_validation,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is RunErrorCode.DISK_WRITE_FAILED
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None
    assert "db.before_begin" not in transaction_stages


def test_publisher_rejects_subclass_validator_override() -> None:
    # Break caught: a publisher subclass overriding the protected validator with a
    # no-op and bypassing final canonical-byte verification before catalog success.
    module = importlib.import_module("quantmind.snapshots.publisher")

    with pytest.raises(TypeError, match="does not support subclassing"):

        class NoOpValidatorPublisher(module.SnapshotPublisher):
            def _validate_publication_precommit(self, publication) -> None:
                del publication


def test_publisher_rejects_subclass_store_attribute_redirect() -> None:
    # Break caught: an overriding __getattribute__ redirecting the exact base validator
    # to a clean mirror store after the real canonical manifest was deleted.
    module = importlib.import_module("quantmind.snapshots.publisher")

    with pytest.raises(TypeError, match="does not support subclassing"):

        class RedirectedStorePublisher(module.SnapshotPublisher):
            def __init__(self, *, redirected_store: SnapshotStore, **kwargs) -> None:
                self._redirected_store = redirected_store

            def __getattribute__(self, name: str):
                if name == "_store":
                    return object.__getattribute__(self, "_redirected_store")
                return super().__getattribute__(name)


def test_publisher_rejects_subclass_repository_attribute_redirect() -> None:
    # Break caught: an overriding __getattribute__ redirecting the transaction seam
    # from the captured run catalog to a forged but internally coherent mirror catalog.
    module = importlib.import_module("quantmind.snapshots.publisher")

    with pytest.raises(TypeError, match="does not support subclassing"):

        class RedirectedRepositoryPublisher(module.SnapshotPublisher):
            def __init__(
                self,
                *,
                redirected_repository: RunRepository,
                **kwargs,
            ) -> None:
                self._redirected_repository = redirected_repository

            def __getattribute__(self, name: str):
                if name == "_repository":
                    return object.__getattribute__(self, "_redirected_repository")
                return super().__getattribute__(name)


def test_precommit_corruption_keeps_concurrent_cancellation_precedence(
    tmp_path: Path,
) -> None:
    # Break caught: filesystem validation masking a cancellation that the catalog
    # observes before deciding whether this publication may become successful.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0J",
    )

    def corrupt_and_cancel_before_commit(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            _candidate_storage_path(tmp_path, candidate, "manifest").unlink()
            repository.request_cancel(run.run_id, now=T3)

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=corrupt_and_cancel_before_commit,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize("stored_target", ["manifest", "output"])
def test_succeeded_retry_surfaces_typed_final_verification_corruption_without_mutation(
    tmp_path: Path,
    stored_target: str,
) -> None:
    # Break caught: an idempotent retry re-verifying before its last callback, then
    # returning historical success after that callback corrupts canonical bytes.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0K",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    def corrupt_after_retry_verification(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            _candidate_storage_path(tmp_path, candidate, stored_target).write_bytes(
                b"tampered on idempotent retry"
            )

    with pytest.raises(SnapshotStoreError):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
            fault_injector=corrupt_after_retry_verification,
        ).publish(run.run_id, candidate, authority=authority)

    assert repository.get(run.run_id) == first.run
    assert repository.list_publications("book-alpha") == (first.publication,)
    assert repository.get_active("book-alpha") == first.active


def test_repository_after_commit_fault_is_resolved_as_durable_success(
    tmp_path: Path,
) -> None:
    # Break caught: T3A response uncertainty being converted by the publisher into a
    # failed/unknown result after the publication transaction is already durable.
    armed = False

    def fail_after_commit(stage: str) -> None:
        if armed and stage == "db.after_commit":
            raise OSError("lost repository commit response")

    repository = RunRepository(tmp_path, fault_injector=fail_after_commit)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0K",
        repository=repository,
    )
    armed = True

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.run.run_outcome is RunOutcome.SUCCEEDED
    assert result.published is True
    assert len(RunRepository(tmp_path).list_publications("book-alpha")) == 1
    assert RunRepository(tmp_path).get_active("book-alpha").pointer_version == 1


def test_publisher_after_commit_fault_surfaces_uncertainty_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    # Break caught: a publisher-local postcommit fault being misrecorded as failure or
    # causing its exact retry to append a second publication/pointer transition.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0L",
    )

    def fail_after_repository_commit(stage) -> None:
        if stage is module.PublisherFaultStage.AFTER_REPOSITORY_COMMIT:
            raise OSError("lost publisher response")

    with pytest.raises(OSError, match="lost publisher response"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
            fault_injector=fail_after_repository_commit,
        ).publish(run.run_id, candidate, authority=authority)

    durable = RunRepository(tmp_path)
    assert durable.get(run.run_id).run_outcome is RunOutcome.SUCCEEDED
    assert len(durable.list_publications("book-alpha")) == 1
    assert durable.get_active("book-alpha").pointer_version == 1

    retried = module.SnapshotPublisher(
        repository=durable,
        store=SnapshotStore(tmp_path),
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)
    assert retried.already_published is True
    assert len(durable.list_publications("book-alpha")) == 1
    assert durable.get_active("book-alpha").pointer_version == 1


def test_real_process_exit_after_attachment_recovers_verified_orphan(
    tmp_path: Path,
) -> None:
    # Break caught: process death after the candidate CAS accidentally exposing the
    # orphan, losing its recovery identity, or changing the prior active snapshot.
    module, repository, store, first_run, first_authority, first_candidate = (
        _publisher_case(
            tmp_path,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1F",
        )
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(first_run.run_id, first_candidate, authority=first_authority)
    prior_active = first.active
    assert prior_active is not None

    canonical_ref = store.put_bytes(
        b'{"schema_version":"canonical_book_v1","generation":2}',
        media_type="application/json",
        schema_version="canonical_book_v1",
    )
    repository.advance_book_head(
        "book-alpha",
        2,
        canonical_ref.digest,
        now=T4,
    )
    second_run = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B1G",
        requested_at=T4,
        stage_at=T5,
    )
    second_body = first_candidate.manifest_body.model_copy(
        update={
            "book_generation": 2,
            "canonical_book_ref": canonical_ref,
            "canonical_book_hash": canonical_ref.digest,
            "application_build_id": "t3b-crash-test",
        }
    )
    second_candidate = module.SnapshotCandidateV1(
        manifest_body=second_body,
        outputs=first_candidate.outputs,
    )
    second_authority = module.PublicationAuthorityV1(
        request_fingerprint=second_run.request_fingerprint,
        analytical_config_hash=second_body.analytical_config_hash,
        canonical_book_ref=canonical_ref,
        input_artifacts=second_body.input_artifacts,
        valuation_cut=second_body.valuation_cut,
        snapshot_status=second_body.snapshot_status,
        gates=second_body.gates,
        policy_evidence=second_body.policy_evidence,
        warnings=second_body.warnings,
        refused_outputs=second_body.refused_outputs,
    )
    orphan_manifest = create_manifest(second_body)

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_publish_then_exit_before_catalog_commit,
        args=(str(tmp_path), second_run.run_id, second_candidate, second_authority),
    )
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("publisher crash subprocess did not reach its exit seam")
    assert process.exitcode == 73

    reopened = RunRepository(tmp_path)
    interrupted = reopened.get(second_run.run_id)
    assert interrupted.run_outcome is RunOutcome.RUNNING
    assert interrupted.candidate_snapshot_id == orphan_manifest.snapshot_id
    assert reopened.list_publications("book-alpha") == (first.publication,)
    assert reopened.get_active("book-alpha") == prior_active
    assert SnapshotStore(tmp_path).verify_snapshot(
        orphan_manifest.snapshot_id
    ).manifest == orphan_manifest

    recovery_process = context.Process(
        target=_recover_interrupted_in_new_process,
        args=(str(tmp_path), second_run.run_id),
    )
    recovery_process.start()
    recovery_process.join(20)
    if recovery_process.is_alive():
        recovery_process.terminate()
        recovery_process.join(5)
        pytest.fail("startup recovery subprocess did not complete")
    assert recovery_process.exitcode == 0

    recovered = RunRepository(tmp_path).get(second_run.run_id)
    assert recovered.run_outcome is RunOutcome.FAILED
    assert recovered.error_code is RunErrorCode.INTERRUPTED
    assert recovered.candidate_snapshot_id == orphan_manifest.snapshot_id
    assert recovered.published_snapshot_id is None
    assert RunRepository(tmp_path).list_publications("book-alpha") == (
        first.publication,
    )
    assert RunRepository(tmp_path).get_active("book-alpha") == prior_active

@pytest.mark.parametrize(
    ("failure_kind", "expected_code"),
    [
        ("serialization", RunErrorCode.SERIALIZATION_FAILED),
        ("disk", RunErrorCode.DISK_WRITE_FAILED),
    ],
)
def test_publisher_returns_tagged_durable_failure_without_catalog_visibility(
    tmp_path: Path,
    failure_kind: str,
    expected_code: RunErrorCode,
) -> None:
    # Break caught: pre-CAS contract/filesystem failures escaping without honest
    # terminal evidence or being fabricated into a publication result.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0M",
    )
    if failure_kind == "serialization":
        bad_output = candidate.outputs[0].model_copy(
            update={"payload": b'{"xray":"tampered"}'}
        )
        candidate = module.SnapshotCandidateV1(
            manifest_body=candidate.manifest_body,
            outputs=(bad_output,),
        )
        publisher_store = store
    else:
        injected = False

        def fail_write(stage: str, _path: Path) -> None:
            nonlocal injected
            if stage == "before_file_fsync" and not injected:
                injected = True
                raise OSError("injected filesystem fault")

        publisher_store = SnapshotStore(tmp_path, fault_injector=fail_write)

    result = module.SnapshotPublisher(
        repository=repository,
        store=publisher_store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is expected_code
    assert repository.get(run.run_id) == result.run
    assert result.run.candidate_snapshot_id is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize("value_error_source", ["clock", "publisher_hook"])
def test_publisher_does_not_misclassify_infrastructure_value_errors(
    tmp_path: Path,
    value_error_source: str,
) -> None:
    # Break caught: a broad ValueError catch turning clock or publisher-hook defects
    # into false SERIALIZATION_FAILED evidence.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0N",
    )

    def clock() -> datetime:
        if value_error_source == "clock":
            raise ValueError("injected publisher clock defect")
        return T3

    def fail_publisher_hook(stage) -> None:
        if (
            value_error_source == "publisher_hook"
            and stage is module.PublisherFaultStage.AFTER_MANIFEST_DURABLE
        ):
            raise ValueError("injected publisher hook defect")

    with pytest.raises(ValueError, match="injected"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=clock,
            fault_injector=fail_publisher_hook,
        ).publish(run.run_id, candidate, authority=authority)

    durable = RunRepository(tmp_path)
    assert durable.get(run.run_id).run_outcome is RunOutcome.RUNNING
    assert durable.get(run.run_id).candidate_snapshot_id is None
    assert durable.list_publications("book-alpha") == ()
    assert durable.get_active("book-alpha") is None


def test_publisher_failure_terminalization_retries_to_cancellation_precedence(
    tmp_path: Path,
) -> None:
    # Break caught: a cancellation CAS racing failure evidence losing to a generic
    # failure or leaving the run non-terminal after the first stale version.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0P",
    )
    race_armed = True

    def cancel_during_terminalization() -> datetime:
        nonlocal race_armed
        if race_armed:
            race_armed = False
            repository.request_cancel(run.run_id, now=T3)
        return T3

    bad_output = candidate.outputs[0].model_copy(
        update={"payload": b'{"xray":"tampered"}'}
    )
    forged = module.SnapshotCandidateV1(
        manifest_body=candidate.manifest_body,
        outputs=(bad_output,),
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=cancel_during_terminalization,
    ).publish(run.run_id, forged, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert RunRepository(tmp_path).get(run.run_id) == result.run


def test_publisher_acknowledges_cancellation_at_entry_without_writing_candidate(
    tmp_path: Path,
) -> None:
    # Break caught: already-durable cancellation intent still allowing worker output or
    # manifest bytes to be written before the publisher consults controller authority.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0R",
    )
    output_ref = candidate.outputs[0].artifact_ref()
    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_artifact(output_ref)
    repository.request_cancel(run.run_id, now=T3)

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_artifact(output_ref)
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_publisher_propagates_repository_error_while_recording_failure(
    tmp_path: Path,
) -> None:
    # Break caught: claiming a terminal failure result when durable failure evidence
    # could not be recorded because the catalog operation itself failed.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0Q",
    )
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_failure_evidence
            BEFORE UPDATE OF run_outcome ON snapshot_runs
            WHEN NEW.run_outcome = 'FAILED'
            BEGIN
                SELECT RAISE(ABORT, 'injected failure-evidence database fault');
            END
            """
        )
    bad_output = candidate.outputs[0].model_copy(
        update={"payload": b'{"xray":"tampered"}'}
    )
    forged = module.SnapshotCandidateV1(
        manifest_body=candidate.manifest_body,
        outputs=(bad_output,),
    )

    with pytest.raises(RunDatabaseError, match="database mutation failed"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
        ).publish(run.run_id, forged, authority=authority)

    durable = RunRepository(tmp_path)
    assert durable.get(run.run_id).run_outcome is RunOutcome.RUNNING
    assert durable.get(run.run_id).candidate_snapshot_id is None
    assert durable.list_publications("book-alpha") == ()


def test_corrupt_manifest_after_durable_write_records_disk_failure(
    tmp_path: Path,
) -> None:
    # Break caught: a corrupt canonical envelope leaking ManifestError and leaving the
    # run live instead of recording exact pre-CAS disk verification evidence.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B2A",
    )
    manifest = create_manifest(candidate.manifest_body)
    manifest_path = (
        tmp_path
        / "snapshots"
        / "manifests"
        / "analytical_snapshot_manifest_v1"
        / manifest.snapshot_id[:2]
        / f"{manifest.snapshot_id}.json"
    )

    def corrupt_after_durable(stage) -> None:
        if stage is module.PublisherFaultStage.AFTER_MANIFEST_DURABLE:
            manifest_path.write_bytes(b"corrupt canonical manifest")

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=corrupt_after_durable,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.error_code is RunErrorCode.DISK_WRITE_FAILED
    assert repository.get(run.run_id) == result.run
    assert result.run.candidate_snapshot_id is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize(
    "terminal_race",
    [
        "cancelled_before_reload",
        "failed_before_reload",
        "cancelled_during_attach",
        "identical_success_during_attach",
    ],
)
def test_candidate_attachment_converges_terminal_race(
    tmp_path: Path,
    terminal_race: str,
) -> None:
    # Break caught: exact cancellation acknowledgement or an identical concurrent
    # publication winning between reload and attachment escaping as terminal mutation.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B2B",
    )
    raced = False

    def terminalize_before_attachment(stage) -> None:
        nonlocal raced
        before_reload = terminal_race.endswith("before_reload")
        expected_stage = (
            module.PublisherFaultStage.AFTER_SNAPSHOT_VERIFIED
            if before_reload
            else module.PublisherFaultStage.BEFORE_CANDIDATE_ATTACH
        )
        if stage is not expected_stage or raced:
            return
        raced = True
        if terminal_race.startswith("cancelled"):
            cancel = repository.request_cancel(run.run_id, now=T3)
            repository.acknowledge_cancel(
                run.run_id,
                expected_version=cancel.version,
                now=T3,
            )
        elif terminal_race.startswith("failed"):
            current = repository.get(run.run_id)
            repository.mark_failed(
                run.run_id,
                RunFailureV1(code=RunErrorCode.WORKER_FAILED),
                expected_version=current.version,
                now=T3,
            )
        else:
            module.SnapshotPublisher(
                repository=repository,
                store=store,
                clock=lambda: T3,
            ).publish(run.run_id, candidate, authority=authority)

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=terminalize_before_attachment,
    ).publish(run.run_id, candidate, authority=authority)

    assert raced is True
    assert repository.get(run.run_id) == result.run
    if not terminal_race.startswith("identical_success"):
        assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
        assert result.publication_result is None
        if terminal_race.startswith("cancelled"):
            assert result.run.run_outcome is RunOutcome.CANCELLED
            assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
        else:
            assert result.run.run_outcome is RunOutcome.FAILED
            assert result.run.error_code is RunErrorCode.WORKER_FAILED
        assert repository.list_publications("book-alpha") == ()
        assert repository.get_active("book-alpha") is None
    else:
        assert result.result_code is module.PublisherResultCode.CATALOG_RESULT
        assert result.publication_result is not None
        assert result.run.run_outcome is RunOutcome.SUCCEEDED
        assert result.published is True
        assert result.already_published is True
        assert len(repository.list_publications("book-alpha")) == 1
        assert repository.get_active("book-alpha").pointer_version == 1


def test_candidate_attachment_race_refuses_conflicting_success(
    tmp_path: Path,
) -> None:
    # Break caught: convergence accepting any concurrent success instead of requiring
    # exact candidate and published snapshot identity.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B2F",
    )
    competing_payload = b'{"xray":"competing"}'
    competing_ref = _artifact_ref(
        competing_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    competing_body = candidate.manifest_body.model_copy(
        update={
            "outputs": (
                candidate.manifest_body.outputs[0].model_copy(
                    update={"object_ref": competing_ref}
                ),
            )
        }
    )
    competing_candidate = module.SnapshotCandidateV1(
        manifest_body=competing_body,
        outputs=(
            candidate.outputs[0].model_copy(update={"payload": competing_payload}),
        ),
    )
    raced = False

    def publish_competing_candidate(stage) -> None:
        nonlocal raced
        if stage is module.PublisherFaultStage.BEFORE_CANDIDATE_ATTACH and not raced:
            raced = True
            module.SnapshotPublisher(
                repository=repository,
                store=store,
                clock=lambda: T3,
            ).publish(run.run_id, competing_candidate, authority=authority)

    with pytest.raises(TerminalRunMutationError):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
            fault_injector=publish_competing_candidate,
        ).publish(run.run_id, candidate, authority=authority)

    competing_snapshot_id = create_manifest(competing_body).snapshot_id
    durable = repository.get(run.run_id)
    assert raced is True
    assert durable.run_outcome is RunOutcome.SUCCEEDED
    assert durable.candidate_snapshot_id == competing_snapshot_id
    assert durable.published_snapshot_id == competing_snapshot_id
    assert repository.get_active("book-alpha").snapshot_id == competing_snapshot_id
    assert len(repository.list_publications("book-alpha")) == 1


@pytest.mark.parametrize("terminal_winner", ["cancelled", "failed"])
def test_failure_terminalization_converges_concurrent_terminal_truth(
    tmp_path: Path,
    terminal_winner: str,
) -> None:
    # Break caught: durable terminal truth winning after failure's read but before
    # mark_failed escaping as TerminalRunMutationError instead of being returned exactly.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B2C",
    )
    armed = True

    def terminalize_during_publisher_clock() -> datetime:
        nonlocal armed
        if armed:
            armed = False
            durable = repository.get(run.run_id)
            if terminal_winner == "cancelled":
                cancel = repository.request_cancel(run.run_id, now=T3)
                repository.acknowledge_cancel(
                    run.run_id,
                    expected_version=cancel.version,
                    now=T3,
                )
            else:
                repository.mark_failed(
                    run.run_id,
                    RunFailureV1(code=RunErrorCode.WORKER_FAILED),
                    expected_version=durable.version,
                    now=T3,
                )
        return T3

    bad_output = candidate.outputs[0].model_copy(
        update={"payload": b'{"xray":"tampered"}'}
    )
    forged = module.SnapshotCandidateV1(
        manifest_body=candidate.manifest_body,
        outputs=(bad_output,),
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=terminalize_during_publisher_clock,
    ).publish(run.run_id, forged, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    if terminal_winner == "cancelled":
        assert result.run.run_outcome is RunOutcome.CANCELLED
        assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    else:
        assert result.run.run_outcome is RunOutcome.FAILED
        assert result.run.error_code is RunErrorCode.WORKER_FAILED
    assert RunRepository(tmp_path).get(run.run_id) == result.run
    assert RunRepository(tmp_path).list_publications("book-alpha") == ()


def test_stale_attachment_reread_converges_acknowledged_cancellation(
    tmp_path: Path,
) -> None:
    # Break caught: stale attachment CAS rereading terminal cancellation and then
    # falling through to commit, which leaks a terminal-mutation exception.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B2D",
    )
    armed = True

    def cancel_before_attachment(stage) -> None:
        nonlocal armed
        if stage is module.PublisherFaultStage.BEFORE_CANDIDATE_ATTACH and armed:
            armed = False
            cancel = repository.request_cancel(run.run_id, now=T3)
            repository.acknowledge_cancel(
                run.run_id,
                expected_version=cancel.version,
                now=T3,
            )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=cancel_before_attachment,
    ).publish(run.run_id, candidate, authority=authority)

    assert armed is False
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert RunRepository(tmp_path).get(run.run_id) == result.run
    assert RunRepository(tmp_path).list_publications("book-alpha") == ()
    assert RunRepository(tmp_path).get_active("book-alpha") is None


def test_precommit_acknowledged_cancellation_returns_terminal_truth(
    tmp_path: Path,
) -> None:
    # Break caught: cancellation acknowledged after candidate attachment but before the
    # final catalog CAS escaping instead of returning the exact durable terminal run.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B2E",
    )
    raced = False

    def acknowledge_before_commit(stage) -> None:
        nonlocal raced
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT and not raced:
            raced = True
            cancel = repository.request_cancel(run.run_id, now=T3)
            repository.acknowledge_cancel(
                run.run_id,
                expected_version=cancel.version,
                now=T3,
            )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=acknowledge_before_commit,
    ).publish(run.run_id, candidate, authority=authority)

    assert raced is True
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.candidate_snapshot_id == create_manifest(
        candidate.manifest_body
    ).snapshot_id
    assert repository.get(run.run_id) == result.run
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_failure_terminalization_converges_attachment_then_cancellation_stales(
    tmp_path: Path,
) -> None:
    # Break caught: a fixed retry budget exhausting after two distinct legal
    # PUBLISHING mutations and leaking stale-version uncertainty instead of cancellation.
    race_steps = 0
    candidate_snapshot_id: str | None = None
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3B",
    )
    candidate_snapshot_id = create_manifest(candidate.manifest_body).snapshot_id

    def mutate_during_terminalization() -> datetime:
        nonlocal race_steps
        durable = repository.get(run.run_id)
        if race_steps == 0:
            assert candidate_snapshot_id is not None
            repository.attach_candidate(
                run.run_id,
                candidate_snapshot_id,
                expected_version=durable.version,
                now=T3,
            )
            race_steps += 1
        elif race_steps == 1:
            repository.request_cancel(run.run_id, now=T3)
            race_steps += 1
        return T3

    forged = module.SnapshotCandidateV1(
        manifest_body=candidate.manifest_body,
        outputs=(
            candidate.outputs[0].model_copy(
                update={"payload": b'{"xray":"tampered"}'}
            ),
        ),
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=mutate_during_terminalization,
    ).publish(run.run_id, forged, authority=authority)

    assert race_steps == 2
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run.candidate_snapshot_id == candidate_snapshot_id
    assert RunRepository(tmp_path).get(run.run_id) == result.run


@pytest.mark.parametrize("newer_durable_state", ["cancellation", "candidate"])
def test_terminalization_floors_a_stale_publisher_clock(
    tmp_path: Path,
    newer_durable_state: str,
) -> None:
    # Break caught: a controller clock behind newer durable failure/cancellation state
    # making terminalization violate the repository's monotonic lifecycle-time invariant.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3C",
    )
    if newer_durable_state == "cancellation":
        advanced = repository.request_cancel(run.run_id, now=T4)
        published_candidate = candidate
        expected_outcome = RunOutcome.CANCELLED
        expected_code = RunErrorCode.CANCELLED_BY_USER
    else:
        advanced = repository.attach_candidate(
            run.run_id,
            create_manifest(candidate.manifest_body).snapshot_id,
            expected_version=run.version,
            now=T4,
        )
        published_candidate = module.SnapshotCandidateV1(
            manifest_body=candidate.manifest_body,
            outputs=(
                candidate.outputs[0].model_copy(
                    update={"payload": b'{"xray":"tampered"}'}
                ),
            ),
        )
        expected_outcome = RunOutcome.FAILED
        expected_code = RunErrorCode.SERIALIZATION_FAILED

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, published_candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert result.run.run_outcome is expected_outcome
    assert result.run.error_code is expected_code
    assert result.run.finished_at_utc == advanced.updated_at_utc
    assert result.run.updated_at_utc == advanced.updated_at_utc
    assert repository.get(run.run_id) == result.run


def test_terminal_cancel_rejection_response_loss_allows_exact_verified_retry(
    tmp_path: Path,
) -> None:
    # Break caught: a catalog rejection committed before publisher response loss becoming
    # impossible to recover even though the exact attached candidate remains verifiable.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3D",
    )

    def cancel_then_lose_response(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            repository.request_cancel(run.run_id, now=T3)
        elif stage is module.PublisherFaultStage.AFTER_REPOSITORY_COMMIT:
            raise OSError("lost terminal catalog response")

    with pytest.raises(OSError, match="lost terminal catalog response"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
            fault_injector=cancel_then_lose_response,
        ).publish(run.run_id, candidate, authority=authority)

    durable = repository.get(run.run_id)
    assert durable.run_outcome is RunOutcome.CANCELLED
    assert durable.error_code is RunErrorCode.CANCELLED_BY_USER
    assert durable.candidate_snapshot_id == create_manifest(
        candidate.manifest_body
    ).snapshot_id
    assert durable.published_snapshot_id is None

    retried = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert retried.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert retried.publication_result is not None
    assert retried.run == durable
    assert retried.publication_result.run == durable
    assert retried.publication_result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert retried.published is False
    assert retried.publication is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_terminal_generation_rejection_response_loss_allows_exact_verified_retry(
    tmp_path: Path,
) -> None:
    # Break caught: retry eligibility being limited to cancellation even though stale
    # generation rejection is an equally durable catalog decision after response loss.
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3E",
    )
    next_book_ref = store.put_bytes(
        b'{"schema_version":"canonical_book_v1","generation":2}',
        media_type="application/json",
        schema_version="canonical_book_v1",
    )

    def advance_generation_then_lose_response(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            repository.advance_book_head(
                "book-alpha",
                2,
                next_book_ref.digest,
                now=T3,
            )
        elif stage is module.PublisherFaultStage.AFTER_REPOSITORY_COMMIT:
            raise OSError("lost terminal catalog response")

    with pytest.raises(OSError, match="lost terminal catalog response"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
            fault_injector=advance_generation_then_lose_response,
        ).publish(run.run_id, candidate, authority=authority)

    durable = repository.get(run.run_id)
    assert durable.run_outcome is RunOutcome.FAILED
    assert durable.error_code is RunErrorCode.STALE_BOOK_GENERATION
    assert durable.candidate_snapshot_id == create_manifest(
        candidate.manifest_body
    ).snapshot_id
    assert durable.published_snapshot_id is None

    retried = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert retried.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert retried.publication_result is not None
    assert retried.run == durable
    assert retried.publication_result.rejection_code is RunErrorCode.STALE_BOOK_GENERATION
    assert retried.published is False
    assert retried.publication is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_terminal_pointer_rejection_response_loss_allows_exact_verified_retry(
    tmp_path: Path,
) -> None:
    # Break caught: a stale-pointer catalog rejection being unrecoverable after response
    # loss despite exact candidate attachment, immutable bytes, and controller authority.
    module, repository, store, first_run, authority, first_candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3F",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(first_run.run_id, first_candidate, authority=authority)
    assert first.published is True

    replacement_payload = b'{"xray":"replacement"}'
    replacement_ref = _artifact_ref(
        replacement_payload,
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    replacement_body = first_candidate.manifest_body.model_copy(
        update={
            "outputs": (
                first_candidate.manifest_body.outputs[0].model_copy(
                    update={"object_ref": replacement_ref}
                ),
            )
        }
    )
    candidate = module.SnapshotCandidateV1(
        manifest_body=replacement_body,
        outputs=(
            first_candidate.outputs[0].model_copy(
                update={"payload": replacement_payload}
            ),
        ),
    )
    run = _publishing_run(
        repository,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3G",
        requested_at=T3,
        stage_at=T3,
    )

    def reject_active(_snapshot_id: str):
        raise ValueError("active failed recovery verification")

    def move_pointer_then_lose_response(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            repository.recover_active(
                "book-alpha",
                verify=reject_active,
                now=T4,
            )
        elif stage is module.PublisherFaultStage.AFTER_REPOSITORY_COMMIT:
            raise OSError("lost terminal catalog response")

    with pytest.raises(OSError, match="lost terminal catalog response"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T4,
            fault_injector=move_pointer_then_lose_response,
        ).publish(run.run_id, candidate, authority=authority)

    durable = repository.get(run.run_id)
    assert durable.run_outcome is RunOutcome.FAILED
    assert durable.error_code is RunErrorCode.STALE_ACTIVE_POINTER
    assert durable.candidate_snapshot_id == create_manifest(
        candidate.manifest_body
    ).snapshot_id
    assert durable.published_snapshot_id is None

    retried = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T4,
    ).publish(run.run_id, candidate, authority=authority)

    assert retried.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert retried.publication_result is not None
    assert retried.run == durable
    assert retried.publication_result.rejection_code is RunErrorCode.STALE_ACTIVE_POINTER
    assert retried.published is False
    assert retried.publication is None
    assert repository.list_publications("book-alpha") == (first.publication,)
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize(
    "refusal_case",
    [
        "worker_failed",
        "disk_write_failed",
        "interrupted",
        "cancelled_without_candidate",
        "mismatched_candidate",
        "missing_manifest",
        "tampered_artifact",
    ],
)
def test_terminal_retry_refuses_ineligible_or_unverifiable_history(
    tmp_path: Path,
    refusal_case: str,
) -> None:
    # Break caught: widening response-loss recovery into a generic terminal-run replay,
    # or trusting candidate identity without re-verifying its immutable stored bytes.
    run_id = "run_01J5X5S8J5J8P7KQ4Y0T3T3B3H"
    generic_codes = {
        "worker_failed": RunErrorCode.WORKER_FAILED,
        "disk_write_failed": RunErrorCode.DISK_WRITE_FAILED,
        "interrupted": RunErrorCode.INTERRUPTED,
    }
    if refusal_case in generic_codes or refusal_case == "cancelled_without_candidate":
        module, repository, store, run, authority, candidate = _publisher_case(
            tmp_path,
            run_id=run_id,
        )
        if refusal_case in generic_codes:
            attached = repository.attach_candidate(
                run.run_id,
                create_manifest(candidate.manifest_body).snapshot_id,
                expected_version=run.version,
                now=T3,
            )
            repository.mark_failed(
                run.run_id,
                RunFailureV1(code=generic_codes[refusal_case]),
                expected_version=attached.version,
                now=T3,
            )
        else:
            requested = repository.request_cancel(run.run_id, now=T3)
            repository.acknowledge_cancel(
                run.run_id,
                expected_version=requested.version,
                now=T3,
            )
        expected_error = TerminalRunMutationError
    else:
        module, repository, store, run, authority, candidate = (
            _cancelled_publication_rejection_case(tmp_path, run_id=run_id)
        )
        if refusal_case == "mismatched_candidate":
            replacement_payload = b'{"xray":"different-terminal-retry"}'
            replacement_ref = _artifact_ref(
                replacement_payload,
                media_type="application/json",
                schema_version="xray_read_model_v1",
            )
            candidate = module.SnapshotCandidateV1(
                manifest_body=candidate.manifest_body.model_copy(
                    update={
                        "outputs": (
                            candidate.manifest_body.outputs[0].model_copy(
                                update={"object_ref": replacement_ref}
                            ),
                        )
                    }
                ),
                outputs=(
                    candidate.outputs[0].model_copy(
                        update={"payload": replacement_payload}
                    ),
                ),
            )
            expected_error = TerminalRunMutationError
        elif refusal_case == "missing_manifest":
            snapshot_id = create_manifest(candidate.manifest_body).snapshot_id
            manifest_path = (
                tmp_path
                / "snapshots"
                / "manifests"
                / "analytical_snapshot_manifest_v1"
                / snapshot_id[:2]
                / f"{snapshot_id}.json"
            )
            manifest_path.unlink()
            expected_error = SnapshotStoreError
        else:
            output_ref = candidate.outputs[0].artifact_ref()
            artifact_path = (
                tmp_path
                / "snapshots"
                / "objects"
                / "sha256"
                / output_ref.digest[:2]
                / output_ref.digest
            )
            artifact_path.write_bytes(b"tampered immutable bytes")
            expected_error = SnapshotStoreError

    durable_before = repository.get(run.run_id)
    publications_before = repository.list_publications("book-alpha")
    active_before = repository.get_active("book-alpha")
    with pytest.raises(expected_error):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T4,
        ).publish(run.run_id, candidate, authority=authority)

    assert repository.get(run.run_id) == durable_before
    assert repository.list_publications("book-alpha") == publications_before
    assert repository.get_active("book-alpha") == active_before


@pytest.mark.skip(reason="T3 pipeline remains intentionally unshipped")
def test_pipeline_happy_path_runs_exact_stage_order_and_publishes_blessed(
    tmp_path: Path,
) -> None:
    # Break caught: an orchestration path skipping durable stage transitions, losing
    # controller authority, or publishing before all frozen stage results are accepted.
    (
        module,
        repository,
        _store,
        run,
        request,
        candidate,
        publisher,
    ) = _queued_pipeline_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3A",
    )
    observed_stages: list[RunStage] = []

    def recording_runner(stage, function, frozen_input):
        observed_stages.append(stage)
        return function(frozen_input)

    result = module.SnapshotPipeline(
        repository=repository,
        publisher=publisher,
        clock=lambda: T3,
        stage_runner=recording_runner,
        ingest_stage=_pipeline_ingest,
        reconcile_stage=_pipeline_reconcile,
        validate_stage=_pipeline_validate,
        model_stage=_pipeline_model,
    ).run(request)

    assert observed_stages == [
        RunStage.INGESTING,
        RunStage.RECONCILING,
        RunStage.VALIDATING,
        RunStage.MODELING,
    ]
    assert result.result_code is module.PipelineResultCode.PUBLICATION_RESULT
    assert result.publication_result is not None
    assert result.run == result.publication_result.run
    assert result.run.run_outcome is RunOutcome.SUCCEEDED
    assert result.publication_result.published is True
    assert result.publication_result.publication is not None
    assert result.publication_result.publication.snapshot_status is SnapshotStatus.BLESSED
    assert result.run.candidate_snapshot_id == create_manifest(
        candidate.manifest_body
    ).snapshot_id
    assert repository.get(run.run_id) == result.run
    assert (
        repository.get_active("book-alpha").snapshot_id
        == result.run.published_snapshot_id
    )


@pytest.mark.skip(reason="T3 pipeline remains intentionally unshipped")
def test_pipeline_hard_validation_failure_stops_before_modeling_and_publication(
    tmp_path: Path,
) -> None:
    # Break caught: a hard validation gate reaching modeling/publication or leaving
    # the durable run live without exact HARD_GATE_FAILED evidence.
    (
        module,
        repository,
        _store,
        run,
        request,
        _candidate,
        publisher,
    ) = _queued_pipeline_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B3B",
    )
    model_calls = 0
    publish_calls = 0

    def hard_fail_validation(payload):
        failed_gate = GateEvidenceV1(
            gate_code="BOOK_RECONCILIATION",
            status=GateStatus.FAILED,
            recovery_class=RecoveryClass.USER_RESOLVABLE,
            evidence=("book mismatch remains",),
            recovery_action="Resolve the book mismatch",
        )
        return module.ValidationResultV1(
            decision=module.ValidationDecision.HARD_FAIL,
            validated_payload=_payload_for_stage(payload, RunStage.VALIDATING),
            gates=(failed_gate,),
            policy_evidence=(),
            warnings=(),
            refused_outputs=(),
        )

    def forbidden_model(_validation):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("hard validation must not invoke modeling")

    original_publish = publisher.publish

    def forbidden_publish(*args, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        return original_publish(*args, **kwargs)

    publisher.publish = forbidden_publish

    result = module.SnapshotPipeline(
        repository=repository,
        publisher=publisher,
        clock=lambda: T3,
        stage_runner=lambda _stage, function, frozen_input: function(frozen_input),
        ingest_stage=_pipeline_ingest,
        reconcile_stage=_pipeline_reconcile,
        validate_stage=hard_fail_validation,
        model_stage=forbidden_model,
    ).run(request)

    assert result.result_code is module.PipelineResultCode.TERMINAL_RUN
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.FAILED
    assert result.run.run_stage is RunStage.VALIDATING
    assert result.run.error_code is RunErrorCode.HARD_GATE_FAILED
    assert repository.get(run.run_id) == result.run
    assert model_calls == 0
    assert publish_calls == 0
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


@pytest.mark.parametrize(
    ("cancel_checkpoint", "expected_stage", "expected_calls"),
    [
        ("entry", RunStage.QUEUED, ()),
        ("ingesting", RunStage.INGESTING, (RunStage.INGESTING,)),
        (
            "reconciling",
            RunStage.RECONCILING,
            (RunStage.INGESTING, RunStage.RECONCILING),
        ),
        (
            "validating",
            RunStage.VALIDATING,
            (RunStage.INGESTING, RunStage.RECONCILING, RunStage.VALIDATING),
        ),
        (
            "modeling",
            RunStage.MODELING,
            (
                RunStage.INGESTING,
                RunStage.RECONCILING,
                RunStage.VALIDATING,
                RunStage.MODELING,
            ),
        ),
        (
            "publishing",
            RunStage.PUBLISHING,
            (
                RunStage.INGESTING,
                RunStage.RECONCILING,
                RunStage.VALIDATING,
                RunStage.MODELING,
            ),
        ),
    ],
)
@pytest.mark.skip(reason="T3 pipeline remains intentionally unshipped")
def test_pipeline_acknowledges_cancellation_at_every_controller_checkpoint(
    tmp_path: Path,
    cancel_checkpoint: str,
    expected_stage: RunStage,
    expected_calls: tuple[RunStage, ...],
) -> None:
    # Break caught: durable cancellation intent allowing the next worker stage or
    # publisher call to start, or being mislabeled as an ordinary failure.
    run_id = f"run_01J5X5S8J5J8P7KQ4Y0T3T3B4{len(expected_calls)}"

    class PublishingCancelRepository(RunRepository):
        def advance_stage(self, current_run_id, stage, *, expected_version, now):
            advanced = super().advance_stage(
                current_run_id,
                stage,
                expected_version=expected_version,
                now=now,
            )
            if cancel_checkpoint == "publishing" and stage is RunStage.PUBLISHING:
                self.request_cancel(current_run_id, now=now)
            return advanced

    repository = PublishingCancelRepository(tmp_path)
    (
        module,
        repository,
        _store,
        run,
        request,
        _candidate,
        publisher,
    ) = _queued_pipeline_case(
        tmp_path,
        run_id=run_id,
        repository=repository,
    )
    observed: list[RunStage] = []
    publish_calls = 0

    def cancelling_runner(stage, function, frozen_input):
        observed.append(stage)
        value = function(frozen_input)
        if cancel_checkpoint == stage.value.lower():
            repository.request_cancel(run.run_id, now=T3)
        return value

    original_publish = publisher.publish

    def recording_publish(*args, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        return original_publish(*args, **kwargs)

    publisher.publish = recording_publish
    if cancel_checkpoint == "entry":
        repository.request_cancel(run.run_id, now=T3)

    result = module.SnapshotPipeline(
        repository=repository,
        publisher=publisher,
        clock=lambda: T3,
        stage_runner=cancelling_runner,
        ingest_stage=_pipeline_ingest,
        reconcile_stage=_pipeline_reconcile,
        validate_stage=_pipeline_validate,
        model_stage=_pipeline_model,
    ).run(request)

    assert tuple(observed) == expected_calls
    assert publish_calls == 0
    assert result.result_code is module.PipelineResultCode.TERMINAL_RUN
    assert result.publication_result is None
    assert result.run.run_outcome is RunOutcome.CANCELLED
    assert result.run.run_stage is expected_stage
    assert result.run.error_code is RunErrorCode.CANCELLED_BY_USER
    assert repository.get(run.run_id) == result.run
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


def test_publisher_uses_exact_catalog_head_instead_of_adapter_override(
    tmp_path: Path,
) -> None:
    # Break caught: a same-generation durable canonical head B being hidden by an
    # adapter that reports head A, allowing an A-bound manifest into the B catalog.
    class ForgedHeadRepository(RunRepository):
        reported_head = None

        def get_book_head(self, book_id):
            if self.reported_head is not None:
                return self.reported_head
            return super().get_book_head(book_id)

    repository = ForgedHeadRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0A",
        repository=repository,
    )
    repository.reported_head = RunRepository.get_book_head(
        repository,
        "book-alpha",
    )
    assert repository.reported_head is not None
    durable_ref = "f" * 64
    assert durable_ref != authority.canonical_book_ref.digest
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            """
            UPDATE book_heads
            SET canonical_book_ref = ?
            WHERE book_id = 'book-alpha' AND generation = 1
            """,
            (durable_ref,),
        )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    durable = RunRepository(tmp_path)
    assert durable.get_book_head("book-alpha").canonical_book_ref == durable_ref
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.run.error_code is RunErrorCode.SERIALIZATION_FAILED
    assert durable.list_publications("book-alpha") == ()
    assert durable.get_active("book-alpha") is None


@pytest.mark.parametrize("terminal_method", ["mark_failed", "acknowledge_cancel"])
def test_publisher_returns_only_persisted_terminal_adapter_evidence(
    tmp_path: Path,
    terminal_method: str,
) -> None:
    # Break caught: an adapter returning a coherent terminal record without persisting
    # it, while the publisher reports that invented record as durable truth.
    class FalseTerminalRepository(RunRepository):
        def mark_failed(self, run_id, failure, *, expected_version, now):
            if terminal_method != "mark_failed":
                return super().mark_failed(
                    run_id,
                    failure,
                    expected_version=expected_version,
                    now=now,
                )
            current = RunRepository.get(self, run_id)
            return type(current).model_validate(
                current.model_copy(
                    update={
                        "run_outcome": RunOutcome.FAILED,
                        "finished_at_utc": now,
                        "updated_at_utc": now,
                        "error_code": failure.code,
                        "error_message": "result serialization failed",
                        "version": current.version + 1,
                    }
                ).model_dump(mode="python", warnings=False)
            )

        def acknowledge_cancel(self, run_id, *, expected_version, now):
            if terminal_method != "acknowledge_cancel":
                return super().acknowledge_cancel(
                    run_id,
                    expected_version=expected_version,
                    now=now,
                )
            current = RunRepository.get(self, run_id)
            return type(current).model_validate(
                current.model_copy(
                    update={
                        "run_outcome": RunOutcome.CANCELLED,
                        "finished_at_utc": now,
                        "updated_at_utc": now,
                        "error_code": RunErrorCode.CANCELLED_BY_USER,
                        "error_message": "cancelled by user",
                        "version": current.version + 1,
                    }
                ).model_dump(mode="python", warnings=False)
            )

    repository = FalseTerminalRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id=(
            "run_01J5X5S8J5J8P7KQ4Y0T3T3C0B"
            if terminal_method == "mark_failed"
            else "run_01J5X5S8J5J8P7KQ4Y0T3T3C0C"
        ),
        repository=repository,
    )
    if terminal_method == "acknowledge_cancel":
        repository.request_cancel(run.run_id, now=T3)
    else:
        candidate = module.SnapshotCandidateV1(
            manifest_body=candidate.manifest_body,
            outputs=(
                candidate.outputs[0].model_copy(
                    update={"payload": b'{"xray":"tampered"}'}
                ),
            ),
        )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    durable = RunRepository(tmp_path).get(run.run_id)
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert durable == result.run
    expected_terminal = {
        "mark_failed": (
            RunOutcome.FAILED,
            RunErrorCode.SERIALIZATION_FAILED,
        ),
        "acknowledge_cancel": (
            RunOutcome.CANCELLED,
            RunErrorCode.CANCELLED_BY_USER,
        ),
    }[terminal_method]
    assert (durable.run_outcome, durable.error_code) == expected_terminal


def test_publisher_rejects_repository_store_root_mismatch(tmp_path: Path) -> None:
    # Break caught: committing a catalog row whose manifest path names bytes that were
    # written under a different ordinary store root.
    module = importlib.import_module("quantmind.snapshots.publisher")
    catalog_root = tmp_path / "catalog"
    store_root = tmp_path / "store"
    repository = RunRepository(catalog_root)
    repository.initialize()

    with pytest.raises(ValueError, match="root"):
        module.SnapshotPublisher(
            repository=repository,
            store=SnapshotStore(store_root),
            clock=lambda: T3,
        )


def test_publisher_rejects_repository_database_outside_construction_root(
    tmp_path: Path,
) -> None:
    # Protective boundary: a mutable adapter view cannot redirect the exact catalog
    # clone away from the base repository's normalized construction root.
    module = importlib.import_module("quantmind.snapshots.publisher")
    repository = RunRepository(tmp_path / "catalog")
    repository.database_path = tmp_path / "other" / "runs.sqlite3"

    with pytest.raises(ValueError, match="database path"):
        module.SnapshotPublisher(
            repository=repository,
            store=SnapshotStore(tmp_path / "catalog"),
            clock=lambda: T3,
        )


@pytest.mark.parametrize("alias_kind", ["parent_segment", "symlink"])
def test_publisher_accepts_canonically_equal_repository_and_store_roots(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    # Protective boundary: spelling the same catalog root through ``..`` or a
    # symlink must not look like a split repository/store configuration.
    module = importlib.import_module("quantmind.snapshots.publisher")
    catalog_root = tmp_path / "catalog"
    catalog_root.mkdir()
    if alias_kind == "parent_segment":
        nested = catalog_root / "nested"
        nested.mkdir()
        store_root = nested / ".."
    else:
        store_root = tmp_path / "catalog-alias"
        store_root.symlink_to(catalog_root, target_is_directory=True)

    publisher = module.SnapshotPublisher(
        repository=RunRepository(catalog_root),
        store=SnapshotStore(store_root),
        clock=lambda: T3,
    )

    assert publisher is not None


def test_publisher_constructor_does_not_create_catalog_storage(tmp_path: Path) -> None:
    # Protective boundary: trusted collaborators are configured at construction,
    # but opening/initializing their filesystem remains a publish-time concern.
    module = importlib.import_module("quantmind.snapshots.publisher")
    catalog_root = tmp_path / "not-created"
    aliased_root = catalog_root / "missing-child" / ".."

    publisher = module.SnapshotPublisher(
        repository=RunRepository(aliased_root),
        store=SnapshotStore(catalog_root),
        clock=lambda: T3,
    )

    assert publisher is not None
    assert not catalog_root.exists()


def test_publisher_preserves_repository_and_store_construction_fault_hooks(
    tmp_path: Path,
) -> None:
    # Protective boundary: rebuilding exact collaborators must preserve the explicit
    # construction-time observability/fault seams used by crash-consistency tests.
    repository_stages: list[str] = []
    store_stages: list[str] = []
    repository = RunRepository(
        tmp_path,
        fault_injector=repository_stages.append,
    )
    module, repository, _store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0F",
        repository=repository,
    )
    repository_stages.clear()
    store = SnapshotStore(
        tmp_path,
        fault_injector=lambda stage, _path: store_stages.append(stage),
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.published is True
    assert "db.before_begin" in repository_stages
    assert "db.after_manifest_insert" in repository_stages
    assert "before_file_fsync" in store_stages


def test_publisher_uses_exact_store_and_reopens_every_published_snapshot(
    tmp_path: Path,
) -> None:
    # Break caught: a same-root store subclass fabricating valid return contracts while
    # writing no output or manifest bytes, followed by a catalog success.
    class NoWriteStore(SnapshotStore):
        def put_bytes(self, payload, *, media_type, schema_version):
            return _artifact_ref(
                payload,
                media_type=media_type,
                schema_version=schema_version,
            )

        def put_verified_manifest(self, manifest):
            payload = canonical_json_bytes(manifest)
            return StoredManifestV1(
                snapshot_id=manifest.snapshot_id,
                manifest_relpath=(
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{manifest.snapshot_id[:2]}/{manifest.snapshot_id}.json"
                ),
                envelope_sha256=hashlib.sha256(payload).hexdigest(),
                envelope_byte_length=len(payload),
                status=manifest.body.snapshot_status,
                manifest=manifest,
            )

        def verify_snapshot(self, snapshot_id, *, required_output_roles=()):
            del required_output_roles
            manifest = create_manifest(candidate.manifest_body)
            assert snapshot_id == manifest.snapshot_id
            return VerifiedSnapshotV1(
                snapshot_id=snapshot_id,
                status=manifest.body.snapshot_status,
                manifest=manifest,
            )

    module, repository, _store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0D",
    )
    result = module.SnapshotPublisher(
        repository=repository,
        store=NoWriteStore(tmp_path),
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.published is True
    assert result.publication is not None
    reopened = SnapshotStore(tmp_path).verify_snapshot(
        result.publication.snapshot_id
    )
    assert reopened.manifest == create_manifest(candidate.manifest_body)


def test_publisher_uses_repository_construction_root_not_overrideable_view(
    tmp_path: Path,
) -> None:
    # Break caught: a repository constructed against catalog A later reporting root B,
    # causing the publisher to commit and return B while lifecycle reads came from A.
    class MisreportedRootRepository(RunRepository):
        def __init__(self, actual_root: Path, reported_root: Path) -> None:
            self._reported_root: Path | None = None
            super().__init__(actual_root)
            self._reported_root = reported_root

        @property
        def root(self) -> Path:
            if self._reported_root is None:
                return self.__dict__["_construction_root"]
            return self._reported_root

        @root.setter
        def root(self, value: Path) -> None:
            self.__dict__["_construction_root"] = Path(value)

    root_a = tmp_path / "catalog-a"
    root_b = tmp_path / "catalog-b"
    run_id = "run_01J5X5S8J5J8P7KQ4Y0T3T3C0E"
    _module, repository_b, _store_b, run_b, _authority_b, candidate_b = (
        _publisher_case(root_b, run_id=run_id)
    )
    snapshot_id = create_manifest(candidate_b.manifest_body).snapshot_id
    run_b = repository_b.attach_candidate(
        run_b.run_id,
        snapshot_id,
        expected_version=run_b.version,
        now=T2,
    )
    deceptive = MisreportedRootRepository(root_a, root_b)
    module, repository_a, store_a, run_a, authority_a, candidate_a = _publisher_case(
        root_a,
        run_id=run_id,
        repository=deceptive,
    )
    assert repository_a.database_path == root_a / "snapshots" / "runs.sqlite3"
    assert repository_a.root == root_b

    result = module.SnapshotPublisher(
        repository=repository_a,
        store=store_a,
        clock=lambda: T3,
    ).publish(run_a.run_id, candidate_a, authority=authority_a)

    durable_a = RunRepository(root_a).get(run_id)
    durable_b = RunRepository(root_b).get(run_id)
    assert result.published is True
    assert result.run == durable_a
    assert durable_a.run_outcome is RunOutcome.SUCCEEDED
    assert durable_b == run_b


@pytest.mark.parametrize("collaborator", ["repository", "store", "both"])
def test_publisher_captures_base_instance_dict_without_subclass_descriptor_dispatch(
    tmp_path: Path,
    collaborator: str,
) -> None:
    # Break caught: object.__getattribute__(instance, "__dict__") still invoking a
    # malicious subclass descriptor and forging the base constructor's exact state.
    forged_root = tmp_path / "forged-root"

    class ForgedDictRepository(RunRepository):
        @property
        def __dict__(self):
            return {
                "_configured_root": forged_root,
                "database_path": forged_root / "snapshots" / "runs.sqlite3",
                "_configured_fault_injector": None,
            }

    class ForgedDictStore(SnapshotStore):
        @property
        def __dict__(self):
            return {
                "_configured_root": forged_root,
                "_configured_fault_injector": None,
            }

    repository = (
        ForgedDictRepository(tmp_path)
        if collaborator in {"repository", "both"}
        else RunRepository(tmp_path)
    )
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3C0M",
        repository=repository,
    )
    if collaborator in {"store", "both"}:
        store = ForgedDictStore(tmp_path)

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.published is True
    assert RunRepository(tmp_path).get(run.run_id) == result.run
    assert not (forged_root / "snapshots" / "runs.sqlite3").exists()


@pytest.mark.parametrize(
    "forgery",
    ["metadata", "missing_active", "future_active", "already_published"],
)
def test_publisher_ignores_forged_commit_return_and_resolves_durable_truth(
    tmp_path: Path,
    forgery: str,
) -> None:
    class HostileCommitRepository(RunRepository):
        def commit_publication(self, *args, **kwargs):
            durable = super().commit_publication(*args, **kwargs)
            assert durable.publication is not None
            assert durable.active is not None
            if forgery == "metadata":
                forged_publication = durable.publication.model_copy(
                    update={
                        "publication_sequence": 999,
                        "snapshot_status": SnapshotStatus.DEGRADED,
                        "envelope_sha256": "f" * 64,
                        "envelope_byte_length": 1,
                    }
                )
                return durable.model_copy(update={"publication": forged_publication})
            if forgery == "missing_active":
                return durable.model_copy(update={"active": None})
            if forgery == "already_published":
                return durable.model_copy(update={"already_published": True})
            return durable.model_copy(
                update={
                    "active": durable.active.model_copy(
                        update={"pointer_version": 99, "updated_at_utc": T6}
                    )
                }
            )

    repository = HostileCommitRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5A",
        repository=repository,
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)
    durable = RunRepository(tmp_path)
    durable.initialize()
    truth = durable.resolve_publication_result(
        run.run_id, already_published=False
    )

    assert result.publication_result == truth
    assert result.publication == truth.publication
    assert result.active == truth.active


def test_concurrent_publishers_preserve_call_local_idempotency_flags(
    tmp_path: Path,
) -> None:
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5G",
    )
    barrier = threading.Barrier(2)
    results = []
    errors: list[BaseException] = []

    def synchronize_commits(stage) -> None:
        if stage is module.PublisherFaultStage.BEFORE_REPOSITORY_COMMIT:
            barrier.wait(timeout=10)

    def publish() -> None:
        try:
            results.append(
                module.SnapshotPublisher(
                    repository=repository,
                    store=store,
                    clock=lambda: T3,
                    fault_injector=synchronize_commits,
                ).publish(run.run_id, candidate, authority=authority)
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result.already_published for result in results) == [False, True]
    assert all(result.published for result in results)
    assert len(repository.list_publications("book-alpha")) == 1


@pytest.mark.parametrize(
    "forgery",
    ["foreign_run", "sequence", "missing_active", "future_active"],
)
def test_succeeded_retry_ignores_overridden_resolver_and_uses_trusted_truth(
    tmp_path: Path,
    forgery: str,
) -> None:
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5H",
    )
    first = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)
    assert first.publication is not None
    assert first.active is not None

    class HostileResolverRepository(RunRepository):
        def resolve_publication_result(self, run_id, *, already_published):
            durable = super().resolve_publication_result(
                run_id, already_published=already_published
            )
            assert durable.publication is not None
            assert durable.active is not None
            if forgery == "foreign_run":
                foreign_run_id = "run_01J5X5S8J5J8P7KQ4Y0T3T3BZZ"
                return durable.model_copy(
                    update={
                        "run": durable.run.model_copy(
                            update={"run_id": foreign_run_id}
                        ),
                        "publication": durable.publication.model_copy(
                            update={"run_id": foreign_run_id}
                        ),
                    }
                )
            if forgery == "sequence":
                return durable.model_copy(
                    update={
                        "publication": durable.publication.model_copy(
                            update={"publication_sequence": 999}
                        )
                    }
                )
            if forgery == "missing_active":
                return durable.model_copy(update={"active": None})
            return durable.model_copy(
                update={
                    "active": durable.active.model_copy(
                        update={"pointer_version": 99, "updated_at_utc": T6}
                    )
                }
            )

    hostile = HostileResolverRepository(tmp_path)
    hostile.initialize()
    retried = module.SnapshotPublisher(
        repository=hostile,
        store=store,
        clock=lambda: T4,
    ).publish(run.run_id, candidate, authority=authority)

    truth = RunRepository(tmp_path).resolve_publication_result(
        run.run_id,
        already_published=True,
    )
    assert retried.publication_result == truth


def test_publisher_ignores_hostile_resolver_manifest_projection(
    tmp_path: Path,
) -> None:
    class HostileResolverRepository(RunRepository):
        def resolve_publication_result(self, run_id, *, already_published):
            durable = super().resolve_publication_result(
                run_id, already_published=already_published
            )
            assert durable.publication is not None
            return durable.model_copy(
                update={
                    "publication": durable.publication.model_copy(
                        update={
                            "snapshot_status": SnapshotStatus.DEGRADED,
                            "envelope_sha256": "f" * 64,
                            "envelope_byte_length": 1,
                        }
                    )
                }
            )

    repository = HostileResolverRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5B",
        repository=repository,
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    truth = RunRepository(tmp_path).resolve_publication_result(
        run.run_id,
        already_published=False,
    )
    assert result.publication_result == truth


def test_publisher_ignores_instance_bound_resolver_shadow(tmp_path: Path) -> None:
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5J",
    )

    def forged_resolver(self, run_id, *, already_published):
        durable = RunRepository.resolve_publication_result(
            self,
            run_id,
            already_published=already_published,
        )
        assert durable.publication is not None
        return durable.model_copy(
            update={
                "publication": durable.publication.model_copy(
                    update={"publication_sequence": 999}
                )
            }
        )

    repository.resolve_publication_result = MethodType(  # type: ignore[method-assign]
        forged_resolver,
        repository,
    )
    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    truth = RunRepository(tmp_path).resolve_publication_result(
        run.run_id,
        already_published=False,
    )
    assert result.publication_result == truth


def test_publisher_ignores_protected_publication_decoder_override(
    tmp_path: Path,
) -> None:
    class HostileDecoderRepository(RunRepository):
        @staticmethod
        def _publication_from_row(row):
            durable = RunRepository._publication_from_row(row)
            return durable.model_copy(update={"publication_sequence": 999})

    repository = HostileDecoderRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5K",
        repository=repository,
    )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    truth = RunRepository(tmp_path).resolve_publication_result(
        run.run_id,
        already_published=False,
    )
    assert result.publication_result == truth


def test_later_pointer_advance_cannot_create_resolver_comparison_toctou(
    tmp_path: Path,
) -> None:
    class OldResultResolverRepository(RunRepository):
        old_result: PublicationResultV1 | None = None

        def resolve_publication_result(self, run_id, *, already_published):
            if self.old_result is not None:
                return self.old_result.model_copy(
                    update={"already_published": already_published}
                )
            return super().resolve_publication_result(
                run_id,
                already_published=already_published,
            )

    repository = OldResultResolverRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5L",
        repository=repository,
    )
    advanced = False

    def advance_after_commit(stage) -> None:
        nonlocal advanced
        if stage is not module.PublisherFaultStage.AFTER_REPOSITORY_COMMIT:
            return
        repository.old_result = RunRepository.resolve_publication_result(
            repository,
            run.run_id,
            already_published=False,
        )
        advancer = RunRepository(tmp_path)
        second = _publishing_run(
            advancer,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5M",
            requested_at=T3,
            stage_at=T3,
        )
        second_snapshot_id = "b" * 64
        second = advancer.attach_candidate(
            second.run_id,
            second_snapshot_id,
            expected_version=second.version,
            now=T4,
        )
        first_publication = repository.old_result.publication
        assert first_publication is not None
        RunRepository.commit_publication(
            advancer,
            second.run_id,
            ManifestPublicationV1(
                snapshot_id=second_snapshot_id,
                book_id="book-alpha",
                book_generation=1,
                snapshot_status=SnapshotStatus.BLESSED,
                schema_version="analytical_snapshot_manifest_v1",
                hash_algorithm="sha256",
                manifest_relpath=(
                    "snapshots/manifests/analytical_snapshot_manifest_v1/"
                    f"{second_snapshot_id[:2]}/{second_snapshot_id}.json"
                ),
                envelope_sha256="d" * 64,
                envelope_byte_length=4_096,
            ),
            expected_version=second.version,
            now=T4,
        )
        advanced = True

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=advance_after_commit,
    ).publish(run.run_id, candidate, authority=authority)

    assert advanced is True
    assert result.published is True
    assert RunRepository(tmp_path).get_active("book-alpha").snapshot_id == "b" * 64


def test_publisher_floors_commit_clock_to_an_identical_attached_candidate(
    tmp_path: Path,
) -> None:
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5C",
    )
    snapshot_id = create_manifest(candidate.manifest_body).snapshot_id
    attached = None

    def attach_identical_candidate_at_t4(stage) -> None:
        nonlocal attached
        if stage is module.PublisherFaultStage.BEFORE_CANDIDATE_ATTACH:
            current = repository.get(run.run_id)
            attached = repository.attach_candidate(
                run.run_id,
                snapshot_id,
                expected_version=current.version,
                now=T4,
            )

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
        fault_injector=attach_identical_candidate_at_t4,
    ).publish(run.run_id, candidate, authority=authority)

    assert attached is not None
    assert result.published is True
    assert result.run.finished_at_utc == attached.updated_at_utc
    assert result.run.updated_at_utc == attached.updated_at_utc
    assert result.publication is not None
    assert result.publication.published_at_utc == attached.updated_at_utc


def test_publisher_compares_manifest_identity_after_canonical_unicode_reparse(
    tmp_path: Path,
) -> None:
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5D",
    )
    decomposed = "Cafe\u0301-build"
    assert decomposed != unicodedata.normalize("NFC", decomposed)
    decomposed_source = "se\u0301lective-feed"
    assert decomposed_source != unicodedata.normalize("NFC", decomposed_source)
    input_binding = candidate.manifest_body.input_artifacts[0].model_copy(
        update={"source": decomposed_source}
    )
    body = candidate.manifest_body.model_copy(
        update={
            "application_build_id": decomposed,
            "input_artifacts": (input_binding,),
        }
    )
    candidate = module.SnapshotCandidateV1(
        manifest_body=body,
        outputs=candidate.outputs,
    )
    authority = authority.model_copy(update={"input_artifacts": (input_binding,)})

    result = module.SnapshotPublisher(
        repository=repository,
        store=store,
        clock=lambda: T3,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.published is True
    assert result.publication is not None
    verified = store.verify_snapshot(result.publication.snapshot_id)
    assert verified.manifest.body.application_build_id == unicodedata.normalize(
        "NFC", decomposed
    )
    assert verified.manifest.body.input_artifacts[0].source == unicodedata.normalize(
        "NFC", decomposed_source
    )


@pytest.mark.parametrize(
    "error_code",
    [RunErrorCode.WORKER_FAILED, RunErrorCode.STALE_BOOK_GENERATION],
)
def test_publisher_catalog_result_rejects_open_or_candidate_free_model_construct(
    tmp_path: Path,
    error_code: RunErrorCode,
) -> None:
    module, repository, _store, run, _authority, _candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5E",
    )
    failed = repository.mark_failed(
        run.run_id,
        RunFailureV1(code=RunErrorCode.WORKER_FAILED),
        expected_version=run.version,
        now=T3,
    )
    if error_code is RunErrorCode.STALE_BOOK_GENERATION:
        failed = failed.model_copy(
            update={
                "error_code": error_code,
                "error_message": (
                    "canonical book generation changed before publication"
                ),
            }
        )
    publication_result = PublicationResultV1.model_construct(
        run=failed,
        publication=None,
        active=None,
        published=False,
        already_published=False,
        rejection_code=error_code,
    )
    forged = module.SnapshotPublisherResultV1.model_construct(
        result_code=module.PublisherResultCode.CATALOG_RESULT,
        run=failed,
        publication_result=publication_result,
    )

    with pytest.raises(ValueError, match="publication|rejection|stale|open"):
        module.SnapshotPublisherResultV1.model_validate(
            forged.model_dump(mode="python", warnings=False)
        )


def test_terminal_publication_retry_uses_run_scoped_atomic_resolver(
    tmp_path: Path,
) -> None:
    module, _repository, store, run, authority, candidate = (
        _cancelled_publication_rejection_case(
            tmp_path,
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B5F",
        )
    )

    class ResolverOnlyRepository(RunRepository):
        resolver_calls = 0

        def get_active(self, _book_id):
            raise AssertionError("terminal retry must not assemble split-read evidence")

        def resolve_publication_result(self, run_id, *, already_published):
            self.resolver_calls += 1
            return super().resolve_publication_result(
                run_id, already_published=already_published
            )

    resolver = ResolverOnlyRepository(tmp_path)
    resolver.initialize()
    result = module.SnapshotPublisher(
        repository=resolver,
        store=store,
        clock=lambda: T4,
    ).publish(run.run_id, candidate, authority=authority)

    assert result.result_code is module.PublisherResultCode.CATALOG_RESULT
    assert result.rejection_code is RunErrorCode.CANCELLED_BY_USER
    assert result.run == run
    assert resolver.resolver_calls == 0
