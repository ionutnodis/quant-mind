from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
)
from quantmind.snapshots.run_repository import NewRunV1, RunRepository
from quantmind.snapshots.store import SnapshotStore


T0 = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 20, 8, 1, tzinfo=UTC)
T2 = datetime(2026, 8, 20, 8, 2, tzinfo=UTC)
T3 = datetime(2026, 8, 20, 8, 3, tzinfo=UTC)


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


def _publishing_run(repository: RunRepository):
    run = repository.create_or_join(
        NewRunV1(
            run_id="run_01J5X5S8J5J8P7KQ4Y0T3T3B0A",
            run_kind="ANALYTICAL_SNAPSHOT",
            request_fingerprint="a" * 64,
            client_idempotency_key="refresh",
            book_id="book-alpha",
            target_cut_utc=T0,
        ),
        now=T1,
    ).record
    run = repository.claim_start(run.run_id, expected_version=run.version, now=T1)
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
            now=T2,
        )
    return run


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
    repository.advance_book_head(
        "book-alpha", 1, canonical_book_ref.digest, now=T0
    )
    run = _publishing_run(repository)
    authority = publisher_module.PublicationAuthorityV1(
        canonical_book_ref=canonical_book_ref,
        input_artifacts=(input_binding,),
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
    assert result.publication.envelope_sha256 != result.publication.snapshot_id
    assert result.active is not None
    assert result.active.snapshot_id == result.publication.snapshot_id
    assert result.active.pointer_version == 1
    assert store.read_verified_artifact(output_ref) == output_payload
    verified = SnapshotStore(tmp_path).verify_snapshot(result.publication.snapshot_id)
    assert verified.manifest.body.outputs[0].object_ref == output_ref
    envelope = canonical_json_bytes(verified.manifest)
    assert result.publication.envelope_sha256 == hashlib.sha256(envelope).hexdigest()
    assert result.publication.envelope_byte_length == len(envelope)
    reopened = RunRepository(tmp_path)
    assert reopened.get(run.run_id) == result.run
    assert reopened.list_publications("book-alpha") == (result.publication,)
    assert reopened.get_active("book-alpha") == result.active
