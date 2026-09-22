from __future__ import annotations

from pathlib import Path

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.hashing import sha256_model
from psrc.contract.models import RunPolicy, SandboxMode, StrategyKind
from psrc.runtime.artifacts import ArtifactStore
from psrc.runtime.orchestrator import run_trainable
from psrc.runtime.training import build_training_input_evidence
from psrc.strategies.catalog import TrainableExample, supervised_examples


def test_supervised_catalog_contains_six_distinct_algorithms(tmp_path: Path) -> None:
    examples = supervised_examples()
    assert len(examples) == 6
    assert len({example.manifest.strategy_id for example in examples}) == 6
    assert {example.manifest.kind for example in examples} == {StrategyKind.SUPERVISED}

    algorithms = set()
    for index, example in enumerate(examples):
        strategy = example.factory()
        store = ArtifactStore(tmp_path / f"artifacts-{index}")
        artifact = strategy.train(example.training, store)
        algorithms.add(artifact.metadata["algorithm"])
    assert len(algorithms) == 6


@pytest.mark.parametrize(
    "example", supervised_examples(), ids=lambda item: item.manifest.strategy_id
)
def test_every_supervised_strategy_trains_reloads_and_backtests(
    example: TrainableExample, tmp_path: Path
) -> None:
    strategy = example.factory()
    plan = compile_run(
        run_id=f"test.{strategy.manifest.strategy_id}",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
        training_input_evidence_sha256=sha256_model(
            build_training_input_evidence(example.training)
        ),
    )
    report = run_trainable(
        plan=plan,
        strategy=strategy,
        training=example.training,
        events=example.events,
        engine=ReferenceEngine(),
        store=ArtifactStore(tmp_path / "artifacts"),
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert report.status == "succeeded"
    assert len(report.artifacts) == 1
    assert report.artifacts[0].training_request_sha256 == example.training.request_sha256
    assert "TRAINING" in report.lifecycle
    assert "ARTIFACT_LOADED" in report.lifecycle
    assert report.metrics.decisions == len(example.events)
