from __future__ import annotations

from datetime import UTC, datetime

from psrc.contract.compatibility import apply_compatibility_plan
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    DataKind,
    DatasetManifest,
    DatasetStream,
    EngineCapabilities,
    ExecutionPlan,
    RuntimeCapabilities,
    SandboxMode,
    StrategyManifest,
    TimeframeMode,
)
from psrc.domain.actions import Action, ActionEnvelope, SubmitOrder, TargetPosition
from psrc.domain.market import BookSnapshotL2Payload, MarketEvent
from psrc.runtime.capabilities import capabilities as runtime_capabilities
from psrc.runtime.strategy import RuntimeStrategy

_SANDBOX_RANK = {
    SandboxMode.DEVELOPMENT: 0,
    SandboxMode.STRICT_CONTAINER: 1,
}


def validate_runtime_context(plan: ExecutionPlan) -> None:
    """Bind orchestration-owned profiles to the runtime that executes the plan."""
    runtime: RuntimeCapabilities = runtime_capabilities()
    mismatches: dict[str, object] = {}
    if runtime.runtime_id != plan.runtime_id:
        mismatches["runtime_id"] = {"planned": plan.runtime_id, "actual": runtime.runtime_id}
    actual_sha256 = sha256_model(runtime)
    if actual_sha256 != plan.runtime_capabilities_sha256:
        mismatches["runtime_capabilities_sha256"] = {
            "planned": plan.runtime_capabilities_sha256,
            "actual": actual_sha256,
        }
    missing = plan.runtime_required_profiles - runtime.profiles
    if missing:
        mismatches["runtime_required_profiles"] = {
            "missing": sorted(missing),
            "actual": sorted(runtime.profiles),
        }
    if mismatches:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.EXECUTION_CONTEXT_MISMATCH,
                message="Actual runtime context does not match the compiled plan",
                details={"mismatches": mismatches, "fallback_used": False},
            )
        )


def validate_execution_context(
    *,
    plan: ExecutionPlan,
    strategy: RuntimeStrategy,
    engine: EngineCapabilities,
    sandbox_mode: SandboxMode,
) -> None:
    """Bind a compiled plan to the objects and isolation level that actually execute it."""
    actual_manifest = strategy.manifest
    mismatches: dict[str, object] = {}
    if actual_manifest.strategy_id != plan.strategy_id:
        mismatches["strategy_id"] = {
            "planned": plan.strategy_id,
            "actual": actual_manifest.strategy_id,
        }
    actual_manifest_sha256 = sha256_model(actual_manifest)
    if actual_manifest_sha256 != plan.strategy_manifest_sha256:
        mismatches["strategy_manifest_sha256"] = {
            "planned": plan.strategy_manifest_sha256,
            "actual": actual_manifest_sha256,
        }
    if actual_manifest.data_requirements != plan.data_requirements:
        mismatches["data_requirements"] = "runtime manifest differs from compiled plan"
    if engine.engine_id != plan.engine_id:
        mismatches["engine_id"] = {"planned": plan.engine_id, "actual": engine.engine_id}
    actual_engine_sha256 = sha256_model(engine)
    if actual_engine_sha256 != plan.engine_capabilities_sha256:
        mismatches["engine_capabilities_sha256"] = {
            "planned": plan.engine_capabilities_sha256,
            "actual": actual_engine_sha256,
        }
    actual_sandbox = SandboxMode(sandbox_mode)
    required_sandbox = SandboxMode(plan.required_sandbox)
    if _SANDBOX_RANK[actual_sandbox] < _SANDBOX_RANK[required_sandbox]:
        mismatches["sandbox_mode"] = {
            "required": required_sandbox,
            "actual": actual_sandbox,
        }
    if actual_sandbox not in engine.sandbox_modes:
        mismatches["engine_sandbox_modes"] = {
            "actual": actual_sandbox,
            "declared": sorted(engine.sandbox_modes),
        }
    if mismatches:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.EXECUTION_CONTEXT_MISMATCH,
                message="Actual execution context does not match the compiled plan",
                details={"mismatches": mismatches, "fallback_used": False},
            )
        )


