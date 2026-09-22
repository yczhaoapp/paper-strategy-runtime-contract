from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Never, Protocol, cast
from weakref import WeakKeyDictionary

from pydantic import Field

from psrc.constants import CONTRACT_VERSION
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import ContractModel, Identifier
from psrc.sandbox.runtime import trusted_runtime_io


class ArtifactFile(ContractModel):
    logical_name: str
    media_type: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class ArtifactManifest(ContractModel):
    contract_version: str = CONTRACT_VERSION
    artifact_id: Identifier
    strategy_id: Identifier
    strategy_version: str
    artifact_kind: Literal["model", "policy", "state", "not_applicable"]
    framework: str
    created_at: datetime
    training_dataset_id: Identifier | None = None
    seed: int | None = None
    training_request_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    files: tuple[ArtifactFile, ...]
    metadata: dict[str, str] = Field(default_factory=dict)


class ArtifactIO(Protocol):
    """Minimal byte channel available to a strategy callback.

    The orchestrator supplies a capability implementation, not the authoritative
    :class:`ArtifactStore` service.  Keeping this as a protocol also lets trusted
    unit tests exercise strategy serialization directly with an ArtifactStore.
    """

    def save_bytes(
        self,
        *,
        run_id: str,
        artifact_id: str,
        strategy_id: str,
        strategy_version: str,
        artifact_kind: Literal["model", "policy", "state"],
        framework: str,
        logical_name: str,
        media_type: str,
        payload: bytes,
        training_dataset_id: str,
        seed: int,
        training_request_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactManifest: ...

    def load_bytes(
        self, *, run_id: str, strategy_id: str, manifest: ArtifactManifest
    ) -> dict[str, bytes]: ...


_AUTHORIZED_ROOTS: WeakKeyDictionary[ArtifactStore, Path] = WeakKeyDictionary()


class ArtifactStore:
    """Content-verified local artifact store with path traversal protection."""

    __slots__ = ("__weakref__",)

    def __init__(self, root: Path) -> None:
        authorized_root = root.resolve()
        if os.name == "nt" and not str(authorized_root).startswith("\\\\?\\"):
            value = str(authorized_root)
            authorized_root = Path(
                "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
            )
        authorized_root.mkdir(parents=True, exist_ok=True)
        _AUTHORIZED_ROOTS[self] = authorized_root

    @property
    def root(self) -> Path:
        """Return the runtime-authorized root without exposing a mutable authority field."""
        return _AUTHORIZED_ROOTS[self]

    @staticmethod
    def _validate_component(value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or len(path.parts) != 1
            or value in {"", ".", ".."}
            or "/" in value
            or "\\" in value
            or ":" in value
            or value.endswith((" ", "."))
        ):
            raise ValueError(f"artifact path component is unsafe: {value!r}")
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if value.split(".")[0].upper() in reserved:
            raise ValueError(f"reserved artifact path component: {value!r}")
        return value

    @trusted_runtime_io
    def save_bytes(
        self,
        *,
        run_id: str,
        artifact_id: str,
        strategy_id: str,
        strategy_version: str,
        artifact_kind: Literal["model", "policy", "state"],
        framework: str,
        logical_name: str,
        media_type: str,
        payload: bytes,
        training_dataset_id: str,
        seed: int,
        training_request_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactManifest:
        safe_artifact = self._validate_component(artifact_id)
        safe_name = self._validate_component(logical_name)
        if safe_name == "manifest.json":
            raise ValueError("artifact payload cannot replace the manifest")
        artifact_dir = self.root / safe_artifact
        if artifact_dir.is_symlink():
            raise ValueError("artifact directories cannot be symbolic links")
        digest = hashlib.sha256(payload).hexdigest()
        if artifact_dir.exists():
            manifest_path = artifact_dir / "manifest.json"
            try:
                existing = ArtifactManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                self._fail(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                    message="Existing artifact manifest cannot be validated",
                    details={
                        "artifact_id": artifact_id,
                        "cause": f"{type(exc).__name__}: {exc}",
                    },
                )
            expected_metadata = metadata or {}
            reusable = (
                existing.artifact_id == artifact_id
                and existing.strategy_id == strategy_id
                and existing.strategy_version == strategy_version
                and existing.artifact_kind == artifact_kind
                and existing.framework == framework
                and existing.training_dataset_id == training_dataset_id
                and existing.seed == seed
                and existing.training_request_sha256 == training_request_sha256
                and existing.metadata == expected_metadata
                and len(existing.files) == 1
                and existing.files[0].logical_name == logical_name
                and existing.files[0].media_type == media_type
                and existing.files[0].sha256 == digest
                and existing.files[0].size_bytes == len(payload)
            )
            if not reusable:
                self._fail(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                    message="Artifact ID already exists with different content or provenance",
                    details={"artifact_id": artifact_id, "incoming_sha256": digest},
                )
            self.load_bytes(run_id=run_id, strategy_id=strategy_id, manifest=existing)
            return existing
        manifest = ArtifactManifest(
            artifact_id=artifact_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            artifact_kind=artifact_kind,
            framework=framework,
            created_at=datetime.now(UTC),
            training_dataset_id=training_dataset_id,
            seed=seed,
            training_request_sha256=training_request_sha256,
            files=(
                ArtifactFile(
                    logical_name=logical_name,
                    media_type=media_type,
                    sha256=digest,
                    size_bytes=len(payload),
                ),
            ),
            metadata=metadata or {},
        )
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            (staging / safe_name).write_bytes(payload)
            (staging / "manifest.json").write_text(
                manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            staging.rename(artifact_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return manifest

    @trusted_runtime_io
    def load_bytes(
        self, *, run_id: str, strategy_id: str, manifest: ArtifactManifest
    ) -> dict[str, bytes]:
        artifact_dir = self.root / self._validate_component(manifest.artifact_id)
        if artifact_dir.is_symlink():
            raise ValueError("artifact directories cannot be symbolic links")
        if not artifact_dir.is_dir():
            self._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.ARTIFACT_NOT_FOUND,
                message=f"Artifact {manifest.artifact_id!r} is absent",
                details={"artifact_id": manifest.artifact_id},
            )
        loaded: dict[str, bytes] = {}
        for declared in manifest.files:
            target = artifact_dir / self._validate_component(declared.logical_name)
            if target.is_symlink():
                raise ValueError("artifact payload cannot be a symbolic link")
            if not target.is_file():
                self._fail(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    code=ErrorCode.ARTIFACT_NOT_FOUND,
                    message=f"Artifact file {declared.logical_name!r} is absent",
                    details={"artifact_id": manifest.artifact_id},
                )
            payload = target.read_bytes()
            if len(payload) != declared.size_bytes:
                self._fail(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                    message=f"Artifact file {declared.logical_name!r} failed size validation",
                    details={"expected": declared.size_bytes, "actual": len(payload)},
                )
            actual = hashlib.sha256(payload).hexdigest()
            if actual != declared.sha256:
                self._fail(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                    message=f"Artifact file {declared.logical_name!r} failed integrity validation",
                    details={"expected": declared.sha256, "actual": actual},
                )
            loaded[declared.logical_name] = payload
        return loaded

    @trusted_runtime_io
    def verify_manifest(
        self,
        *,
        run_id: str,
        strategy_id: str,
        strategy_version: str,
        candidate: ArtifactManifest,
    ) -> ArtifactManifest:
        """Load the canonical disk manifest and verify all bytes before strategy loading."""
        artifact_dir = self.root / self._validate_component(candidate.artifact_id)
        manifest_path = artifact_dir / "manifest.json"
        if artifact_dir.is_symlink() or manifest_path.is_symlink():
            self._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                message="Artifact manifest path cannot contain symbolic links",
                details={"artifact_id": candidate.artifact_id},
            )
        if not manifest_path.is_file():
            self._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.ARTIFACT_NOT_FOUND,
                message=f"Artifact {candidate.artifact_id!r} has no stored manifest",
                details={"artifact_id": candidate.artifact_id},
            )
        try:
            stored = ArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                message="Stored artifact manifest is invalid",
                details={
                    "artifact_id": candidate.artifact_id,
                    "cause": f"{type(exc).__name__}: {exc}",
                },
            )
        if stored != candidate:
            self._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                message="Returned artifact manifest differs from the stored manifest",
                details={"artifact_id": candidate.artifact_id},
            )
        identity_mismatches: dict[str, object] = {}
        if stored.strategy_id != strategy_id:
            identity_mismatches["strategy_id"] = {
                "expected": strategy_id,
                "actual": stored.strategy_id,
            }
        if stored.strategy_version != strategy_version:
            identity_mismatches["strategy_version"] = {
                "expected": strategy_version,
                "actual": stored.strategy_version,
            }
        if identity_mismatches:
            self._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.ARTIFACT_HASH_MISMATCH,
                message="Stored artifact identity does not match the executing strategy",
                details={"artifact_id": candidate.artifact_id, "mismatches": identity_mismatches},
            )
        self.load_bytes(run_id=run_id, strategy_id=strategy_id, manifest=stored)
        return stored

    @staticmethod
    def _fail(
        *,
        run_id: str,
        strategy_id: str,
        code: ErrorCode,
        message: str,
        details: dict[str, object],
    ) -> Never:
        raise ContractViolation(
            ContractError(
                run_id=run_id,
                stage=ErrorStage.ARTIFACT,
                code=code,
                message=message,
                strategy_id=strategy_id,
                details=details,
            )
        )


