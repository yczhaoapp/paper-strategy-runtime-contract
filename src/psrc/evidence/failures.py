from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import (
    DatasetManifest,
    RunPolicy,
    SandboxMode,
    StrategyManifest,
    Timeframe,
    TimeframeMode,
)
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, SubmitOrder, TargetPosition
from psrc.domain.market import MarketEvent
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.runtime.artifacts import ArtifactFile, ArtifactManifest, ArtifactStore
from psrc.runtime.orchestrator import run_trainable
from psrc.runtime.report import FailureReport, write_failure_bundle
from psrc.runtime.training import TrainingRequest
from psrc.sandbox.container import DockerSandbox
from psrc.strategies.catalog import rule_examples, supervised_examples
from psrc.strategies.rule import TwapExecutionStrategy


class _IllegalPositionStrategy(SmaCrossStrategy):
    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal(2),
                reason_code="evidence.illegal-position",
            ),
        )


class _IllegalOrderStrategy(TwapExecutionStrategy):
    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        return (
            SubmitOrder(
                client_order_id="illegal:oversized",
                instrument_id=event.instrument_id,
                side="buy",
                order_type="market",
                quantity=Decimal("3"),
                reason_code="evidence.illegal-order",
            ),
        )


def generate_failure_evidence(output: Path) -> dict[str, str]:
    output.mkdir(parents=True, exist_ok=True)
    events = minute_bars()
    strategy = SmaCrossStrategy()
    dataset = minute_bar_manifest(events)
    sandbox = (
        SandboxMode.STRICT_CONTAINER
        if DockerSandbox.current_process_attested()
        else SandboxMode.DEVELOPMENT
    )
    engine = capabilities(strict_container=sandbox == SandboxMode.STRICT_CONTAINER)
    policy = RunPolicy(required_sandbox=sandbox)
    observed: dict[str, str] = {}

    def capture(
        name: str,
        expected: ErrorCode,
        operation: Callable[[], object],
        *,
        report_strategy: StrategyManifest = strategy.manifest,
        report_dataset: DatasetManifest = dataset,
        actual_input: dict[str, object] | None = None,
    ) -> None:
        started = datetime.now(UTC)
        try:
            operation()
        except ContractViolation as exc:
            if exc.error.code != expected:
                raise AssertionError(
                    f"{name}: expected {expected}, observed {exc.error.code}"
                ) from exc
            report = FailureReport(
                run_id=exc.error.run_id,
                started_at=started,
                finished_at=datetime.now(UTC),
                error=exc.error,
                strategy_manifest=report_strategy,
                dataset_manifest=report_dataset,
                engine_capabilities=engine,
                run_policy=policy,
                actual_input=actual_input or {},
            )
            write_failure_bundle(report, output / name)
            observed[name] = str(exc.error.code)
            return
        raise AssertionError(f"{name}: scenario unexpectedly succeeded")

    incomplete_stream = dataset.streams[0].model_copy(
        update={"fields": frozenset({"open", "close"})}
    )
    incomplete_dataset = dataset.model_copy(update={"streams": (incomplete_stream,)})
    capture(
        "field_missing",
        ErrorCode.DATA_FIELD_MISSING,
        lambda: compile_run(
            run_id="evidence.field-missing",
            strategy=strategy.manifest,
            dataset=incomplete_dataset,
            engine=engine,
            policy=policy,
        ),
        report_dataset=incomplete_dataset,
        actual_input={"event_count": len(events), "stream_id": incomplete_stream.stream_id},
    )

    daily_stream = dataset.streams[0].model_copy(
        update={"timeframe": Timeframe(mode=TimeframeMode.BAR, interval="P1D")}
    )
    daily_dataset = dataset.model_copy(update={"streams": (daily_stream,)})
    capture(
        "timeframe_mismatch",
        ErrorCode.DATA_TIMEFRAME_MISMATCH,
        lambda: compile_run(
            run_id="evidence.timeframe-mismatch",
            strategy=strategy.manifest,
            dataset=daily_dataset,
            engine=engine,
            policy=policy,
        ),
        report_dataset=daily_dataset,
        actual_input={"event_count": len(events), "stream_id": daily_stream.stream_id},
    )

    wrong_symbol_stream = dataset.streams[0].model_copy(
        update={"symbols": frozenset({"OTHER.SYMBOL"})}
    )
    wrong_symbol_dataset = dataset.model_copy(update={"streams": (wrong_symbol_stream,)})
    capture(
        "symbol_mapping_failed",
        ErrorCode.SYMBOL_MAPPING_FAILED,
        lambda: compile_run(
            run_id="evidence.symbol-mapping",
            strategy=strategy.manifest,
            dataset=wrong_symbol_dataset,
            engine=engine,
            policy=policy,
        ),
        report_dataset=wrong_symbol_dataset,
        actual_input={"event_count": len(events), "stream_id": wrong_symbol_stream.stream_id},
    )

    missing_manifest = ArtifactManifest(
        artifact_id="missing-model",
        strategy_id=strategy.manifest.strategy_id,
        strategy_version=strategy.manifest.strategy_version,
        artifact_kind="model",
        framework="numpy",
        created_at=datetime.now(UTC),
        training_dataset_id=dataset.dataset_id,
        seed=0,
        files=(
            ArtifactFile(
                logical_name="model.json",
                media_type="application/json",
                sha256="0" * 64,
                size_bytes=0,
            ),
        ),
    )
    capture(
        "model_file_missing",
        ErrorCode.ARTIFACT_NOT_FOUND,
        lambda: ArtifactStore(output / "_empty-artifact-store").load_bytes(
            run_id="evidence.model-missing",
            strategy_id=strategy.manifest.strategy_id,
            manifest=missing_manifest,
        ),
        actual_input={"artifact_id": missing_manifest.artifact_id},
    )

    def illegal_action() -> object:
        illegal = _IllegalPositionStrategy()
        plan = compile_run(
            run_id="evidence.illegal-action",
            strategy=illegal.manifest,
            dataset=dataset,
            engine=engine,
            policy=policy,
        )
        return ReferenceEngine().run(
            plan=plan,
            strategy=illegal,
            events=events,
            sandbox_mode=sandbox,
        )

    capture(
        "illegal_action",
        ErrorCode.ACTION_INVALID,
        illegal_action,
        actual_input={"event_count": len(events)},
    )

    twap_example = next(
        item for item in rule_examples() if item.manifest.strategy_id == "rule.twap_execution"
    )

    def illegal_order() -> object:
        illegal = _IllegalOrderStrategy()
        plan = compile_run(
            run_id="evidence.illegal-order",
            strategy=illegal.manifest,
            dataset=twap_example.dataset,
            engine=engine,
            policy=policy,
        )
        return ReferenceEngine().run(
            plan=plan,
            strategy=illegal,
            events=twap_example.events,
            sandbox_mode=sandbox,
        )

    capture(
        "illegal_order",
        ErrorCode.ORDER_REJECTED,
        illegal_order,
        report_strategy=_IllegalOrderStrategy.manifest,
        report_dataset=twap_example.dataset,
        actual_input={"event_count": len(twap_example.events), "requested_quantity": "3"},
    )

    supervised_example = supervised_examples()[0]

    def training_failure() -> object:
        trainable = supervised_example.factory()
        plan = compile_run(
            run_id="evidence.training-failed",
            strategy=trainable.manifest,
            dataset=supervised_example.dataset,
            engine=engine,
            policy=policy,
        )
        invalid = TrainingRequest(
            run_id="evidence.invalid-training-request",
            dataset_id="synthetic.invalid",
            seed=7,
            features=((1.0,), (2.0,)),
            labels=(1.0, -1.0),
        )
        return run_trainable(
            plan=plan,
            strategy=trainable,
            training=invalid,
            events=supervised_example.events,
            engine=ReferenceEngine(),
            store=ArtifactStore(output / "_failed-training-artifacts"),
            sandbox_mode=sandbox,
        )

    capture(
        "training_failed",
        ErrorCode.TRAINING_FAILED,
        training_failure,
        report_strategy=supervised_example.manifest,
        report_dataset=supervised_example.dataset,
        actual_input={
            "training_feature_rows": 2,
            "training_label_rows": 2,
            "expected_feature_width": 2,
        },
    )

    def backtest_failure() -> object:
        plan = compile_run(
            run_id="evidence.backtest-failed",
            strategy=strategy.manifest,
            dataset=dataset,
            engine=engine,
            policy=policy,
        )
        return ReferenceEngine().run(
            plan=plan,
            strategy=strategy,
            events=(),
            sandbox_mode=sandbox,
        )

    capture(
        "backtest_failed",
        ErrorCode.BACKTEST_FAILED,
        backtest_failure,
        actual_input={"event_count": 0},
    )
    (output / "summary.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "scenario_count": len(observed),
                "observed_error_codes": observed,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return observed