def prepare_adapter_invocation(
    *,
    plan: ExecutionPlan,
    strategy: RuntimeStrategy,
    engine: EngineCapabilities,
    sandbox_mode: SandboxMode,
    source_events: tuple[MarketEvent, ...],
) -> tuple[MarketEvent, ...]:
    """Validate the public adapter boundary and return deterministic effective events."""
    validate_execution_context(
        plan=plan,
        strategy=strategy,
        engine=engine,
        sandbox_mode=sandbox_mode,
    )
    validate_events(plan, source_events)
    validate_plan_events(plan, source_events)
    try:
        effective_events = apply_compatibility_plan(source_events, plan)
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
                    "source_event_count": len(source_events),
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
    validate_effective_events(plan, effective_events)
    validate_events(plan, effective_events)
    return effective_events


def validate_effective_events(plan: ExecutionPlan, events: tuple[MarketEvent, ...]) -> None:
    """Execute constraints that require the post-transformation event stream."""
    if len(plan.data_requirements) != 1:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_STREAM_MISSING,
            message="Effective event validation requires one attributable data requirement",
            details={"declared_requirement_count": len(plan.data_requirements)},
        )
    requirement = plan.data_requirements[0]
    required_symbols = requirement.symbols or tuple(
        sorted({event.instrument_id for event in events})
    )
    counts = {
        symbol: sum(event.instrument_id == symbol for event in events)
        for symbol in required_symbols
    }
    insufficient = {
        symbol: count for symbol, count in counts.items() if count < requirement.lookback
    }
    if insufficient:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_RECORD_COUNT_MISMATCH,
            message="Effective events do not satisfy the declared per-symbol lookback",
            details={
                "stream_id": requirement.stream_id,
                "required_lookback": requirement.lookback,
                "observed_by_symbol": counts,
                "insufficient_symbols": insufficient,
            },
        )
    maximum = requirement.max_staleness_ns
    if maximum is None:
        return
    stale: list[dict[str, object]] = []
    for event in events:
        delay = event.available_time - event.event_time
        actual_ns = (
            (delay.days * 86400 + delay.seconds) * 1_000_000_000
            + delay.microseconds * 1000
        )
        if actual_ns > maximum:
            stale.append(
                {
                    "event_id": event.event_id,
                    "actual_staleness_ns": actual_ns,
                }
            )
            if len(stale) == 5:
                break
    if stale:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_STALENESS_EXCEEDED,
            message="Effective events exceed the declared maximum data staleness",
            details={
                "stream_id": requirement.stream_id,
                "max_staleness_ns": maximum,
                "first_invalid_events": stale,
                "staleness_definition": "available_time_minus_event_time",
            },
        )


def validate_events(plan: ExecutionPlan, events: tuple[MarketEvent, ...]) -> None:
    if not events:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                stage=ErrorStage.BACKTEST,
                code=ErrorCode.BACKTEST_FAILED,
                message="Adapter received no market events",
            )
        )
    keys = [(event.available_time, event.sequence) for event in events]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.DATA_ORDERING_INVALID,
                message="Adapters require nonempty, unique, chronologically ordered events",
            )
        )


_PAYLOAD_FIELDS: dict[DataKind, frozenset[str]] = {
    DataKind.BAR: frozenset({"open", "high", "low", "close", "volume"}),
    DataKind.TRADE: frozenset({"price", "size", "aggressor_side", "trade_id"}),
    DataKind.QUOTE_L1: frozenset({"bid_price", "bid_size", "ask_price", "ask_size"}),
    DataKind.BOOK_SNAPSHOT_L2: frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
}


def _duration_seconds(value: str | None) -> int | None:
    if value == "P1D":
        return 86400
    units = {"S": 1, "M": 60, "H": 3600}
    if value and value.startswith("PT") and value[-1] in units:
        try:
            quantity = int(value[2:-1])
        except ValueError:
            return None
        return quantity * units[value[-1]] if quantity > 0 else None
    return None


