from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import quantmind.snapshots.store as snapshot_store
from quantmind.snapshots.contracts import (
    GateEvidenceV1,
    GateStatus,
    RecoveryClass,
    SnapshotStatus,
    ValuationCutV1,
    canonical_json_bytes,
)
from quantmind.snapshots.input_artifacts import (
    ArtifactRightsMode,
    InputRepresentation,
    ReproducibilityClass,
    bind_input_artifact,
)
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestBodyV1,
    AnalyticalSnapshotManifestV1,
    ArtifactRefV1,
    DuplicateJSONKeyError,
    ManifestIdentityError,
    OutputArtifactBindingV1,
    UnsupportedManifestSchemaError,
    create_manifest,
)
from quantmind.snapshots.store import (
    ActiveSnapshotResolutionV1,
    ArtifactDigestMismatchError,
    ArtifactLengthMismatchError,
    ArtifactNotFoundError,
    ManifestFilenameMismatchError,
    NoClobberPublicationError,
    NonRegularSnapshotFileError,
    SnapshotStore,
    SnapshotVerificationError,
    select_last_good,
)


def _fake_ref(digest: str = "f" * 64, *, byte_length: int = 7) -> ArtifactRefV1:
    return ArtifactRefV1(
        hash_algorithm="sha256",
        digest=digest,
        byte_length=byte_length,
        media_type="application/json",
        schema_version="synthetic_v1",
    )


