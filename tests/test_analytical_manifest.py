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
    ManifestIdentityError,
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


def _input_binding(**changes):
    values = {
        "logical_role": "NORMALIZED_MARKS",
        "logical_id": "marks-at-cut",
        "representation": InputRepresentation.NORMALIZED_INPUT,
        "object_ref": _ref(
            INPUT_DIGEST,
            schema_version="normalized_marks_v1",
            byte_length=17,
        ),
        "source": "synthetic-fixture",
        "provider": "quantmind",
        "entitlement_reference": "synthetic-local-entitlement",
        "entitlement_version": "synthetic-entitlement-v1",
        "rights_mode": ArtifactRightsMode.RAW_ALLOWED,
        "rights_manifest_version": "synthetic-rights-v1",
        "reproducibility_class": ReproducibilityClass.NORMALIZED_ONLY,
    }
    values.update(changes)
    return bind_input_artifact(**values)


def _body(**changes) -> AnalyticalSnapshotManifestBodyV1:
    input_binding = _input_binding()
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
        "corporate_action_version": "corporate-actions-v1",
        "calendar_version": "exchange-calendars-v1",
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
        "policy_evidence": (
            _policy_evidence("OUTPUT", "book-xray", "BOOK_RECONCILIATION"),
        ),
        "warnings": ("ASYNCHRONOUS_CUT",),
        "refused_outputs": (),
        "outputs": (output_binding,),
    }
    values.update(changes)
    return AnalyticalSnapshotManifestBodyV1(**values)


def _policy_evidence(subject_kind: str, subject_id: str, gate_code: str) -> dict[str, str]:
    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "gate_code": gate_code,
    }


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


@pytest.mark.parametrize(
    "rights_mode",
    [
        ArtifactRightsMode.PROVENANCE_ONLY,
        "RAW_ALLOWED",
        "PROVENANCE_ONLY",
        object(),
        None,
    ],
)
def test_retained_raw_input_bytes_requires_the_exact_allowing_enum(rights_mode):
    with pytest.raises((TypeError, ValueError)):
        retained_raw_input_bytes(b"vendor bytes", rights_mode=rights_mode)


def test_entitlement_evidence_is_explicitly_paired_or_explicitly_not_applicable():
    no_entitlement = _input_binding(
        entitlement_reference=None,
        entitlement_version=None,
    )
    assert no_entitlement.entitlement_reference is None
    assert no_entitlement.entitlement_version is None

    with pytest.raises(ValueError, match="entitlement"):
        _input_binding(entitlement_reference=None)
    with pytest.raises(ValueError, match="entitlement"):
        _input_binding(entitlement_version=None)

    values = no_entitlement.model_dump(mode="python")
    values.pop("entitlement_reference")
    with pytest.raises(ValueError):
        type(no_entitlement)(**values)


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
        "entitlement_reference": "vendor-account-entitlement",
        "entitlement_version": "vendor-entitlement-v1",
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
        b'"base_currency":"USD","book_generation":7,"book_id":"synthetic-book","calendar_version":"exchange-calendars-v1",'
        b'"canonical_book_hash":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"canonical_book_ref":{"byte_length":23,"digest":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"hash_algorithm":"sha256","media_type":"application/json","schema_version":"canonical_book_v1"},'
        b'"canonicalization_version":"quantmind_canonical_json_v1","corporate_action_version":"corporate-actions-v1",'
        b'"factor_taxonomy_version":"factor-taxonomy-v1",'
        b'"gates":[{"evidence":["normalized minus source NLV is USD 0.00"],"gate_code":"BOOK_RECONCILIATION",'
        b'"recovery_action":"Resolve account or cash mismatches","recovery_class":"USER_RESOLVABLE","status":"PASSED"}],'
        b'"hash_algorithm":"sha256","included_account_ids":["account-a","account-b"],'
        b'"input_artifacts":[{"entitlement_reference":"synthetic-local-entitlement","entitlement_version":"synthetic-entitlement-v1",'
        b'"logical_id":"marks-at-cut","logical_role":"NORMALIZED_MARKS",'
        b'"object_ref":{"byte_length":17,"digest":"2222222222222222222222222222222222222222222222222222222222222222",'
        b'"hash_algorithm":"sha256","media_type":"application/json","schema_version":"normalized_marks_v1"},'
        b'"provider":"quantmind","representation":"NORMALIZED_INPUT","reproducibility_class":"NORMALIZED_ONLY",'
        b'"rights_manifest_version":"synthetic-rights-v1","rights_mode":"RAW_ALLOWED","source":"synthetic-fixture"}],'
        b'"latent_factor_model_version":null,"legacy_book_ref":"abcdef012345","normalized_nlv":"1000.00",'
        b'"option_pricer_version":null,"outputs":[{"logical_id":"book-xray","logical_role":"XRAY_READ_MODEL",'
        b'"model_version":"xray-model-v1","object_ref":{"byte_length":19,"digest":"3333333333333333333333333333333333333333333333333333333333333333",'
        b'"hash_algorithm":"sha256","media_type":"application/json","schema_version":"xray_read_model_v1"}}],'
        b'"policy_evidence":[{"gate_code":"BOOK_RECONCILIATION","subject_id":"book-xray","subject_kind":"OUTPUT"}],'
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
        "b291efcc2bcaf40218ed3494a0363121f5454c39f672913a486cc50a15d5480f"
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
        {"calendar_version": "exchange-calendars-v2"},
        {"corporate_action_version": "corporate-actions-v2"},
        {
            "input_artifacts": (
                _input_binding(entitlement_reference="different-entitlement"),
            )
        },
        {
            "input_artifacts": (
                _input_binding(entitlement_version="synthetic-entitlement-v2"),
            )
        },
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

    nullable_versions = _body(corporate_action_version=None, calendar_version=None)
    assert create_manifest(nullable_versions).snapshot_id != original

    for required_nullable_field in ("corporate_action_version", "calendar_version"):
        values = nullable_versions.model_dump(mode="python")
        values.pop(required_nullable_field)
        with pytest.raises(ValueError):
            AnalyticalSnapshotManifestBodyV1(**values)


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


