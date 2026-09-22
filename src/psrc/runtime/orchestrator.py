from __future__ import annotations

from datetime import UTC, datetime

from psrc.adapters.base import BacktestAdapter
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.contract.models import ExecutionPlan, SandboxMode
from psrc.domain.market import MarketEvent
from psrc.runtime.artifacts import ArtifactManifest, ArtifactStore
from psrc.runtime.guards import (
    prepare_adapter_invocation,
    validate_runtime_context,
)
from psrc.runtime.lifecycle import Lifecycle, LifecycleState
from psrc.runtime.report import RunReport, RuntimeLogRecord
from psrc.runtime.strategy import RuntimeStrategy
from psrc.runtime.training import (
    TrainableRuntimeStrategy,
    TrainingInputEvidence,
    TrainingRequest,
    build_training_input_evidence,
)
from psrc.sandbox.runtime import bind_strategy_run_id


def _effective_events(
    *,
    plan: ExecutionPlan,
    strategy: RuntimeStrategy,
    events: tuple[MarketEvent, ...],
    engine: BacktestAdapter,
    sandbox_mode: SandboxMode,
) -> tuple[tuple[MarketEvent, ...], RuntimeLogRecord]:
    validate_runtime_context(plan)
    effective = prepare_adapter_invocation(
        plan=plan,
        strategy=strategy,
        engine=engine.capabilities,
        sandbox_mode=sandbox_mode,
        source_events=events,
    )
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


def _validate_training_binding(
    plan: ExecutionPlan, training: TrainingRequest
) -> TrainingInputEvidence:
    evidence = build_training_input_evidence(training)
    actual = sha256_model(evidence)
    expected = plan.training_input_evidence_sha256
    if expected != actual:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.TRAINING_DATA_MISMATCH,
                message="Actual training request does not match the compiled execution plan",
                details={
                    "planned_training_input_evidence_sha256": expected,
                    "actual_training_input_evidence_sha256": actual,
                    "actual_training_request_sha256": evidence.request_sha256,
                    "fallback_used": False,
                },
            )
        )
    return evidence


def _validate_artifact_training_provenance(
    *,
    plan: ExecutionPlan,
    training: TrainingRequest,
    evidence: TrainingInputEvidence,
    artifact: ArtifactManifest,
) -> None:
    mismatches: dict[str, object] = {}
    if artifact.training_request_sha256 != evidence.request_sha256:
        mismatches["training_request_sha256"] = {
            "expected": evidence.request_sha256,
            "actual": artifact.training_request_sha256,
        }
    if artifact.training_dataset_id != training.dataset_id:
        mismatches["training_dataset_id"] = {
            "expected": training.dataset_id,
            "actual": artifact.training_dataset_id,
        }
    if artifact.seed != training.seed:
        mismatches["seed"] = {"expected": training.seed, "actual": artifact.seed}
    if mismatches:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                stage=ErrorStage.ARTIFACT,
                code=ErrorCode.TRAINING_DATA_MISMATCH,
                message="Training artifact provenance does not match the executed request",
                details={"mismatches": mismatches, "fallback_used": False},
            )
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
    bind_strategy_run_id(strategy, plan.run_id)
    lifecycle = Lifecycle(
        run_id=plan.run_id,
        strategy_id=strategy.manifest.strategy_id,
        engine_id=plan.engine_id,
    )
    try:
        effective_events, compatibility_log = _effective_events(
            plan=plan,
            strategy=strategy,
            events=events,
            engine=engine,
            sandbox_mode=sandbox_mode,
        )
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
            events=events,
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
    bind_strategy_run_id(strategy, plan.run_id)
    training_evidence = _validate_training_binding(plan, training)
    lifecycle = Lifecycle(
        run_id=plan.run_id,
        strategy_id=strategy.manifest.strategy_id,
        engine_id=plan.engine_id,
    )
    lifecycle.transition(LifecycleState.VALIDATED)
    lifecycle.transition(LifecycleState.CAPABILITIES_NEGOTIATED)
    lifecycle.transition(LifecycleState.INITIALIZED)
    try:
        effective_events, compatibility_log = _effective_events(
            plan=plan,
            strategy=strategy,
            events=events,
            engine=engine,
            sandbox_mode=sandbox_mode,
        )
        prefix_logs = [
            _log("initialization", "Strategy initialized"),
            compatibility_log,
        ]
        lifecycle.transition(LifecycleState.TRAINING)
        prefix_logs.append(_log("training", "Model or policy training started"))
        artifact = strategy.train(training, store)
        artifact = store.verify_manifest(
            run_id=plan.run_id,
            strategy_id=strategy.manifest.strategy_id,
            strategy_version=strategy.manifest.strategy_version,
            candidate=artifact,
        )
        _validate_artifact_training_provenance(
            plan=plan,
            training=training,
            evidence=training_evidence,
            artifact=artifact,
        )
        prefix_logs.append(
            _log("training", "Model or policy training completed", artifact_id=artifact.artifact_id)
        )
        lifecycle.transition(LifecycleState.ARTIFACT_SAVED)
        prefix_logs.append(
            _log("artifact", "Training artifact saved", artifact_id=artifact.artifact_id)
        )
        strategy.load(artifact, store, run_id=plan.run_id)
        store.verify_manifest(
            run_id=plan.run_id,
            strategy_id=strategy.manifest.strategy_id,
            strategy_version=strategy.manifest.strategy_version,
            candidate=artifact,
        )
        lifecycle.transition(LifecycleState.ARTIFACT_LOADED)
        prefix_logs.append(
            _log(
                "artifact",
                "Stored artifact bytes rechecked and strategy load callback completed",
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
            events=events,
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