def _manifest(
    *,
    canonical_book_ref: ArtifactRefV1,
    input_ref: ArtifactRefV1,
    output_ref: ArtifactRefV1,
    status: SnapshotStatus = SnapshotStatus.BLESSED,
    generation: int = 1,
) -> object:
    input_binding = bind_input_artifact(
        logical_role="NORMALIZED_MARKS",
        logical_id="marks",
        representation=InputRepresentation.NORMALIZED_INPUT,
        object_ref=input_ref,
        source="synthetic",
        provider="quantmind",
        entitlement_reference=None,
        entitlement_version=None,
        rights_mode=ArtifactRightsMode.RAW_ALLOWED,
        rights_manifest_version="synthetic-rights-v1",
        reproducibility_class=ReproducibilityClass.NORMALIZED_ONLY,
    )
    output_binding = OutputArtifactBindingV1(
        logical_role="XRAY_READ_MODEL",
        logical_id="xray",
        object_ref=output_ref,
        model_version="xray-v1",
    )
    book_gate = GateEvidenceV1(
        gate_code="BOOK_RECONCILIATION",
        status=GateStatus.PASSED,
        recovery_class=RecoveryClass.USER_RESOLVABLE,
        evidence=("book reconciles",),
        recovery_action="Resolve the book mismatch",
    )
    gates = [book_gate]
    policy_evidence = [
        {
            "subject_kind": "OUTPUT",
            "subject_id": "xray",
            "gate_code": "BOOK_RECONCILIATION",
        }
    ]
    refused_outputs: tuple[str, ...] = ()
    if status is SnapshotStatus.DEGRADED:
        gates.append(
            GateEvidenceV1(
                gate_code="TAIL_POLICY",
                status=GateStatus.REFUSED,
                recovery_class=RecoveryClass.MODEL_OWNER_UPDATE,
                evidence=("tail model is unavailable",),
                recovery_action="Publish a supported tail model",
            )
        )
        policy_evidence.append(
            {
                "subject_kind": "CAPABILITY",
                "subject_id": "TAIL",
                "gate_code": "TAIL_POLICY",
            }
        )
        refused_outputs = ("TAIL",)

    body = AnalyticalSnapshotManifestBodyV1(
        schema_version="analytical_snapshot_manifest_v1",
        canonicalization_version="quantmind_canonical_json_v1",
        hash_algorithm="sha256",
        book_id="synthetic-book",
        book_generation=generation,
        legacy_book_ref="abcdef012345",
        valuation_cut=ValuationCutV1(
            target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
            display_timezone="America/New_York",
            capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
            capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
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
        application_commit="1d2b187",
        application_build_id="test-build",
        snapshot_status=status,
        gates=tuple(sorted(gates, key=lambda gate: gate.gate_code)),
        policy_evidence=tuple(
            sorted(
                policy_evidence,
                key=lambda evidence: (
                    evidence["subject_kind"],
                    evidence["subject_id"],
                    evidence["gate_code"],
                ),
            )
        ),
        warnings=(),
        refused_outputs=refused_outputs,
        outputs=(output_binding,),
    )
    return create_manifest(body)


def _complete_manifest(
    store: SnapshotStore,
    *,
    status: SnapshotStatus = SnapshotStatus.BLESSED,
    generation: int = 1,
):
    book = store.put_bytes(
        b'{"schema_version":"canonical_book_v1"}',
        media_type="application/json",
        schema_version="canonical_book_v1",
    )
    input_ref = store.put_bytes(
        b'{"marks":"synthetic"}',
        media_type="application/json",
        schema_version="normalized_marks_v1",
    )
    output = store.put_bytes(
        b'{"xray":"synthetic"}',
        media_type="application/json",
        schema_version="xray_read_model_v1",
    )
    return _manifest(
        canonical_book_ref=book,
        input_ref=input_ref,
        output_ref=output,
        status=status,
        generation=generation,
    )


def _object_path(root: Path, digest: str) -> Path:
    return root / "snapshots" / "objects" / "sha256" / digest[:2] / digest


def _manifest_path(root: Path, snapshot_id: str) -> Path:
    return (
        root
        / "snapshots"
        / "manifests"
        / "analytical_snapshot_manifest_v1"
        / snapshot_id[:2]
        / f"{snapshot_id}.json"
    )


def test_bytes_and_canonical_contract_roundtrip_at_exact_full_digest_paths(tmp_path):
    store = SnapshotStore(tmp_path)
    payload = b"opaque\x00artifact"
    ref = store.put_bytes(
        payload,
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    )
    assert ref.digest == "ad24f8366d2e13ad7f3cc8c2bbe05a235184caefd2e5f31c3bbd1edeacfd264e"
    assert ref.byte_length == len(payload)
    assert _object_path(tmp_path, ref.digest).read_bytes() == payload
    assert stat.S_IMODE(_object_path(tmp_path, ref.digest).stat().st_mode) == 0o600
    assert store.read_verified_artifact(ref) == payload
    assert store.put_bytes(
        payload,
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    ) == ref

    cut = ValuationCutV1(
        target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        display_timezone="America/New_York",
        capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
    )
    canonical_ref = store.put_canonical(cut, media_type="application/json")
    assert store.read_verified_artifact(canonical_ref) == (
        b'{"capture_end_utc":"2026-07-24T20:20:00Z",'
        b'"capture_start_utc":"2026-07-24T20:15:00Z",'
        b'"display_timezone":"America/New_York",'
        b'"target_cut_utc":"2026-07-24T20:15:00Z"}'
    )


def test_store_reads_accept_only_full_lowercase_ids_and_ignore_temp_names(tmp_path):
    store = SnapshotStore(tmp_path)
    with pytest.raises(ValueError, match="64 lowercase"):
        store.read_verified_manifest("abcdef012345")
    with pytest.raises(ValueError, match="64 lowercase"):
        store.read_verified_manifest("A" * 64)

    full_id = "a" * 64
    temp = _manifest_path(tmp_path, full_id).with_name(f".{full_id}.abandoned.tmp")
    temp.parent.mkdir(parents=True)
    temp.write_bytes(b"complete-looking but unpublished")
    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_manifest(full_id)


def test_reader_rejects_missing_truncated_tampered_symlink_and_nonregular_objects(tmp_path):
    store = SnapshotStore(tmp_path)
    missing = _fake_ref()
    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_artifact(missing)

    ref = store.put_bytes(
        b"verified artifact",
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    )
    path = _object_path(tmp_path, ref.digest)
    path.write_bytes(b"short")
    with pytest.raises(ArtifactLengthMismatchError):
        store.read_verified_artifact(ref)

    path.write_bytes(b"x" * ref.byte_length)
    with pytest.raises(ArtifactDigestMismatchError):
        store.read_verified_artifact(ref)

    path.unlink()
    target = tmp_path / "symlink-target"
    target.write_bytes(b"verified artifact")
    path.symlink_to(target)
    with pytest.raises(NonRegularSnapshotFileError):
        store.read_verified_artifact(ref)

    path.unlink()
    path.mkdir()
    with pytest.raises(NonRegularSnapshotFileError):
        store.read_verified_artifact(ref)


def test_put_never_overwrites_a_corrupt_existing_content_address(tmp_path):
    store = SnapshotStore(tmp_path)
    payload = b"immutable bytes"
    ref = store.put_bytes(
        payload,
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    )
    path = _object_path(tmp_path, ref.digest)
    path.write_bytes(b"corrupt content")

    with pytest.raises((ArtifactLengthMismatchError, ArtifactDigestMismatchError)):
        store.put_bytes(
            payload,
            media_type="application/octet-stream",
            schema_version="opaque_v1",
        )
    assert path.read_bytes() == b"corrupt content"


def test_eexist_race_verifies_the_winner_instead_of_replacing_it(tmp_path):
    payload = b"same concurrent payload"
    target_created = False

    def create_winner(stage: str, path: Path) -> None:
        nonlocal target_created
        if stage == "before_link" and not target_created:
            path.write_bytes(payload)
            os.chmod(path, 0o600)
            target_created = True

    store = SnapshotStore(tmp_path, fault_injector=create_winner)
    ref = store.put_bytes(
        payload,
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    )
    assert target_created
    assert store.read_verified_artifact(ref) == payload
    assert not tuple(_object_path(tmp_path, ref.digest).parent.glob("*.tmp"))


def test_unsupported_no_clobber_primitive_fails_closed(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)

    def unsupported(_source: Path, _target: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(store, "_link_no_clobber", unsupported)
    with pytest.raises(NoClobberPublicationError):
        store.put_bytes(
            b"cannot publish",
            media_type="application/octet-stream",
            schema_version="opaque_v1",
        )


@pytest.mark.parametrize(
    ("fault_stage", "final_should_exist"),
    [
        ("before_write", False),
        ("after_write_chunk", False),
        ("before_file_fsync", False),
        ("before_reread", False),
        ("before_link", False),
        ("before_directory_fsync", True),
    ],
)
def test_faults_never_make_an_incomplete_final_object_verifiable(
    tmp_path, fault_stage, final_should_exist
):
    payload = b"x" * (128 * 1024)
    injected = False

    def fail_once(stage: str, _path: Path) -> None:
        nonlocal injected
        if stage == fault_stage and not injected:
            injected = True
            raise RuntimeError(f"injected {stage}")

    store = SnapshotStore(tmp_path, fault_injector=fail_once)
    with pytest.raises(RuntimeError, match=fault_stage):
        store.put_bytes(
            payload,
            media_type="application/octet-stream",
            schema_version="opaque_v1",
        )
    assert injected

    digest = "15601535eca4a38b7e31ad6494861121cb9f84ccf55d4beb6a707d4f7a87813d"
    path = _object_path(tmp_path, digest)
    assert path.exists() is final_should_exist
    if final_should_exist:
        ref = ArtifactRefV1(
            hash_algorithm="sha256",
            digest=digest,
            byte_length=len(payload),
            media_type="application/octet-stream",
            schema_version="opaque_v1",
        )
        assert SnapshotStore(tmp_path).read_verified_artifact(ref) == payload
    assert not tuple(path.parent.glob("*.tmp"))


def test_two_processes_converge_on_one_object_without_shared_temp_names(tmp_path):
    code = """
import sys
from pathlib import Path
from quantmind.snapshots.store import SnapshotStore
root = Path(sys.argv[1])
ref = SnapshotStore(root).put_bytes(
    b'concurrent object',
    media_type='application/octet-stream',
    schema_version='opaque_v1',
)
print(ref.digest)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=20) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], results
    digests = [stdout.strip() for stdout, _stderr in results]
    assert digests[0] == digests[1]
    parent = _object_path(tmp_path, digests[0]).parent
    assert [path.name for path in parent.iterdir()] == [digests[0]]


def test_put_manifest_verifies_all_required_refs_before_durable_orphan_storage(tmp_path):
    store = SnapshotStore(tmp_path)
    manifest = _complete_manifest(store)
    path = store.put_manifest(manifest)
    assert path == _manifest_path(tmp_path, manifest.snapshot_id)
    assert store.read_verified_manifest(manifest.snapshot_id) == manifest
    verified = store.verify_snapshot(
        manifest.snapshot_id, required_output_roles=("XRAY_READ_MODEL",)
    )
    assert verified.snapshot_id == manifest.snapshot_id
    assert verified.status is SnapshotStatus.BLESSED
    assert not (tmp_path / "snapshots" / "active.json").exists()

    with pytest.raises(SnapshotVerificationError, match="required output role"):
        store.verify_snapshot(manifest.snapshot_id, required_output_roles=("TAIL",))


def test_put_verified_manifest_returns_exact_durable_envelope_metadata(tmp_path):
    # Break caught: the catalog publisher confusing the body-derived snapshot ID with
    # the full canonical manifest-envelope digest, path, or byte length.
    store = SnapshotStore(tmp_path)
    manifest = _complete_manifest(store)
    envelope = canonical_json_bytes(manifest)

    stored = store.put_verified_manifest(manifest)

    assert stored == snapshot_store.StoredManifestV1(
        snapshot_id=manifest.snapshot_id,
        manifest_relpath=(
            "snapshots/manifests/analytical_snapshot_manifest_v1/"
            f"{manifest.snapshot_id[:2]}/{manifest.snapshot_id}.json"
        ),
        envelope_sha256=hashlib.sha256(envelope).hexdigest(),
        envelope_byte_length=len(envelope),
        status=SnapshotStatus.BLESSED,
        manifest=manifest,
    )
    assert stored.envelope_sha256 != stored.snapshot_id
    assert store.read_verified_manifest(stored.snapshot_id) == stored.manifest


def test_inspect_verified_manifest_returns_existing_metadata_without_writing(tmp_path):
    # Break caught: terminal retry rebuilding metadata from an unbounded catalog scan
    # or accidentally recreating missing historical bytes through a write seam.
    store = SnapshotStore(tmp_path)
    manifest = _complete_manifest(store)
    expected = store.put_verified_manifest(manifest)
    files_before = tuple(
        sorted(
            path.relative_to(tmp_path)
            for path in tmp_path.rglob("*")
            if path.is_file()
        )
    )

    inspected = SnapshotStore(tmp_path).inspect_verified_manifest(
        manifest.snapshot_id
    )

    files_after = tuple(
        sorted(
            path.relative_to(tmp_path)
            for path in tmp_path.rglob("*")
            if path.is_file()
        )
    )
    assert inspected == expected
    assert files_after == files_before


def test_read_verified_manifest_fails_when_a_required_artifact_later_disappears(tmp_path):
    store = SnapshotStore(tmp_path)
    manifest = _complete_manifest(store)
    store.put_manifest(manifest)
    output_ref = manifest.body.outputs[0].object_ref
    _object_path(tmp_path, output_ref.digest).unlink()

    with pytest.raises(ArtifactNotFoundError):
        store.read_verified_manifest(manifest.snapshot_id)


def test_put_manifest_refuses_missing_or_corrupt_referenced_objects(tmp_path):
    store = SnapshotStore(tmp_path)
    book = store.put_bytes(
        b"book", media_type="application/json", schema_version="canonical_book_v1"
    )
    output = store.put_bytes(
        b"output", media_type="application/json", schema_version="xray_v1"
    )
    missing_manifest = _manifest(
        canonical_book_ref=book,
        input_ref=_fake_ref(),
        output_ref=output,
    )
    with pytest.raises(ArtifactNotFoundError):
        store.put_manifest(missing_manifest)
    assert not _manifest_path(tmp_path, missing_manifest.snapshot_id).exists()

    input_ref = store.put_bytes(
        b"input", media_type="application/json", schema_version="input_v1"
    )
    corrupt_manifest = _manifest(
        canonical_book_ref=book,
        input_ref=input_ref,
        output_ref=output,
    )
    _object_path(tmp_path, output.digest).write_bytes(b"damage")
    with pytest.raises((ArtifactLengthMismatchError, ArtifactDigestMismatchError)):
        store.put_manifest(corrupt_manifest)
    assert not _manifest_path(tmp_path, corrupt_manifest.snapshot_id).exists()


def test_store_revalidates_bypassed_generic_canonical_contract_before_writing(tmp_path):
    store = SnapshotStore(tmp_path)
    cut = ValuationCutV1(
        target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        display_timezone="America/New_York",
        capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
    )
    invalid_cut = cut.model_copy(update={"display_timezone": "Mars/Olympus_Mons"})
    with pytest.raises(ValueError):
        store.put_canonical(invalid_cut, media_type="application/json")

    missing_cut = ValuationCutV1.model_construct(
        target_cut_utc=cut.target_cut_utc,
        capture_start_utc=cut.capture_start_utc,
        capture_end_utc=cut.capture_end_utc,
    )
    with pytest.raises(ValueError):
        store.put_canonical(missing_cut, media_type="application/json")
    assert not (tmp_path / "snapshots").exists()


def test_store_revalidates_bypassed_artifact_reference_before_reading(tmp_path):
    store = SnapshotStore(tmp_path)
    reference = store.put_bytes(
        b"valid bytes",
        media_type="application/octet-stream",
        schema_version="opaque_v1",
    )
    invalid_reference = reference.model_copy(update={"hash_algorithm": "sha512"})
    with pytest.raises(ValueError):
        store.read_verified_artifact(invalid_reference)

    missing_reference = ArtifactRefV1.model_construct(
        hash_algorithm="sha256",
        byte_length=reference.byte_length,
        media_type=reference.media_type,
        schema_version=reference.schema_version,
    )
    with pytest.raises(ValueError):
        store.read_verified_artifact(missing_reference)


def test_store_strictly_parses_bypassed_manifest_bytes_before_reference_checks(tmp_path):
    store = SnapshotStore(tmp_path)
    manifest = _complete_manifest(store)

    invalid_top_body = manifest.body.model_copy(update={"base_currency": "US"})
    invalid_output_ref = manifest.body.outputs[0].object_ref.model_copy(
        update={"hash_algorithm": "sha512"}
    )
    invalid_output = manifest.body.outputs[0].model_copy(
        update={"object_ref": invalid_output_ref}
    )
    invalid_nested_body = manifest.body.model_copy(update={"outputs": (invalid_output,)})

    for invalid_body in (invalid_top_body, invalid_nested_body):
        invalid_id = hashlib.sha256(canonical_json_bytes(invalid_body)).hexdigest()
        bypassed = AnalyticalSnapshotManifestV1.model_construct(
            snapshot_id=invalid_id,
            body=invalid_body,
        )
        with pytest.raises(ValueError):
            store.put_manifest(bypassed)
        assert not _manifest_path(tmp_path, invalid_id).exists()

    missing_envelope = AnalyticalSnapshotManifestV1.model_construct(body=manifest.body)
    with pytest.raises(ValueError):
        store.put_manifest(missing_envelope)


@pytest.mark.parametrize("corruption", ["duplicate", "unknown_schema", "embedded_id"])
def test_manifest_reader_rejects_ambiguous_schema_or_filename_identity(tmp_path, corruption):
    store = SnapshotStore(tmp_path)
    manifest = _complete_manifest(store)
    path = _manifest_path(tmp_path, manifest.snapshot_id)
    path.parent.mkdir(parents=True)
    canonical = __import__(
        "quantmind.snapshots.contracts", fromlist=["canonical_json_bytes"]
    ).canonical_json_bytes(manifest)
    if corruption == "duplicate":
        payload = b'{"snapshot_id":"' + b"0" * 64 + b'",' + canonical[1:]
        expected = DuplicateJSONKeyError
    elif corruption == "unknown_schema":
        payload = canonical.replace(
            b'"schema_version":"analytical_snapshot_manifest_v1"',
            b'"schema_version":"analytical_snapshot_manifest_v2"',
        )
        expected = UnsupportedManifestSchemaError
    else:
        other_id = "e" * 64
        other_path = _manifest_path(tmp_path, other_id)
        other_path.parent.mkdir(parents=True)
        other_path.write_bytes(canonical)
        with pytest.raises(ManifestFilenameMismatchError):
            store.read_verified_manifest(other_id)
        return
    path.write_bytes(payload)
    with pytest.raises(expected):
        store.read_verified_manifest(manifest.snapshot_id)


def test_select_last_good_keeps_a_verified_active_blessed_or_degraded_snapshot(tmp_path):
    store = SnapshotStore(tmp_path)
    blessed = _complete_manifest(store, generation=10)
    degraded = _complete_manifest(
        store, status=SnapshotStatus.DEGRADED, generation=11
    )
    store.put_manifest(blessed)
    store.put_manifest(degraded)
    calls: list[str] = []

    def verify(snapshot_id: str):
        calls.append(snapshot_id)
        return store.verify_snapshot(snapshot_id)

    for active in (blessed, degraded):
        calls.clear()
        resolution = select_last_good(
            active.snapshot_id,
            ("f" * 64,),
            verify,
        )
        assert isinstance(resolution, ActiveSnapshotResolutionV1)
        assert resolution.requested_snapshot_id == active.snapshot_id
        assert resolution.resolved_snapshot_id == active.snapshot_id
        assert resolution.resolved_status is active.body.snapshot_status
        assert resolution.fallback_used is False
        assert resolution.failures == ()
        assert calls == [active.snapshot_id]


def test_selector_preserves_manifest_identity_error_code_for_a_full_wrong_id(tmp_path):
    store = SnapshotStore(tmp_path)
    prior = _complete_manifest(store, generation=9)
    store.put_manifest(prior)

    wrong_id = "0" * 64
    wrong_path = _manifest_path(tmp_path, wrong_id)
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_bytes(
        canonical_json_bytes({"snapshot_id": wrong_id, "body": prior.body})
    )

    resolution = select_last_good(
        wrong_id,
        (prior.snapshot_id,),
        store.verify_snapshot,
    )
    assert resolution.resolved_snapshot_id == prior.snapshot_id
    assert resolution.fallback_used is True
    assert resolution.failures[0].snapshot_id == wrong_id
    assert resolution.failures[0].error_code == ManifestIdentityError.__name__


def test_select_last_good_uses_only_caller_ordered_verified_prior_blessed_ids(tmp_path):
    store = SnapshotStore(tmp_path)
    active = _complete_manifest(store, generation=20)
    corrupt_prior = _complete_manifest(store, generation=19)
    degraded_prior = _complete_manifest(
        store, status=SnapshotStatus.DEGRADED, generation=18
    )
    valid_prior = _complete_manifest(store, generation=17)
    orphan_not_supplied = _complete_manifest(store, generation=16)
    for manifest in (
        active,
        corrupt_prior,
        degraded_prior,
        valid_prior,
        orphan_not_supplied,
    ):
        store.put_manifest(manifest)

    _object_path(tmp_path, active.body.outputs[0].object_ref.digest).unlink()
    # The manifests share their synthetic outputs, so republish distinct output objects and
    # manifests for each fallback candidate before corrupting only the immediate prior.
    prior_output = store.put_bytes(
        b"prior-output",
        media_type="application/json",
        schema_version="xray_v1",
    )
    corrupt_prior = _manifest(
        canonical_book_ref=corrupt_prior.body.canonical_book_ref,
        input_ref=corrupt_prior.body.input_artifacts[0].object_ref,
        output_ref=prior_output,
        generation=19,
    )
    store.put_manifest(corrupt_prior)
    valid_output = store.put_bytes(
        b"valid-prior-output",
        media_type="application/json",
        schema_version="xray_v1",
    )
    valid_prior = _manifest(
        canonical_book_ref=valid_prior.body.canonical_book_ref,
        input_ref=valid_prior.body.input_artifacts[0].object_ref,
        output_ref=valid_output,
        generation=17,
    )
    store.put_manifest(valid_prior)
    degraded_output = store.put_bytes(
        b"degraded-prior-output",
        media_type="application/json",
        schema_version="xray_v1",
    )
    degraded_prior = _manifest(
        canonical_book_ref=degraded_prior.body.canonical_book_ref,
        input_ref=degraded_prior.body.input_artifacts[0].object_ref,
        output_ref=degraded_output,
        status=SnapshotStatus.DEGRADED,
        generation=18,
    )
    store.put_manifest(degraded_prior)
    _object_path(tmp_path, prior_output.digest).write_bytes(b"corrupt-prior")

    calls: list[str] = []

    def verify(snapshot_id: str):
        calls.append(snapshot_id)
        return store.verify_snapshot(snapshot_id)

    files_before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
    resolution = select_last_good(
        active.snapshot_id,
        (
            corrupt_prior.snapshot_id,
            degraded_prior.snapshot_id,
            valid_prior.snapshot_id,
        ),
        verify,
    )
    files_after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())

    assert resolution.resolved_snapshot_id == valid_prior.snapshot_id
    assert resolution.resolved_status is SnapshotStatus.BLESSED
    assert resolution.fallback_used is True
    assert [failure.snapshot_id for failure in resolution.failures] == [
        active.snapshot_id,
        corrupt_prior.snapshot_id,
        degraded_prior.snapshot_id,
    ]
    assert resolution.failures[-1].error_code == "NOT_BLESSED"
    assert calls == [
        active.snapshot_id,
        corrupt_prior.snapshot_id,
        degraded_prior.snapshot_id,
        valid_prior.snapshot_id,
    ]
    assert orphan_not_supplied.snapshot_id not in calls
    assert files_after == files_before


def test_select_last_good_records_malformed_ids_and_returns_no_readable_snapshot():
    calls: list[str] = []

    def verify(snapshot_id: str):
        calls.append(snapshot_id)
        raise ArtifactNotFoundError(f"missing {snapshot_id}")

    resolution = select_last_good(
        "bad-active-prefix",
        ("a" * 64, "b" * 64),
        verify,
    )
    assert resolution.requested_snapshot_id == "bad-active-prefix"
    assert resolution.resolved_snapshot_id is None
    assert resolution.resolved_status is None
    assert resolution.fallback_used is False
    assert [failure.error_code for failure in resolution.failures] == [
        "ArtifactNotFoundError",
        "ArtifactNotFoundError",
        "ArtifactNotFoundError",
    ]
    assert calls == ["bad-active-prefix", "a" * 64, "b" * 64]