def test_parse_manifest_preserves_identity_error_for_a_full_wrong_snapshot_id():
    manifest = create_manifest(_body())
    wrong_id_payload = canonical_json_bytes(
        {"snapshot_id": "0" * 64, "body": manifest.body}
    )
    with pytest.raises(ManifestIdentityError, match="does not match"):
        parse_manifest(wrong_id_payload)


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


def test_manifest_policy_evidence_is_required_typed_and_identity_bearing():
    output_policy = _policy_evidence("OUTPUT", "book-xray", "BOOK_RECONCILIATION")
    body = _body(policy_evidence=(output_policy,))
    assert body.policy_evidence[0].subject_id == "book-xray"

    values = body.model_dump(mode="python")
    values.pop("policy_evidence")
    with pytest.raises(ValueError):
        AnalyticalSnapshotManifestBodyV1(**values)

    with pytest.raises(ValueError):
        _body(
            policy_evidence=(
                _policy_evidence("UNTYPED_SUBJECT", "book-xray", "BOOK_RECONCILIATION"),
            )
        )

    alternate_gate = body.gates[0].model_copy(update={"gate_code": "OUTPUT_COMPLETENESS"})
    gates = tuple(sorted((body.gates[0], alternate_gate), key=lambda gate: gate.gate_code))
    alternate_policy = _policy_evidence("OUTPUT", "book-xray", "OUTPUT_COMPLETENESS")
    original = create_manifest(_body(gates=gates, policy_evidence=(output_policy,)))
    alternate = create_manifest(_body(gates=gates, policy_evidence=(alternate_policy,)))
    assert alternate.snapshot_id != original.snapshot_id


def test_manifest_policy_evidence_covers_retained_outputs_and_refused_capabilities():
    output_policy = _policy_evidence("OUTPUT", "book-xray", "BOOK_RECONCILIATION")
    warned_gate = _body().gates[0].model_copy(update={"status": GateStatus.WARNED})
    assert _body(gates=(warned_gate,), policy_evidence=(output_policy,)).gates[0].status is (
        GateStatus.WARNED
    )

    with pytest.raises(ValueError, match="retained output"):
        _body(policy_evidence=())
    with pytest.raises(ValueError, match="gate"):
        _body(
            policy_evidence=(
                _policy_evidence("OUTPUT", "book-xray", "UNKNOWN_GATE"),
            )
        )

    refused_gate = GateEvidenceV1(
        gate_code="TAIL_POLICY",
        status=GateStatus.REFUSED,
        recovery_class=RecoveryClass.MODEL_OWNER_UPDATE,
        evidence=("tail model is unavailable",),
        recovery_action="Publish a supported tail model",
    )
    gates = tuple(sorted((_body().gates[0], refused_gate), key=lambda gate: gate.gate_code))
    coherent = _body(
        snapshot_status=SnapshotStatus.DEGRADED,
        gates=gates,
        refused_outputs=("TAIL",),
        policy_evidence=(
            _policy_evidence("CAPABILITY", "TAIL", "TAIL_POLICY"),
            output_policy,
        ),
    )
    assert coherent.snapshot_status is SnapshotStatus.DEGRADED

    with pytest.raises(ValueError, match="refused capability"):
        _body(
            snapshot_status=SnapshotStatus.DEGRADED,
            gates=gates,
            refused_outputs=("TAIL",),
            policy_evidence=(output_policy,),
        )
    with pytest.raises(ValueError, match="REFUSED"):
        _body(
            snapshot_status=SnapshotStatus.DEGRADED,
            gates=gates,
            refused_outputs=("TAIL",),
            policy_evidence=(
                _policy_evidence("CAPABILITY", "TAIL", "BOOK_RECONCILIATION"),
                output_policy,
            ),
        )
    with pytest.raises(ValueError, match="blessed"):
        _body(gates=gates, policy_evidence=(output_policy,))