class _StrategyArtifactChannel:
    """Per-run capability that never exposes the trusted store object or root."""

    __slots__ = ("__weakref__",)

    def save_bytes(
        self,
        *,
        run_id: str,
        artifact_id: str,
        strategy_id: str,
        strategy_version: str,
        artifact_kind: Literal["model", "policy", "state"],
        framework: str,
        logical_name: str,
        media_type: str,
        payload: bytes,
        training_dataset_id: str,
        seed: int,
        training_request_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactManifest:
        values = {
            "run_id": run_id,
            "artifact_id": artifact_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "artifact_kind": artifact_kind,
            "framework": framework,
            "logical_name": logical_name,
            "media_type": media_type,
            "training_dataset_id": training_dataset_id,
        }
        for name, value in values.items():
            if type(value) is not str:
                raise TypeError(f"artifact channel {name} must be a plain string")
        if type(payload) is not bytes:
            raise TypeError("artifact channel payload must be plain bytes")
        if type(seed) is not int:
            raise TypeError("artifact channel seed must be a plain integer")
        if training_request_sha256 is not None and type(training_request_sha256) is not str:
            raise TypeError("artifact channel training_request_sha256 must be a plain string")
        if metadata is None:
            safe_metadata: dict[str, str] = {}
        else:
            if type(metadata) is not dict or any(
                type(key) is not str or type(value) is not str
                for key, value in metadata.items()
            ):
                raise TypeError("artifact channel metadata must be a plain string dictionary")
            safe_metadata = dict(metadata)
        state = _CHANNEL_STATES.get(self)
        if state is None:
            raise RuntimeError("artifact channel is not authorized")
        return state.store.save_bytes(
            run_id=run_id,
            artifact_id=artifact_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            artifact_kind=artifact_kind,
            framework=framework,
            logical_name=logical_name,
            media_type=media_type,
            payload=payload,
            training_dataset_id=training_dataset_id,
            seed=seed,
            training_request_sha256=training_request_sha256,
            metadata=safe_metadata,
        )

    def load_bytes(
        self, *, run_id: str, strategy_id: str, manifest: ArtifactManifest
    ) -> dict[str, bytes]:
        if type(run_id) is not str or type(strategy_id) is not str:
            raise TypeError("artifact channel identifiers must be plain strings")
        if type(manifest) is not ArtifactManifest:
            raise TypeError("artifact channel manifest must be the verified runtime model")
        state = _CHANNEL_STATES.get(self)
        if state is None:
            raise RuntimeError("artifact channel is not authorized")
        if state.expected_load != manifest:
            raise ValueError(
                "strategy attempted to load an artifact outside the verified load phase"
            )
        loaded = state.store.load_bytes(
            run_id=run_id,
            strategy_id=strategy_id,
            manifest=manifest,
        )
        state.observed_load = manifest
        return loaded


class _ChannelState:
    __slots__ = ("expected_load", "observed_load", "store")

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self.expected_load: ArtifactManifest | None = None
        self.observed_load: ArtifactManifest | None = None


_CHANNEL_STATES: WeakKeyDictionary[_StrategyArtifactChannel, _ChannelState] = WeakKeyDictionary()


def create_strategy_artifact_channel(store: ArtifactStore) -> ArtifactIO:
    """Create a single-run, independently mutable capability class.

    A strategy can alter its own Python class, but that class is unique to the
    run and is never used by the host for authoritative verification.
    """

    channel_type = type(
        f"_RunArtifactChannel_{id(store):x}",
        (_StrategyArtifactChannel,),
        {"__slots__": ()},
    )
    channel = cast(_StrategyArtifactChannel, channel_type())
    _CHANNEL_STATES[channel] = _ChannelState(store)
    return channel


def begin_verified_artifact_load(channel: ArtifactIO, manifest: ArtifactManifest) -> None:
    if not isinstance(channel, _StrategyArtifactChannel):
        raise RuntimeError("artifact channel is not authorized")
    state = _CHANNEL_STATES.get(channel)
    if state is None:
        raise RuntimeError("artifact channel is not authorized")
    state.expected_load = manifest
    state.observed_load = None


def require_verified_artifact_load(
    channel: ArtifactIO,
    manifest: ArtifactManifest,
    *,
    run_id: str,
    strategy_id: str,
) -> None:
    if not isinstance(channel, _StrategyArtifactChannel):
        raise RuntimeError("artifact channel is not authorized")
    state = _CHANNEL_STATES.get(channel)
    if state is None or state.expected_load != manifest or state.observed_load != manifest:
        ArtifactStore._fail(
            run_id=run_id,
            strategy_id=strategy_id,
            code=ErrorCode.ARTIFACT_HASH_MISMATCH,
            message="Strategy load callback did not read the host-verified artifact bytes",
            details={"artifact_id": manifest.artifact_id, "fallback_used": False},
        )
    state.expected_load = None
    state.observed_load = None
