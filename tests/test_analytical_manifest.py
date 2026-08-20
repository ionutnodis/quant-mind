from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

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
    canonical_input_bytes,
    retained_raw_input_bytes,
)
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestBodyV1,
    AnalyticalSnapshotManifestV1,
    ArtifactRefV1,
    DuplicateJSONKeyError,
    NonCanonicalManifestError,
    NonFiniteJSONConstantError,
    OutputArtifactBindingV1,
    UnsupportedManifestSchemaError,
    create_manifest,
    parse_manifest,
    snapshot_display_prefix,
    verify_manifest,
)


BOOK_DIGEST = "1" * 64
INPUT_DIGEST = "2" * 64
OUTPUT_DIGEST = "3" * 64
CONFIG_DIGEST = "4" * 64
POSITION_DIGEST = "5" * 64


def _ref(digest: str, *, schema_version: str, byte_length: int) -> ArtifactRefV1:
    return ArtifactRefV1(
        hash_algorithm="sha256",
        digest=digest,
        byte_length=byte_length,
        media_type="application/json",
        schema_version=schema_version,
    )


def _body(**changes) -> AnalyticalSnapshotManifestBodyV1:
    input_binding = bind_input_artifact(
        logical_role="NORMALIZED_MARKS",
        logical_id="marks-at-cut",
        representation=InputRepresentation.NORMALIZED_INPUT,
        object_ref=_ref(
            INPUT_DIGEST,
            schema_version="normalized_marks_v1",
            byte_length=17,
        ),
        source="synthetic-fixture",
        provider="quantmind",
        rights_mode=ArtifactRightsMode.RAW_ALLOWED,
        rights_manifest_version="synthetic-rights-v1",
        reproducibility_class=ReproducibilityClass.NORMALIZED_ONLY,
    )
    output_binding = OutputArtifactBindingV1(
        logical_role="XRAY_READ_MODEL",
        logical_id="book-xray",
        object_ref=_ref(
            OUTPUT_DIGEST,
            schema_version="xray_read_model_v1",
            byte_length=19,
        ),
        model_version="xray-model-v1",
    )
    gate = GateEvidenceV1(
        gate_code="BOOK_RECONCILIATION",
        status=GateStatus.PASSED,
        recovery_class=RecoveryClass.USER_RESOLVABLE,
        evidence=("normalized minus source NLV is USD 0.00",),
        recovery_action="Resolve account or cash mismatches",
    )
    values = {
        "schema_version": "analytical_snapshot_manifest_v1",
        "canonicalization_version": "quantmind_canonical_json_v1",
        "hash_algorithm": "sha256",
        "book_id": "synthetic-book",
        "book_generation": 7,
        "legacy_book_ref": "abcdef012345",
        "valuation_cut": ValuationCutV1(
            target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
            display_timezone="America/New_York",
            capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
            capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
        ),
        "base_currency": "USD",
        "normalized_nlv": Decimal("1000.00"),
        "included_account_ids": ("account-a", "account-b"),
        "canonical_book_ref": _ref(
            BOOK_DIGEST,
            schema_version="canonical_book_v1",
            byte_length=23,
        ),
        "canonical_book_hash": BOOK_DIGEST,
        "position_hash": POSITION_DIGEST,
        "input_artifacts": (input_binding,),
        "security_master_mapping_version": "security-master-v1",
        "rights_manifest_versions": ("synthetic-rights-v1",),
        "factor_taxonomy_version": "factor-taxonomy-v1",
        "return_series_version": "daily-base-return-v1",
        "production_covariance_model_version": "factor-covariance-v1",
        "residual_model_version": "shrunk-residual-v1",
        "latent_factor_model_version": None,
        "option_pricer_version": None,
        "surface_model_version": None,
        "scenario_library_version": None,
        "analytical_config_hash": CONFIG_DIGEST,
        "application_commit": "1d2b187",
        "application_build_id": "quantmind-0.2.0-test",
        "snapshot_status": SnapshotStatus.BLESSED,
        "gates": (gate,),
        "warnings": ("ASYNCHRONOUS_CUT",),
        "refused_outputs": (),
        "outputs": (output_binding,),
    }
    values.update(changes)
    return AnalyticalSnapshotManifestBodyV1(**values)


def test_input_helpers_use_t1_canonical_bytes_and_enforce_raw_rights():
    contract = ValuationCutV1(
        target_cut_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        display_timezone="America/New_York",
        capture_start_utc=datetime(2026, 7, 24, 20, 15, tzinfo=UTC),
        capture_end_utc=datetime(2026, 7, 24, 20, 20, tzinfo=UTC),
    )
    assert canonical_input_bytes(contract) == canonical_json_bytes(contract)
    assert retained_raw_input_bytes(
        b"permitted vendor bytes", rights_mode=ArtifactRightsMode.RAW_ALLOWED
    ) == b"permitted vendor bytes"

    with pytest.raises(ValueError, match="PROVENANCE_ONLY"):
        retained_raw_input_bytes(
            b"forbidden vendor bytes",
            rights_mode=ArtifactRightsMode.PROVENANCE_ONLY,
        )


