from __future__ import annotations

from datetime import UTC, datetime

from psrc.adapters.base import BacktestAdapter
from psrc.contract.compatibility import apply_compatibility_plan
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import ExecutionPlan, SandboxMode
from psrc.domain.market import MarketEvent
from psrc.runtime.artifacts import ArtifactStore
from psrc.runtime.guards import (
    validate_effective_events,
    validate_execution_context,
    validate_plan_events,
)
from psrc.runtime.lifecycle import Lifecycle, LifecycleState
from psrc.runtime.report import RunReport, RuntimeLogRecord
from psrc.runtime.strategy import RuntimeStrategy
from psrc.runtime.training import TrainableRuntimeStrategy, TrainingRequest


def _effective_events(
    plan: ExecutionPlan, events: tuple[MarketEvent, ...]
) -> tuple[tuple[MarketEvent, ...], RuntimeLogRecord]:
    validate_plan_events(plan, events)
    try:
        effective = apply_compatibility_plan(events, plan)
    except Exception as exc:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.COMPATIBILITY_TRANSFORM_FAILED,
                message="A compiled compatibility transformation failed before engine execution",
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                details={
                    "source_event_count": len(events),
                    "transformations": [
                        item.transformation_id
                        for item in plan.compatibility
                        if item.transformation_id is not None
                    ],
                    "fallback_used": False,
                },
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
    validate_effective_events(plan, effective)
    return effective, RuntimeLogRecord(
        sequence=0,
        timestamp=datetime.now(UTC),
        level="info",
        stage="compatibility",
        message="Compiled compatibility plan applied",
        fields={
            "source_event_count": len(events),
            "effective_event_count": len(effective),
            "transformations": [
                item.transformation_id
                for item in plan.compatibility
                if item.transformation_id is not None
            ],
        },
    )


def _log(stage: str, message: str, **fields: object) -> RuntimeLogRecord:
    return RuntimeLogRecord(
        sequence=0,
        timestamp=datetime.now(UTC),
        level="info",
        stage=stage,
        message=message,
        fields=dict(fields),
    )


def _finalize_report(
    report: RunReport,
    *,
    lifecycle: Lifecycle,
    prefix_logs: list[RuntimeLogRecord],
) -> RunReport:
    combined = [*prefix_logs, *report.logs, _log("finalization", "Run report finalized")]
    logs = tuple(
        record.model_copy(update={"sequence": index}) for index, record in enumerate(combined)
    )
    return report.model_copy(
        update={
            "lifecycle": tuple(state.value for state in lifecycle.history),
            "logs": logs,
        }
    )


def run_rule(
    *,
    plan: ExecutionPlan,
    strategy: RuntimeStrategy,
    events: tuple[MarketEvent, ...],
    engine: BacktestAdapter,
    sandbox_mode: SandboxMode,
) -> RunReport:
    validate_execution_context(
        plan=plan,
        strategy=strategy,
        engine=engine.capabilities,
        sandbox_mode=sandbox_mode,
    )
    lifecycle = Lifecycle(
        run_id=plan.run_id,
        strategy_id=strategy.manifest.strategy_id,
        engine_id=plan.engine_id,
    )
    try:
        effective_events, compatibility_log = _effective_events(plan, events)
        lifecycle.transition(LifecycleState.VALIDATED)
        lifecycle.transition(LifecycleState.CAPABILITIES_NEGOTIATED)
        lifecycle.transition(LifecycleState.INITIALIZED)
        lifecycle.transition(LifecycleState.INFERENCE_BACKTEST)
        prefix_logs = [
            _log("initialization", "Strategy initialized"),
            compatibility_log,
            _log("inference", "Inference event loop started", events=len(effective_events)),
        ]
        report = engine.run(
            plan=plan,
            strategy=strategy,
            events=effective_events,
            sandbox_mode=sandbox_mode,
        )
        lifecycle.transition(LifecycleState.FINALIZED)
        return _finalize_report(report, lifecycle=lifecycle, prefix_logs=prefix_logs)
    except ContractViolation:
        lifecycle.fail()
        raise
    except Exception as exc:
        lifecycle.fail()
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                stage=ErrorStage.BACKTEST,
                code=ErrorCode.BACKTEST_FAILED,
                message="Rule-strategy backtest failed",
                strategy_id=strategy.manifest.strategy_id,
                engine_id=plan.engine_id,
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc


def run_trainable(
    *,
    plan: ExecutionPlan,
    strategy: TrainableRuntimeStrategy,
    training: TrainingRequest,
    events: tuple[MarketEvent, ...],
    engine: BacktestAdapter,
    store: ArtifactStore,
    sandbox_mode: SandboxMode,
) -> RunReport:
    validate_execution_context(
        plan=plan,
        strategy=strategy,
        engine=engine.capabilities,
        sandbox_mode=sandbox_mode,
    )
    lifecycle = Lifecycle(
        run_id=plan.run_id,
        strategy_id=strategy.manifest.strategy_id,
        engine_id=plan.engine_id,
    )
    lifecycle.transition(LifecycleState.VALIDATED)
    lifecycle.transition(LifecycleState.CAPABILITIES_NEGOTIATED)
    lifecycle.transition(LifecycleState.INITIALIZED)
    try:
        effective_events, compatibility_log = _effective_events(plan, events)
        prefix_logs = [
            _log("initialization", "Strategy initialized"),
            compatibility_log,
        ]
        lifecycle.transition(LifecycleState.TRAINING)
        prefix_logs.append(_log("training", "Model or policy training started"))
        artifact = strategy.train(training, store)
        prefix_logs.append(
            _log("training", "Model or policy training completed", artifact_id=artifact.artifact_id)
        )
        lifecycle.transition(LifecycleState.ARTIFACT_SAVED)
        prefix_logs.append(
            _log("artifact", "Training artifact saved", artifact_id=artifact.artifact_id)
        )
        strategy.load(artifact, store, run_id=plan.run_id)
        lifecycle.transition(LifecycleState.ARTIFACT_LOADED)
        prefix_logs.append(
            _log(
                "artifact",
                "Training artifact integrity-checked and loaded",
                artifact_id=artifact.artifact_id,
            )
        )
        lifecycle.transition(LifecycleState.INFERENCE_BACKTEST)
        prefix_logs.append(
            _log("inference", "Inference event loop started", events=len(effective_events))
        )
        report = engine.run(
            plan=plan,
            strategy=strategy,
            events=effective_events,
            sandbox_mode=sandbox_mode,
        )
        lifecycle.transition(LifecycleState.FINALIZED)
        report = report.model_copy(update={"artifacts": (artifact,)})
        return _finalize_report(
            report,
            lifecycle=lifecycle,
            prefix_logs=prefix_logs,
        )
    except ContractViolation:
        lifecycle.fail()
        raise
    except Exception as exc:
        stage = lifecycle.state
        lifecycle.fail()
        if stage == LifecycleState.TRAINING:
            code = ErrorCode.TRAINING_FAILED
            error_stage = ErrorStage.TRAINING
        elif stage == LifecycleState.INFERENCE_BACKTEST:
            code = ErrorCode.BACKTEST_FAILED
            error_stage = ErrorStage.BACKTEST
        else:
            code = ErrorCode.INFERENCE_FAILED
            error_stage = ErrorStage.INFERENCE
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                stage=error_stage,
                code=code,
                message="Trainable strategy lifecycle failed",
                strategy_id=strategy.manifest.strategy_id,
                engine_id=plan.engine_id,
                details={"lifecycle_state": stage.value},
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
