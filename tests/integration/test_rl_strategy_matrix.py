from __future__ import annotations

from pathlib import Path

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import RunPolicy, SandboxMode, StrategyKind
from psrc.runtime.artifacts import ArtifactStore
from psrc.runtime.orchestrator import run_trainable
from psrc.strategies.catalog import TrainableExample, reinforcement_learning_examples


def test_rl_catalog_contains_six_distinct_algorithms(tmp_path: Path) -> None:
    examples = reinforcement_learning_examples()
    assert len(examples) == 6
    assert len({example.manifest.strategy_id for example in examples}) == 6
    assert {example.manifest.kind for example in examples} == {StrategyKind.REINFORCEMENT_LEARNING}

    algorithms = set()
    for index, example in enumerate(examples):
        strategy = example.factory()
        store = ArtifactStore(tmp_path / f"artifacts-{index}")
        artifact = strategy.train(example.training, store)
        algorithms.add(artifact.metadata["algorithm"])
    assert len(algorithms) == 6


@pytest.mark.parametrize(
    "example", reinforcement_learning_examples(), ids=lambda item: item.manifest.strategy_id
)
def test_every_rl_strategy_trains_reloads_and_backtests(
    example: TrainableExample, tmp_path: Path
) -> None:
    strategy = example.factory()
    plan = compile_run(
        run_id=f"test.{strategy.manifest.strategy_id}",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
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
    assert report.artifacts[0].artifact_kind == "policy"
    assert "TRAINING" in report.lifecycle
    assert report.metrics.decisions == len(example.events)
    assert report.metrics.fills > 0


def test_full_catalog_is_exactly_six_per_kind() -> None:
    from psrc.strategies.catalog import all_examples

    counts = {kind: 0 for kind in StrategyKind}
    for example in all_examples():
        counts[StrategyKind(example.manifest.kind)] += 1
    assert counts == {
        StrategyKind.RULE: 6,
        StrategyKind.SUPERVISED: 6,
        StrategyKind.REINFORCEMENT_LEARNING: 6,
    }