def _validate_bar_timeframe(
    *,
    run_id: str,
    strategy_id: str,
    stream: DatasetStream,
    events: tuple[MarketEvent, ...],
) -> None:
    """Validate real bar close timestamps against the declared absolute grid.

    Gaps are valid: a market holiday or missing bar still lands on a later integer
    multiple of the interval. Only the currently specified UTC/epoch grid is
    executable; another declaration must not be accepted with guessed semantics.
    """
    timeframe = stream.timeframe
    if timeframe.mode != TimeframeMode.BAR:
        return
    seconds = _duration_seconds(timeframe.interval)
    if timeframe.timezone != "UTC" or timeframe.alignment != "epoch" or seconds is None:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy_id,
            code=ErrorCode.DATA_TIMEFRAME_MISMATCH,
            message="Bar timeframe uses an unsupported timestamp grid",
            details={
                "stream_id": stream.stream_id,
                "timeframe": timeframe.model_dump(mode="json"),
                "supported_timezone": "UTC",
                "supported_alignment": "epoch",
                "fallback_used": False,
            },
        )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    invalid: list[dict[str, object]] = []
    for event in events:
        timestamp = event.event_time.astimezone(UTC)
        elapsed = timestamp - epoch
        whole_seconds = elapsed.days * 86400 + elapsed.seconds
        if elapsed.microseconds or whole_seconds % seconds:
            invalid.append(
                {
                    "event_id": event.event_id,
                    "event_time": event.event_time.isoformat(),
                }
            )
            if len(invalid) == 5:
                break
    if invalid:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy_id,
            code=ErrorCode.DATA_TIMEFRAME_MISMATCH,
            message="Actual bar event_time is not aligned to the declared timeframe",
            details={
                "stream_id": stream.stream_id,
                "interval": timeframe.interval,
                "timezone": timeframe.timezone,
                "alignment": timeframe.alignment,
                "first_invalid_events": invalid,
                "gaps_allowed": True,
            },
        )


def _input_error(
    *,
    run_id: str,
    strategy_id: str,
    code: ErrorCode,
    message: str,
    details: dict[str, object],
) -> ContractViolation:
    return ContractViolation(
        ContractError(
            run_id=run_id,
            strategy_id=strategy_id,
            stage=ErrorStage.VALIDATION,
            code=code,
            message=message,
            details={**details, "fallback_used": False},
        )
    )


def validate_dataset_events(
    *,
    run_id: str,
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    events: tuple[MarketEvent, ...],
) -> None:
    """Bind actual package bytes to their single declared stream before execution.

    MarketEvent v1 has no stream ID. Accepting multiple declared streams would therefore make
    event attribution ambiguous, so this contract version rejects that input explicitly.
    """
    if len(dataset.streams) != 1:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy.strategy_id,
            code=ErrorCode.DATA_STREAM_MISSING,
            message="Package event input supports exactly one attributable dataset stream",
            details={"declared_stream_count": len(dataset.streams)},
        )
    stream = dataset.streams[0]
    if not events or len(events) != stream.record_count:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy.strategy_id,
            code=ErrorCode.DATA_RECORD_COUNT_MISMATCH,
            message="Actual event count differs from DatasetManifest",
            details={
                "stream_id": stream.stream_id,
                "declared": stream.record_count,
                "actual": len(events),
            },
        )
    actual_kinds = frozenset(str(event.payload.kind) for event in events)
    if actual_kinds != {str(stream.kind)}:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy.strategy_id,
            code=ErrorCode.DATA_KIND_MISMATCH,
            message="Actual market payload kind differs from DatasetManifest",
            details={
                "stream_id": stream.stream_id,
                "declared": str(stream.kind),
                "actual": sorted(actual_kinds),
            },
        )
    actual_symbols = frozenset(event.instrument_id for event in events)
    if actual_symbols != stream.symbols:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy.strategy_id,
            code=ErrorCode.SYMBOL_MAPPING_FAILED,
            message="Actual event symbols differ from DatasetManifest",
            details={
                "stream_id": stream.stream_id,
                "declared": sorted(stream.symbols),
                "actual": sorted(actual_symbols),
            },
        )
    _validate_bar_timeframe(
        run_id=run_id,
        strategy_id=strategy.strategy_id,
        stream=stream,
        events=events,
    )
    available_fields = _PAYLOAD_FIELDS.get(stream.kind)
    if available_fields is None or not stream.fields <= available_fields:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy.strategy_id,
            code=ErrorCode.DATA_FIELD_MISSING,
            message="DatasetManifest fields do not exist on the actual payload type",
            details={
                "stream_id": stream.stream_id,
                "declared_fields": sorted(stream.fields),
                "available_fields": sorted(available_fields or ()),
            },
        )
    requirement = next(
        (item for item in strategy.data_requirements if item.stream_id == stream.stream_id), None
    )
    if requirement is not None and requirement.depth is not None:
        shallow = [
            event.event_id
            for event in events
            if not isinstance(event.payload, BookSnapshotL2Payload)
            or len(event.payload.bids) < requirement.depth
            or len(event.payload.asks) < requirement.depth
        ]
        if shallow:
            raise _input_error(
                run_id=run_id,
                strategy_id=strategy.strategy_id,
                code=ErrorCode.DATA_FIELD_MISSING,
                message="Actual L2 payload does not provide the declared minimum depth",
                details={
                    "stream_id": stream.stream_id,
                    "required_depth": requirement.depth,
                    "first_invalid_event_ids": shallow[:5],
                },
            )
    actual_hash = sha256_model({"events": [event.model_dump(mode="json") for event in events]})
    if stream.data_sha256 != actual_hash:
        raise _input_error(
            run_id=run_id,
            strategy_id=strategy.strategy_id,
            code=ErrorCode.DATA_HASH_MISMATCH,
            message="Package input events do not match the DatasetManifest content hash",
            details={"expected": stream.data_sha256, "actual": actual_hash},
        )


