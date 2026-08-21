from __future__ import annotations

import hashlib
import importlib
import multiprocessing
import os
import pickle
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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
    NewRunV1,
    RunDatabaseError,
    RunErrorCode,
    RunRepository,
)
from quantmind.snapshots.store import (
    ArtifactNotFoundError,
    SnapshotStore,
    SnapshotStoreError,
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
def test_publisher_revalidates_store_results_before_catalog_mutation(
    tmp_path: Path,
    bypass: str,
) -> None:
    # Break caught: a malicious/bypassed SnapshotStore subclass returning fields that
    # look usable without satisfying the frozen StoredManifest/VerifiedSnapshot contract.
    class BypassingStore(SnapshotStore):
        def put_verified_manifest(self, manifest):
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
            verified = super().verify_snapshot(
                snapshot_id,
                required_output_roles=required_output_roles,
            )
            if bypass == "verified_snapshot":
                return SimpleNamespace(
                    snapshot_id=verified.snapshot_id,
                    status=verified.status,
                    manifest=verified.manifest,
                )
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
    assert result.result_code is module.PublisherResultCode.TERMINAL_FAILURE
    assert result.publication_result is None
    assert durable == result.run
    assert durable.run_outcome is RunOutcome.FAILED
    assert durable.error_code is RunErrorCode.SERIALIZATION_FAILED
    assert durable.candidate_snapshot_id is None
    assert repository.list_publications("book-alpha") == ()
    assert repository.get_active("book-alpha") is None


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
    ["model_copy", "model_construct", "nested_model_copy"],
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
    else:
        invalid = result.model_copy(
            update={
                "publication_result": result.publication_result.model_copy(
                    update={"published": False}
                )
            }
        )

    with pytest.raises(ValueError, match="catalog|terminal"):
        module.SnapshotPublisherResultV1.model_validate(
            invalid.model_dump(mode="python", warnings=False)
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
    with pytest.raises(SnapshotStoreError):
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


@pytest.mark.parametrize("value_error_source", ["repository", "publisher_hook"])
def test_publisher_does_not_misclassify_infrastructure_value_errors(
    tmp_path: Path,
    value_error_source: str,
) -> None:
    # Break caught: a broad ValueError catch turning repository, clock, or test-hook
    # defects into false SERIALIZATION_FAILED evidence.
    armed = False

    class OneShotValueErrorRepository(RunRepository):
        def get(self, run_id):
            nonlocal armed
            if armed and value_error_source == "repository":
                armed = False
                raise ValueError("injected repository adapter defect")
            return super().get(run_id)

    repository = OneShotValueErrorRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0N",
        repository=repository,
    )
    armed = True

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
            clock=lambda: T3,
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
    race_armed = False

    class CancellationRaceRepository(RunRepository):
        def mark_failed(self, run_id, failure, *, expected_version, now):
            nonlocal race_armed
            if race_armed:
                race_armed = False
                self.request_cancel(run_id, now=now)
            return super().mark_failed(
                run_id,
                failure,
                expected_version=expected_version,
                now=now,
            )

    repository = CancellationRaceRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0P",
        repository=repository,
    )
    race_armed = True
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
        clock=lambda: T3,
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
    armed = False

    class FailingEvidenceRepository(RunRepository):
        def mark_failed(self, *args, **kwargs):
            if armed:
                raise RunDatabaseError("injected failure-evidence database fault")
            return super().mark_failed(*args, **kwargs)

    repository = FailingEvidenceRepository(tmp_path)
    module, repository, store, run, authority, candidate = _publisher_case(
        tmp_path,
        run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0Q",
        repository=repository,
    )
    armed = True
    bad_output = candidate.outputs[0].model_copy(
        update={"payload": b'{"xray":"tampered"}'}
    )
    forged = module.SnapshotCandidateV1(
        manifest_body=candidate.manifest_body,
        outputs=(bad_output,),
    )

    with pytest.raises(RunDatabaseError, match="failure-evidence"):
        module.SnapshotPublisher(
            repository=repository,
            store=store,
            clock=lambda: T3,
        ).publish(run.run_id, forged, authority=authority)

    durable = RunRepository(tmp_path)
    assert durable.get(run.run_id).run_outcome is RunOutcome.RUNNING
    assert durable.get(run.run_id).candidate_snapshot_id is None
    assert durable.list_publications("book-alpha") == ()