def test_manifest_policy_evidence_is_ordered_unique_and_status_coherent():
    output_policy = _policy_evidence("OUTPUT", "book-xray", "BOOK_RECONCILIATION")
    duplicate = (output_policy, output_policy)
    with pytest.raises(ValueError, match="unique"):
        _body(policy_evidence=duplicate)

    second_output = _body().outputs[0].model_copy(
        update={"logical_role": "ATTRIBUTION_READ_MODEL", "logical_id": "attribution"}
    )
    outputs = tuple(
        sorted(
            (_body().outputs[0], second_output),
            key=lambda output: (output.logical_role, output.logical_id),
        )
    )
    ordered = (
        _policy_evidence("OUTPUT", "attribution", "BOOK_RECONCILIATION"),
        output_policy,
    )
    with pytest.raises(ValueError, match="sorted"):
        _body(outputs=outputs, policy_evidence=tuple(reversed(ordered)))

    refused_book_gate = _body().gates[0].model_copy(update={"status": GateStatus.REFUSED})
    tail_gate = refused_book_gate.model_copy(update={"gate_code": "TAIL_POLICY"})
    gates = tuple(sorted((refused_book_gate, tail_gate), key=lambda gate: gate.gate_code))
    with pytest.raises(ValueError, match="retained output"):
        _body(
            snapshot_status=SnapshotStatus.DEGRADED,
            gates=gates,
            refused_outputs=("TAIL",),
            policy_evidence=(
                _policy_evidence("CAPABILITY", "TAIL", "TAIL_POLICY"),
                output_policy,
            ),
        )


def test_manifest_binds_the_exact_canonical_book_object_and_all_input_rights_versions():
    with pytest.raises(ValueError, match="canonical book hash"):
        _body(canonical_book_hash="6" * 64)

    with pytest.raises(ValueError, match="rights manifest"):
        _body(rights_manifest_versions=("different-rights-v1",))


def test_public_manifest_functions_revalidate_bypassed_top_level_and_nested_models():
    body = _body()
    invalid_top_level = body.model_copy(update={"base_currency": "US"})
    with pytest.raises(ValueError):
        create_manifest(invalid_top_level)

    invalid_ref = body.canonical_book_ref.model_copy(update={"hash_algorithm": "sha512"})
    invalid_nested = body.model_copy(update={"canonical_book_ref": invalid_ref})
    with pytest.raises(ValueError):
        create_manifest(invalid_nested)

    missing_values = body.model_dump(mode="python")
    missing_values.pop("application_commit")
    invalid_constructed = AnalyticalSnapshotManifestBodyV1.model_construct(**missing_values)
    with pytest.raises(ValueError):
        create_manifest(invalid_constructed)

    valid_manifest = create_manifest(body)
    bypassed_manifest = valid_manifest.model_copy(update={"body": invalid_top_level})
    with pytest.raises(ValueError):
        verify_manifest(bypassed_manifest)

    nested_bypassed_manifest = valid_manifest.model_copy(update={"body": invalid_nested})
    with pytest.raises(ValueError):
        verify_manifest(nested_bypassed_manifest)

    missing_envelope = AnalyticalSnapshotManifestV1.model_construct(body=body)
    with pytest.raises(ValueError):
        verify_manifest(missing_envelope)


def test_public_input_helpers_revalidate_bypassed_contracts_and_artifact_refs():
    cut = _body().valuation_cut
    invalid_cut = cut.model_copy(update={"display_timezone": "Mars/Olympus_Mons"})
    with pytest.raises(ValueError):
        canonical_input_bytes(invalid_cut)

    invalid_ref = _ref(
        INPUT_DIGEST,
        schema_version="normalized_marks_v1",
        byte_length=17,
    ).model_copy(update={"hash_algorithm": "sha512"})
    with pytest.raises(ValueError):
        _input_binding(object_ref=invalid_ref)

    missing_ref = ArtifactRefV1.model_construct(
        hash_algorithm="sha256",
        byte_length=17,
        media_type="application/json",
        schema_version="normalized_marks_v1",
    )
    with pytest.raises(ValueError):
        _input_binding(object_ref=missing_ref)

    missing_cut = ValuationCutV1.model_construct(
        target_cut_utc=cut.target_cut_utc,
        capture_start_utc=cut.capture_start_utc,
        capture_end_utc=cut.capture_end_utc,
    )
    with pytest.raises(ValueError):
        canonical_input_bytes(missing_cut)


def test_display_prefix_is_pure_and_never_accepted_as_identity():
    first = "abcdef012345" + "0" * 52
    second = "abcdef012345" + "1" * 52
    assert first != second
    assert snapshot_display_prefix(first) == snapshot_display_prefix(second) == "abcdef012345"
    with pytest.raises(ValueError):
        snapshot_display_prefix("abcdef012345")