def validate_plan_events(plan: ExecutionPlan, events: tuple[MarketEvent, ...]) -> None:
    """Verify that source events supplied to an orchestrated run match the compiled dataset."""
    if len(plan.dataset_streams) != 1:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_STREAM_MISSING,
            message="ExecutionPlan requires exactly one attributable source stream",
            details={"declared_stream_count": len(plan.dataset_streams)},
        )
    stream = plan.dataset_streams[0]
    if not events or len(events) != stream.record_count:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_RECORD_COUNT_MISMATCH,
            message="Runtime event count differs from the compiled dataset stream",
            details={"declared": stream.record_count, "actual": len(events)},
        )
    actual_kinds = frozenset(str(event.payload.kind) for event in events)
    if actual_kinds != {str(stream.kind)}:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_KIND_MISMATCH,
            message="Runtime payload kind differs from the compiled dataset stream",
            details={"declared": str(stream.kind), "actual": sorted(actual_kinds)},
        )
    actual_symbols = frozenset(event.instrument_id for event in events)
    if actual_symbols != stream.symbols:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.SYMBOL_MAPPING_FAILED,
            message="Runtime symbols differ from the compiled dataset stream",
            details={"declared": sorted(stream.symbols), "actual": sorted(actual_symbols)},
        )
    _validate_bar_timeframe(
        run_id=plan.run_id,
        strategy_id=plan.strategy_id,
        stream=stream,
        events=events,
    )
    actual_hash = sha256_model({"events": [event.model_dump(mode="json") for event in events]})
    if actual_hash != stream.data_sha256:
        raise _input_error(
            run_id=plan.run_id,
            strategy_id=plan.strategy_id,
            code=ErrorCode.DATA_HASH_MISMATCH,
            message="Runtime events differ from the content compiled into ExecutionPlan",
            details={"expected": stream.data_sha256, "actual": actual_hash},
        )


def validate_actions(
    plan: ExecutionPlan,
    strategy: RuntimeStrategy,
    actions: tuple[Action, ...],
    symbols: frozenset[str],
) -> None:
    requirements = strategy.manifest.action_requirements
    for action in actions:
        try:
            checked = ActionEnvelope.model_validate(action.model_dump(mode="python")).root
            if checked.kind not in requirements.allowed:
                raise ValueError("action kind was not declared by strategy")
            symbol = getattr(checked, "instrument_id", None)
            if symbol is not None and symbol not in symbols:
                raise ValueError("action symbol has no mapping in actual input")
            if isinstance(checked, TargetPosition):
                maximum = requirements.max_abs_position
                if maximum is not None and abs(checked.quantity) > maximum:
                    raise ValueError("target position exceeds declared limit")
            if isinstance(checked, SubmitOrder):
                maximum = requirements.max_order_quantity
                if maximum is not None and checked.quantity > maximum:
                    raise ContractViolation(
                        ContractError(
                            run_id=plan.run_id,
                            strategy_id=plan.strategy_id,
                            engine_id=plan.engine_id,
                            stage=ErrorStage.ACTION_VALIDATION,
                            code=ErrorCode.ORDER_REJECTED,
                            message="Order exceeds declared quantity limit",
                            details={
                                "requested": str(checked.quantity),
                                "maximum": str(maximum),
                            },
                        )
                    )
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation(
                ContractError(
                    run_id=plan.run_id,
                    strategy_id=plan.strategy_id,
                    engine_id=plan.engine_id,
                    stage=ErrorStage.ACTION_VALIDATION,
                    code=ErrorCode.ACTION_INVALID,
                    message="Adapter rejected invalid canonical action",
                    cause_chain=(str(exc),),
                )
            ) from exc
