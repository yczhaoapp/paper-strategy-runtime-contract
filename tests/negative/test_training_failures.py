from __future__ import annotations

import json
from pathlib import Path

import pytest

from psrc.adapters.base import BacktestAdapter
from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.cli import main
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import ExecutionPlan, RunPolicy, SandboxMode
from psrc.domain.market import MarketEvent
from psrc.runtime.artifacts import ArtifactStore
from psrc.runtime.orchestrator import run_trainable
from psrc.runtime.report import RunReport
from psrc.runtime.strategy import RuntimeStrategy
from psrc.runtime.training import TrainingRequest
from psrc.strategies.catalog import supervised_examples


def test_invalid_training_shape_is_structured_failure(tmp_path: Path) -> None:
    example = supervised_examples()[0]
    strategy = example.factory()
    plan = compile_run(
        run_id="test.training-failure",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    invalid = TrainingRequest(
        run_id="train.invalid",
        dataset_id="synthetic.invalid",
        seed=7,
        features=((1.0,), (2.0,)),
        labels=(1.0, -1.0),
    )
    with pytest.raises(ContractViolation) as raised:
        run_trainable(
            plan=plan,
            strategy=strategy,
            training=invalid,
            events=example.events,
            engine=ReferenceEngine(),
            store=ArtifactStore(tmp_path / "artifacts"),
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )
    assert raised.value.error.code == ErrorCode.TRAINING_FAILED
    assert raised.value.error.cause_chain


def test_trainable_engine_crash_is_backtest_failure(tmp_path: Path) -> None:
    example = supervised_examples()[0]
    strategy = example.factory()
    plan = compile_run(
        run_id="test.trainable-backtest-failure",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )

    class FailingAdapter:
        capabilities = capabilities()

        def run(
            self,
            *,
            plan: ExecutionPlan,
            strategy: RuntimeStrategy,
            events: tuple[MarketEvent, ...],
            sandbox_mode: SandboxMode,
        ) -> RunReport:
            del plan, strategy, events, sandbox_mode
            raise RuntimeError("synthetic engine crash")

    adapter: BacktestAdapter = FailingAdapter()
    with pytest.raises(ContractViolation) as raised:
        run_trainable(
            plan=plan,
            strategy=strategy,
            training=example.training,
            events=example.events,
            engine=adapter,
            store=ArtifactStore(tmp_path / "artifacts"),
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )
    assert raised.value.error.code == ErrorCode.BACKTEST_FAILED
    assert raised.value.error.stage == "backtest"


def test_package_run_rejects_training_payload_that_differs_from_evidence(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "run"
    assert main(["package", "export", "--output", str(packages)]) == 0
    package = packages / "supervised.logistic_direction"
    request_path = package / "training-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["dataset_id"] = "tampered.other-dataset"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(package),
                "--output",
                str(output),
            ]
        )
        == 3
    )
    failure = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert failure["error"]["code"] == "TRAINING_DATA_MISMATCH"
    assert failure["error"]["details"]["fallback_used"] is False
    assert not (output / "bundle.json").exists()