def test_input_binding_rejects_rights_representation_mismatch_and_unknown_metadata():
    values = {
        "logical_role": "RAW_QUOTES",
        "logical_id": "quotes-at-cut",
        "representation": InputRepresentation.RAW_RETAINED,
        "object_ref": _ref(
            INPUT_DIGEST,
            schema_version="opaque_vendor_quotes_v1",
            byte_length=17,
        ),
        "source": "vendor-feed",
        "provider": "licensed-vendor",
        "rights_mode": ArtifactRightsMode.PROVENANCE_ONLY,
        "rights_manifest_version": "vendor-rights-v1",
        "reproducibility_class": ReproducibilityClass.COMPLETE,
    }
    with pytest.raises(ValueError, match="raw retained"):
        bind_input_artifact(**values)

    values["rights_mode"] = ArtifactRightsMode.RAW_ALLOWED
    with pytest.raises(ValueError):
        bind_input_artifact(**values, authorization_header="Bearer secret")


def test_manifest_body_has_a_literal_canonical_preimage_and_full_sha256_golden():
    body = _body()
    expected = (
        b'{"analytical_config_hash":"4444444444444444444444444444444444444444444444444444444444444444",'
        b'"application_build_id":"quantmind-0.2.0-test","application_commit":"1d2b187",'
        b'"base_currency":"USD","book_generation":7,"book_id":"synthetic-book",'
        b'"canonical_book_hash":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"canonical_book_ref":{"byte_length":23,"digest":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"hash_algorithm":"sha256","media_type":"application/json","schema_version":"canonical_book_v1"},'
        b'"canonicalization_version":"quantmind_canonical_json_v1","factor_taxonomy_version":"factor-taxonomy-v1",'
        b'"gates":[{"evidence":["normalized minus source NLV is USD 0.00"],"gate_code":"BOOK_RECONCILIATION",'
        b'"recovery_action":"Resolve account or cash mismatches","recovery_class":"USER_RESOLVABLE","status":"PASSED"}],'
        b'"hash_algorithm":"sha256","included_account_ids":["account-a","account-b"],'
        b'"input_artifacts":[{"logical_id":"marks-at-cut","logical_role":"NORMALIZED_MARKS",'
        b'"object_ref":{"byte_length":17,"digest":"2222222222222222222222222222222222222222222222222222222222222222",'
        b'"hash_algorithm":"sha256","media_type":"application/json","schema_version":"normalized_marks_v1"},'
        b'"provider":"quantmind","representation":"NORMALIZED_INPUT","reproducibility_class":"NORMALIZED_ONLY",'
        b'"rights_manifest_version":"synthetic-rights-v1","rights_mode":"RAW_ALLOWED","source":"synthetic-fixture"}],'
        b'"latent_factor_model_version":null,"legacy_book_ref":"abcdef012345","normalized_nlv":"1000.00",'
        b'"option_pricer_version":null,"outputs":[{"logical_id":"book-xray","logical_role":"XRAY_READ_MODEL",'
        b'"model_version":"xray-model-v1","object_ref":{"byte_length":19,"digest":"3333333333333333333333333333333333333333333333333333333333333333",'
        b'"hash_algorithm":"sha256","media_type":"application/json","schema_version":"xray_read_model_v1"}}],'
        b'"position_hash":"5555555555555555555555555555555555555555555555555555555555555555",'
        b'"production_covariance_model_version":"factor-covariance-v1","refused_outputs":[],'
        b'"residual_model_version":"shrunk-residual-v1","return_series_version":"daily-base-return-v1",'
        b'"rights_manifest_versions":["synthetic-rights-v1"],"scenario_library_version":null,'
        b'"schema_version":"analytical_snapshot_manifest_v1","security_master_mapping_version":"security-master-v1",'
        b'"snapshot_status":"BLESSED","surface_model_version":null,'
        b'"valuation_cut":{"capture_end_utc":"2026-07-24T20:20:00Z","capture_start_utc":"2026-07-24T20:15:00Z",'
        b'"display_timezone":"America/New_York","target_cut_utc":"2026-07-24T20:15:00Z"},'
        b'"warnings":["ASYNCHRONOUS_CUT"]}'
    )

    assert canonical_json_bytes(body) == expected
    manifest = create_manifest(body)
    assert manifest.snapshot_id == (
        "e549e47b729877423cc7c0ab385a8e7abdcde104b5f58532b808235a33a8d9f8"
    )
    assert len(manifest.snapshot_id) == 64
    verify_manifest(manifest)


