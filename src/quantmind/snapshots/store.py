"""Crash-aware immutable filesystem storage for analytical snapshots."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from quantmind.snapshots.contracts import FrozenContractBase, SnapshotStatus, canonical_json_bytes
from quantmind.snapshots.input_artifacts import (
    ArtifactRefV1,
    revalidate_frozen_contract,
    validate_artifact_ref,
)
from quantmind.snapshots.manifest import (
    AnalyticalSnapshotManifestV1,
    parse_manifest,
)


_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_WRITE_CHUNK_SIZE: Final = 64 * 1024


class SnapshotStoreError(RuntimeError):
    """Base class for immutable store failures."""


class SnapshotVerificationError(SnapshotStoreError):
    pass


class ArtifactNotFoundError(SnapshotVerificationError):
    pass


class NonRegularSnapshotFileError(SnapshotVerificationError):
    pass


class ArtifactLengthMismatchError(SnapshotVerificationError):
    pass


class ArtifactDigestMismatchError(SnapshotVerificationError):
    pass


class ManifestFilenameMismatchError(SnapshotVerificationError):
    pass


class NoClobberPublicationError(SnapshotStoreError):
    pass


class SnapshotDurabilityError(SnapshotStoreError):
    pass


class VerifiedSnapshotV1(FrozenContractBase):
    snapshot_id: str
    status: SnapshotStatus
    manifest: AnalyticalSnapshotManifestV1


class SnapshotRejectionV1(FrozenContractBase):
    snapshot_id: str
    error_code: str
    detail: str


class ActiveSnapshotResolutionV1(FrozenContractBase):
    requested_snapshot_id: str | None
    resolved_snapshot_id: str | None
    resolved_status: SnapshotStatus | None
    fallback_used: bool
    failures: tuple[SnapshotRejectionV1, ...]


FaultInjector = Callable[[str, Path], None]
SnapshotVerifier = Callable[[str], VerifiedSnapshotV1]


def _require_full_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


class SnapshotStore:
    """One no-clobber writer for content-addressed objects and manifests."""

    def __init__(
        self,
        root: Path,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.root = Path(root)
        self._fault_injector = fault_injector

    def _inject(self, stage: str, path: Path) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, path)

    def _object_path(self, digest: str) -> Path:
        digest = _require_full_digest(digest, "artifact digest")
        return (
            self.root
            / "snapshots"
            / "objects"
            / "sha256"
            / digest[:2]
            / digest
        )

    def _manifest_path(self, snapshot_id: str) -> Path:
        snapshot_id = _require_full_digest(snapshot_id, "snapshot ID")
        return (
            self.root
            / "snapshots"
            / "manifests"
            / "analytical_snapshot_manifest_v1"
            / snapshot_id[:2]
            / f"{snapshot_id}.json"
        )

    @staticmethod
    def _read_regular_bytes(path: Path) -> bytes:
        flags = os.O_RDONLY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        flags |= nofollow
        try:
            if not nofollow and path.is_symlink():
                raise NonRegularSnapshotFileError(f"snapshot path is a symlink: {path}")
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise ArtifactNotFoundError(f"snapshot file is missing: {path}") from error
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.EISDIR}:
                raise NonRegularSnapshotFileError(
                    f"snapshot path is not a regular file: {path}"
                ) from error
            raise SnapshotStoreError(f"cannot open snapshot file: {path}") from error

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise NonRegularSnapshotFileError(
                    f"snapshot path is not a regular file: {path}"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, _WRITE_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError as error:
            raise SnapshotStoreError(f"cannot read snapshot file: {path}") from error
        finally:
            os.close(descriptor)

    @classmethod
    def _read_verified_file(
        cls,
        path: Path,
        *,
        expected_digest: str,
        expected_length: int,
    ) -> bytes:
        payload = cls._read_regular_bytes(path)
        if len(payload) != expected_length:
            raise ArtifactLengthMismatchError(
                f"snapshot file length mismatch at {path}: "
                f"expected {expected_length}, got {len(payload)}"
            )
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_digest:
            raise ArtifactDigestMismatchError(
                f"snapshot file digest mismatch at {path}: "
                f"expected {expected_digest}, got {actual}"
            )
        return payload

    @staticmethod
    def _link_no_clobber(source: Path, target: Path) -> None:
        os.link(source, target, follow_symlinks=False)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _existing_target_is_verified(
        self,
        target: Path,
        *,
        expected_digest: str,
        expected_length: int,
    ) -> bool:
        try:
            self._read_verified_file(
                target,
                expected_digest=expected_digest,
                expected_length=expected_length,
            )
        except ArtifactNotFoundError:
            return False
        return True

    def _complete_directory_barrier(self, target: Path) -> None:
        self._inject("before_directory_fsync", target)
        try:
            self._fsync_directory(target.parent)
        except OSError as error:
            raise SnapshotDurabilityError(
                f"directory fsync failed after immutable publication: {target.parent}"
            ) from error

    def _publish_immutable(
        self,
        target: Path,
        payload: bytes,
        *,
        expected_digest: str,
    ) -> None:
        expected_digest = _require_full_digest(expected_digest, "content digest")
        expected_length = len(payload)
        if self._existing_target_is_verified(
            target,
            expected_digest=expected_digest,
            expected_length=expected_length,
        ):
            self._complete_directory_barrier(target)
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temp = Path(temp_name)
        published = False
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                self._inject("before_write", temp)
                for offset in range(0, len(payload), _WRITE_CHUNK_SIZE):
                    chunk = payload[offset : offset + _WRITE_CHUNK_SIZE]
                    written = handle.write(chunk)
                    if written != len(chunk):
                        raise SnapshotStoreError(
                            f"short write to immutable snapshot temp: {temp}"
                        )
                    self._inject("after_write_chunk", temp)
                handle.flush()
                self._inject("before_file_fsync", temp)
                try:
                    os.fsync(handle.fileno())
                except OSError as error:
                    raise SnapshotDurabilityError(
                        f"file fsync failed for immutable snapshot temp: {temp}"
                    ) from error

            self._inject("before_reread", temp)
            self._read_verified_file(
                temp,
                expected_digest=expected_digest,
                expected_length=expected_length,
            )
            self._inject("before_link", target)
            try:
                self._link_no_clobber(temp, target)
                published = True
            except FileExistsError:
                self._read_verified_file(
                    target,
                    expected_digest=expected_digest,
                    expected_length=expected_length,
                )
            except OSError as error:
                raise NoClobberPublicationError(
                    f"filesystem cannot atomically publish without clobbering: {target}"
                ) from error

            temp.unlink()
            self._complete_directory_barrier(target)
        finally:
            # If publication happened and the directory barrier failed, the target is a valid
            # orphan. It must remain available for a later idempotent verification.
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                if not published:
                    raise

    def put_bytes(
        self,
        payload: bytes,
        *,
        media_type: str,
        schema_version: str,
    ) -> ArtifactRefV1:
        if not isinstance(payload, bytes):
            raise TypeError("immutable artifact payload must be bytes")
        digest = hashlib.sha256(payload).hexdigest()
        reference = ArtifactRefV1(
            hash_algorithm="sha256",
            digest=digest,
            byte_length=len(payload),
            media_type=media_type,
            schema_version=schema_version,
        )
        self._publish_immutable(
            self._object_path(digest), payload, expected_digest=digest
        )
        return reference

    def put_canonical(
        self,
        value: FrozenContractBase,
        *,
        media_type: str,
    ) -> ArtifactRefV1:
        if not isinstance(value, FrozenContractBase):
            raise TypeError("canonical artifact must be a frozen analytical contract")
        validated_value = revalidate_frozen_contract(value)
        schema_version = getattr(
            validated_value, "schema_version", type(validated_value).__name__
        )
        if not isinstance(schema_version, str):
            raise ValueError("canonical artifact schema version must be a string")
        return self.put_bytes(
            canonical_json_bytes(validated_value),
            media_type=media_type,
            schema_version=schema_version,
        )

    def read_verified_artifact(self, reference: ArtifactRefV1) -> bytes:
        reference = validate_artifact_ref(reference)
        return self._read_verified_file(
            self._object_path(reference.digest),
            expected_digest=reference.digest,
            expected_length=reference.byte_length,
        )

    @staticmethod
    def _all_references(
        manifest: AnalyticalSnapshotManifestV1,
    ) -> tuple[ArtifactRefV1, ...]:
        return (
            manifest.body.canonical_book_ref,
            *(binding.object_ref for binding in manifest.body.input_artifacts),
            *(binding.object_ref for binding in manifest.body.outputs),
        )

    def _verify_manifest_references(
        self, manifest: AnalyticalSnapshotManifestV1
    ) -> None:
        for reference in self._all_references(manifest):
            self.read_verified_artifact(reference)

    def put_manifest(self, manifest: AnalyticalSnapshotManifestV1) -> Path:
        if not isinstance(manifest, AnalyticalSnapshotManifestV1):
            raise TypeError("manifest must be AnalyticalSnapshotManifestV1")
        payload = canonical_json_bytes(manifest)
        validated_manifest = parse_manifest(payload)
        self._verify_manifest_references(validated_manifest)
        envelope_digest = hashlib.sha256(payload).hexdigest()
        target = self._manifest_path(validated_manifest.snapshot_id)
        self._publish_immutable(target, payload, expected_digest=envelope_digest)
        return target

    def read_verified_manifest(
        self, snapshot_id: str
    ) -> AnalyticalSnapshotManifestV1:
        snapshot_id = _require_full_digest(snapshot_id, "snapshot ID")
        path = self._manifest_path(snapshot_id)
        payload = self._read_regular_bytes(path)
        manifest = parse_manifest(payload)
        if manifest.snapshot_id != snapshot_id:
            raise ManifestFilenameMismatchError(
                f"manifest filename ID {snapshot_id} does not match embedded ID "
                f"{manifest.snapshot_id}"
            )
        self._verify_manifest_references(manifest)
        return manifest

    def verify_snapshot(
        self,
        snapshot_id: str,
        *,
        required_output_roles: tuple[str, ...] = (),
    ) -> VerifiedSnapshotV1:
        manifest = self.read_verified_manifest(snapshot_id)
        if len(required_output_roles) != len(set(required_output_roles)) or any(
            not role.strip() for role in required_output_roles
        ):
            raise ValueError("required output roles must be nonblank and unique")
        available_roles = {binding.logical_role for binding in manifest.body.outputs}
        missing_roles = tuple(
            role for role in required_output_roles if role not in available_roles
        )
        if missing_roles:
            raise SnapshotVerificationError(
                f"required output role is missing: {', '.join(missing_roles)}"
            )
        return VerifiedSnapshotV1(
            snapshot_id=manifest.snapshot_id,
            status=manifest.body.snapshot_status,
            manifest=manifest,
        )


def select_last_good(
    active_snapshot_id: str | None,
    prior_blessed_snapshot_ids: Sequence[str],
    verify: SnapshotVerifier,
) -> ActiveSnapshotResolutionV1:
    """Select from caller-owned publication order without walking or mutating storage."""

    failures: list[SnapshotRejectionV1] = []

    def attempt(snapshot_id: str) -> VerifiedSnapshotV1 | None:
        try:
            verified = verify(snapshot_id)
            if not isinstance(verified, VerifiedSnapshotV1):
                raise TypeError("verifier must return VerifiedSnapshotV1")
            if verified.snapshot_id != snapshot_id:
                raise SnapshotVerificationError(
                    "verifier returned a different snapshot identity"
                )
            return verified
        except Exception as error:
            failures.append(
                SnapshotRejectionV1(
                    snapshot_id=snapshot_id,
                    error_code=type(error).__name__,
                    detail=str(error) or type(error).__name__,
                )
            )
            return None

    if active_snapshot_id is not None:
        active = attempt(active_snapshot_id)
        if active is not None and active.status in {
            SnapshotStatus.BLESSED,
            SnapshotStatus.DEGRADED,
        }:
            return ActiveSnapshotResolutionV1(
                requested_snapshot_id=active_snapshot_id,
                resolved_snapshot_id=active.snapshot_id,
                resolved_status=active.status,
                fallback_used=False,
                failures=tuple(failures),
            )

    for candidate_id in prior_blessed_snapshot_ids:
        candidate = attempt(candidate_id)
        if candidate is None:
            continue
        if candidate.status is not SnapshotStatus.BLESSED:
            failures.append(
                SnapshotRejectionV1(
                    snapshot_id=candidate_id,
                    error_code="NOT_BLESSED",
                    detail="fallback candidates must be published BLESSED snapshots",
                )
            )
            continue
        return ActiveSnapshotResolutionV1(
            requested_snapshot_id=active_snapshot_id,
            resolved_snapshot_id=candidate.snapshot_id,
            resolved_status=candidate.status,
            fallback_used=True,
            failures=tuple(failures),
        )

    return ActiveSnapshotResolutionV1(
        requested_snapshot_id=active_snapshot_id,
        resolved_snapshot_id=None,
        resolved_status=None,
        fallback_used=False,
        failures=tuple(failures),
    )


__all__ = [
    "ActiveSnapshotResolutionV1",
    "ArtifactDigestMismatchError",
    "ArtifactLengthMismatchError",
    "ArtifactNotFoundError",
    "ManifestFilenameMismatchError",
    "NoClobberPublicationError",
    "NonRegularSnapshotFileError",
    "SnapshotDurabilityError",
    "SnapshotStore",
    "SnapshotStoreError",
    "SnapshotVerificationError",
    "SnapshotRejectionV1",
    "VerifiedSnapshotV1",
    "select_last_good",
]
