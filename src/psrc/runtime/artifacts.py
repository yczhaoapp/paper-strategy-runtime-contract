from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

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


class ArtifactStore:
    """Content-verified local artifact store with path traversal protection."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if os.name == "nt" and not str(self.root).startswith("\\\\?\\"):
            value = str(self.root)
            self.root = Path(
                "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
            )
        self.root.mkdir(parents=True, exist_ok=True)

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

    @staticmethod
    def _fail(
        *,
        run_id: str,
        strategy_id: str,
        code: ErrorCode,
        message: str,
        details: dict[str, object],
    ) -> None:
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