def test_manifest_identity_is_mapping_order_stable_and_identity_field_sensitive():
    body = _body()
    reversed_mapping = dict(reversed(tuple(body.model_dump(mode="python").items())))
    reordered_body = AnalyticalSnapshotManifestBodyV1(**reversed_mapping)
    assert create_manifest(reordered_body).snapshot_id == create_manifest(body).snapshot_id

    mutations = (
        {"book_generation": 8},
        {"included_account_ids": ("account-b", "account-a")},
        {
            "canonical_book_ref": body.canonical_book_ref.model_copy(
                update={"digest": "6" * 64}
            ),
            "canonical_book_hash": "6" * 64,
        },
        {"position_hash": "6" * 64},
        {"analytical_config_hash": "6" * 64},
        {"application_commit": "different-commit"},
        {"warnings": ("DIFFERENT_WARNING",)},
        {
            "outputs": (
                body.outputs[0].model_copy(
                    update={
                        "object_ref": body.outputs[0].object_ref.model_copy(
                            update={"digest": "6" * 64}
                        )
                    }
                ),
            )
        },
    )
    original = create_manifest(body).snapshot_id
    for mutation in mutations:
        assert create_manifest(_body(**mutation)).snapshot_id != original


@pytest.mark.parametrize(
    "forbidden_field",
    ["mandate_version", "limit_evaluation", "published_at", "run_id", "freshness"],
)
def test_manifest_body_forbids_non_identity_publication_and_mandate_fields(forbidden_field):
    values = _body().model_dump(mode="python")
    values[forbidden_field] = "forbidden"
    with pytest.raises(ValueError):
        AnalyticalSnapshotManifestBodyV1(**values)


def test_manifest_direct_wrong_id_and_malformed_digests_are_rejected():
    body = _body()
    with pytest.raises(ValueError, match="snapshot ID"):
        AnalyticalSnapshotManifestV1(snapshot_id="0" * 64, body=body)

    for digest in ("a" * 12, "A" * 64, "g" * 64):
        with pytest.raises(ValueError):
            ArtifactRefV1(
                hash_algorithm="sha256",
                digest=digest,
                byte_length=1,
                media_type="application/octet-stream",
                schema_version="opaque_v1",
            )


def test_manifest_parse_dispatches_schema_and_rejects_ambiguous_json_before_models():
    canonical = canonical_json_bytes(create_manifest(_body()))
    assert parse_manifest(canonical) == create_manifest(_body())

    duplicate = b'{"snapshot_id":"' + b"0" * 64 + b'",' + canonical[1:]
    with pytest.raises(DuplicateJSONKeyError, match="snapshot_id"):
        parse_manifest(duplicate)

    nonfinite = canonical.replace(b'"normalized_nlv":"1000.00"', b'"normalized_nlv":NaN')
    with pytest.raises(NonFiniteJSONConstantError, match="NaN"):
        parse_manifest(nonfinite)

    unknown_schema = canonical.replace(
        b'"schema_version":"analytical_snapshot_manifest_v1"',
        b'"schema_version":"analytical_snapshot_manifest_v2"',
    )
    with pytest.raises(UnsupportedManifestSchemaError, match="v2"):
        parse_manifest(unknown_schema)

    with pytest.raises(UnsupportedManifestSchemaError, match="missing"):
        parse_manifest(b'{"snapshot_id":"' + b"0" * 64 + b'","body":{}}')

    with pytest.raises(NonCanonicalManifestError):
        parse_manifest(b" " + canonical)


def test_manifest_schema_algorithm_collections_and_status_are_strict():
    body = _body()
    for change in (
        {"hash_algorithm": "sha512"},
        {"canonicalization_version": "unknown"},
        {"legacy_book_ref": "ABCDEF012345"},
        {"input_artifacts": (body.input_artifacts[0], body.input_artifacts[0])},
        {"outputs": (body.outputs[0], body.outputs[0])},
        {"gates": (body.gates[0], body.gates[0])},
        {"warnings": ("Z_WARNING", "A_WARNING")},
        {"snapshot_status": SnapshotStatus.DEGRADED, "refused_outputs": ()},
        {"snapshot_status": SnapshotStatus.BLESSED, "refused_outputs": ("TAIL",)},
        {
            "gates": (
                body.gates[0].model_copy(update={"status": GateStatus.FAILED}),
            )
        },
    ):
        with pytest.raises(ValueError):
            _body(**change)


def test_manifest_binds_the_exact_canonical_book_object_and_all_input_rights_versions():
    with pytest.raises(ValueError, match="canonical book hash"):
        _body(canonical_book_hash="6" * 64)

    with pytest.raises(ValueError, match="rights manifest"):
        _body(rights_manifest_versions=("different-rights-v1",))


def test_display_prefix_is_pure_and_never_accepted_as_identity():
    first = "abcdef012345" + "0" * 52
    second = "abcdef012345" + "1" * 52
    assert first != second
    assert snapshot_display_prefix(first) == snapshot_display_prefix(second) == "abcdef012345"
    with pytest.raises(ValueError):
        snapshot_display_prefix("abcdef012345")
