from __future__ import annotations

import json
from pathlib import Path

import pytest

from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.runtime.artifacts import ArtifactStore


def test_artifact_round_trip_and_integrity(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    payload = json.dumps({"weights": [1.0, 2.0]}, sort_keys=True).encode()
    manifest = store.save_bytes(
        run_id="test.artifact",
        artifact_id="model-001",
        strategy_id="supervised.test",
        strategy_version="1.0.0",
        artifact_kind="model",
        framework="numpy",
        logical_name="model.json",
        media_type="application/json",
        payload=payload,
        training_dataset_id="synthetic.train",
        seed=7,
    )

    assert (
        store.load_bytes(run_id="test.artifact", strategy_id="supervised.test", manifest=manifest)[
            "model.json"
        ]
        == payload
    )

    (tmp_path / "artifacts" / "model-001" / "model.json").write_bytes(b"tampered")
    with pytest.raises(ContractViolation) as raised:
        store.load_bytes(run_id="test.artifact", strategy_id="supervised.test", manifest=manifest)
    assert raised.value.error.code == ErrorCode.ARTIFACT_HASH_MISMATCH


def test_artifact_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError):
        store.save_bytes(
            run_id="test.traversal",
            artifact_id="../escape",
            strategy_id="supervised.test",
            strategy_version="1.0.0",
            artifact_kind="model",
            framework="numpy",
            logical_name="model.json",
            media_type="application/json",
            payload=b"{}",
            training_dataset_id="synthetic.train",
            seed=0,
        )


def test_identical_content_addressed_artifact_is_idempotent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    arguments = {
        "run_id": "test.idempotent",
        "artifact_id": "same-model",
        "strategy_id": "supervised.test",
        "strategy_version": "1.0.0",
        "artifact_kind": "model",
        "framework": "numpy",
        "logical_name": "model.json",
        "media_type": "application/json",
        "payload": b'{"weight":1}',
        "training_dataset_id": "synthetic.train",
        "seed": 7,
    }
    first = store.save_bytes(**arguments)  # type: ignore[arg-type]
    second = store.save_bytes(**arguments)  # type: ignore[arg-type]
    assert second == first


def test_artifact_id_collision_with_different_content_fails(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    common = {
        "run_id": "test.collision",
        "artifact_id": "same-model",
        "strategy_id": "supervised.test",
        "strategy_version": "1.0.0",
        "artifact_kind": "model",
        "framework": "numpy",
        "logical_name": "model.json",
        "media_type": "application/json",
        "training_dataset_id": "synthetic.train",
        "seed": 7,
    }
    store.save_bytes(payload=b'{"weight":1}', **common)  # type: ignore[arg-type]
    with pytest.raises(ContractViolation) as raised:
        store.save_bytes(payload=b'{"weight":2}', **common)  # type: ignore[arg-type]
    assert raised.value.error.code == ErrorCode.ARTIFACT_HASH_MISMATCH
