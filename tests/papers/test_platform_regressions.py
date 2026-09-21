from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from psrc.contract.models import ResourcePolicy
from psrc.runtime.artifacts import ArtifactStore
from psrc.sandbox.container import ContainerMounts, DockerSandbox, docker_executable


def save(store: ArtifactStore, **changes: Any) -> Any:
    options: dict[str, Any] = dict(
        run_id="test.atomic",
        artifact_id="sha256-" + "1" * 64,
        strategy_id="test.model",
        strategy_version="1.0",
        artifact_kind="policy",
        framework="json",
        logical_name="policy.json",
        media_type="application/json",
        payload=b'{"weights": [1, 2]}',
        training_dataset_id="test.data",
        seed=7,
    )
    return store.save_bytes(**(options | changes))


def test_interrupted_manifest_write_never_publishes_partial_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path / "store")
    original = Path.write_text

    def interrupted(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "manifest.json":
            raise OSError("simulated disk interruption")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", interrupted)
        with pytest.raises(OSError, match="interruption"):
            save(store)
    assert list(store.root.iterdir()) == []
    manifest = save(store)
    assert store.load_bytes(run_id="test.atomic", strategy_id="test.model", manifest=manifest)


def test_artifact_store_supports_paths_longer_than_legacy_windows_limit(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / ("a" * 70) / ("b" * 70) / ("c" * 70))
    manifest = save(store)
    path = store.root / manifest.artifact_id / "manifest.json"
    assert len(str(path)) > 260
    assert path.is_file()
    assert save(store) == manifest  # repeated complete artifacts are safely reused.
    assert store.load_bytes(run_id="test.atomic", strategy_id="test.model", manifest=manifest)


@pytest.mark.parametrize(
    "name", ["../escape", "x/y", "x/", "x\\y", "C:drive", "NUL", "con.json", "trail."]
)
def test_artifact_names_are_portable_and_cannot_escape(name: str) -> None:
    with pytest.raises(ValueError):
        ArtifactStore._validate_component(name)


def test_manifest_name_is_reserved_for_metadata(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        save(ArtifactStore(tmp_path), logical_name="manifest.json")


def test_no_unix_identity_api_still_builds_unprivileged_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from psrc.contract.models import ResourcePolicy

    for name in ("getuid", "getgid", "geteuid"):
        monkeypatch.delattr(os, name, raising=False)
    invocation = DockerSandbox.command(
        policy=ResourcePolicy(),
        mounts=ContainerMounts(tmp_path, tmp_path, tmp_path),
        command=("--help",),
    )
    assert invocation[invocation.index("--user") + 1] == "65532:65532"
    assert DockerSandbox.current_process_attested() is False


def test_docker_command_uses_the_resolved_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/test/bin/docker")
    assert docker_executable() == "/test/bin/docker"
    invocation = DockerSandbox.command(
        policy=ResourcePolicy(),
        mounts=ContainerMounts(tmp_path, tmp_path, tmp_path),
        command=("--help",),
    )
    assert invocation[0] == "/test/bin/docker"


def test_strict_image_contains_every_non_tree_review_input() -> None:
    root = Path(__file__).resolve().parents[2]
    ignored = {
        line.strip()
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert ".github" not in ignored
    assert "COPY .github /app/.github" in dockerfile
    assert "NOTICE" in dockerfile
    assert ".gitattributes" in dockerfile
