from __future__ import annotations

from pathlib import Path

import pytest

from psrc.adapters.backtrader import BacktraderAdapter
from psrc.adapters.backtrader import capabilities as backtrader_capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.hashing import sha256_model
from psrc.contract.models import RunPolicy, SandboxMode
from psrc.runtime.artifacts import ArtifactStore
from psrc.runtime.orchestrator import run_trainable
from psrc.runtime.training import build_training_input_evidence
from psrc.strategies.catalog import (
    TrainableExample,
    reinforcement_learning_examples,
    supervised_examples,
)


def _example(strategy_id: str) -> TrainableExample:
    return next(
        item
        for item in (*supervised_examples(), *reinforcement_learning_examples())
        if item.manifest.strategy_id == strategy_id
    )


@pytest.mark.parametrize(
    "example",
    (
        _example("supervised.logistic_direction"),
        _example("reinforcement_learning.tabular_q_inventory"),
    ),
    ids=("supervised-logistic", "rl-tabular-q"),
)
def test_backtrader_executes_train_save_reload_infer_backtest_lifecycle(
    example: TrainableExample,
    tmp_path: Path,
) -> None:
    strategy = example.factory()
    plan = compile_run(
        run_id=f"test.backtrader.{strategy.manifest.strategy_id}",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=backtrader_capabilities(),
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
        engine=BacktraderAdapter(),
        store=ArtifactStore(tmp_path / "artifacts"),
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert plan.runtime_required_profiles in (
        frozenset({"training.supervised.v1"}),
        frozenset({"training.rl.v1"}),
    )
    assert plan.engine_required_profiles == frozenset(
        {"core.bar.v1", "execution.basic.v1"}
    )
    assert report.status == "succeeded"
    assert report.metrics.decisions == len(example.events)
    assert report.metrics.fills > 0
    assert "TRAINING" in report.lifecycle
    assert "ARTIFACT_LOADED" in report.lifecycle
