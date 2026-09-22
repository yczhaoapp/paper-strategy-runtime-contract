from __future__ import annotations

from decimal import Decimal
from math import isfinite

from psrc.contract.models import (
    ActionKind,
    ActionRequirements,
    DataKind,
    DataRequirement,
    LifecycleRequirements,
    ResourcePolicy,
    SandboxMode,
    StrategyKind,
    StrategyManifest,
    Timeframe,
    TimeframeMode,
    TrainingMode,
)


def stable_float(value: float, *, digits: int = 12) -> float:
    """Quantize learned numeric output for cross-platform evidence hashing."""
    if not isfinite(value):
        raise ValueError("strategy numeric output must be finite")
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def canonicalize_numeric(value: object) -> object:
    """Recursively normalize floats before content-addressing model artifacts."""
    if isinstance(value, float):
        return stable_float(value)
    if isinstance(value, dict):
        return {str(key): canonicalize_numeric(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize_numeric(item) for item in value]
    return value


def make_manifest(
    *,
    strategy_id: str,
    kind: StrategyKind,
    entrypoint: str,
    profiles: frozenset[str],
    data: tuple[DataRequirement, ...],
    actions: frozenset[ActionKind],
    training: TrainingMode,
    max_position: Decimal | None = Decimal("10"),
    max_order: Decimal | None = None,
) -> StrategyManifest:
    effective_max_order = max_order
    if (
        effective_max_order is None
        and max_position is not None
        and ActionKind.TARGET_POSITION in actions
    ):
        # A direct reversal from +limit to -limit is one derived native order.
        effective_max_order = max_position * 2
    return StrategyManifest(
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        kind=kind,
        entrypoint=entrypoint,
        required_profiles=profiles,
        lifecycle=LifecycleRequirements(
            training=training, state_checkpointing=kind != StrategyKind.RULE
        ),
        data_requirements=data,
        action_requirements=ActionRequirements(
            allowed=actions,
            max_abs_position=max_position,
            max_order_quantity=effective_max_order,
        ),
        resources=ResourcePolicy(sandbox=SandboxMode.DEVELOPMENT),
        deterministic=True,
        seed=7,
    )


def bar_requirement(
    *,
    interval: str,
    symbols: tuple[str, ...],
    lookback: int,
    stream_id: str = "bars",
) -> DataRequirement:
    return DataRequirement(
        stream_id=stream_id,
        kind=DataKind.BAR,
        timeframe=Timeframe(mode=TimeframeMode.BAR, interval=interval),
        symbols=symbols,
        required_fields=frozenset({"open", "high", "low", "close", "volume"}),
        lookback=lookback,
    )


def event_requirement(
    *,
    stream_id: str,
    kind: DataKind,
    symbols: tuple[str, ...],
    fields: frozenset[str],
    depth: int | None = None,
) -> DataRequirement:
    return DataRequirement(
        stream_id=stream_id,
        kind=kind,
        timeframe=Timeframe(mode=TimeframeMode.EVENT),
        symbols=symbols,
        required_fields=fields,
        lookback=1,
        depth=depth,
    )
