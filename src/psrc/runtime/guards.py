from __future__ import annotations

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.contract.models import DataKind, DatasetManifest, ExecutionPlan, StrategyManifest
from psrc.domain.actions import Action, ActionEnvelope, SubmitOrder, TargetPosition
from psrc.domain.market import BookSnapshotL2Payload, MarketEvent
from psrc.runtime.strategy import RuntimeStrategy


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
