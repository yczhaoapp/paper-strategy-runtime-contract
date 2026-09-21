from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from psrc.contract.models import (
    ActionKind,
    ActionRequirements,
    DataKind,
    DataRequirement,
    DatasetManifest,
    DatasetStream,
    EngineCapabilities,
    ExecutionSemantics,
    LifecycleRequirements,
    RunPolicy,
    SandboxMode,
    StrategyKind,
    StrategyManifest,
    SupportLevel,
    Timeframe,
    TimeframeMode,
    TrainingMode,
)

SHA = "0" * 64


@pytest.fixture
def minute_timeframe() -> Timeframe:
    return Timeframe(mode=TimeframeMode.BAR, interval="PT1M")


@pytest.fixture
def strategy(minute_timeframe: Timeframe) -> StrategyManifest:
    return StrategyManifest(
        strategy_id="rule.sma_cross",
        strategy_version="1.0.0",
        kind=StrategyKind.RULE,
        entrypoint="strategy:Strategy",
        required_profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        lifecycle=LifecycleRequirements(training=TrainingMode.NOT_REQUIRED),
        data_requirements=(
            DataRequirement(
                stream_id="bars",
                kind=DataKind.BAR,
                timeframe=minute_timeframe,
                symbols=("SYNTH.TEST",),
                required_fields=frozenset({"open", "high", "low", "close", "volume"}),
                lookback=20,
            ),
        ),
        action_requirements=ActionRequirements(
            allowed=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
            max_abs_position=Decimal("1"),
        ),
    )


@pytest.fixture
def dataset(minute_timeframe: Timeframe) -> DatasetManifest:
    return DatasetManifest(
        dataset_id="synthetic.minute-bars",
        dataset_version="1.0.0",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        streams=(
            DatasetStream(
                stream_id="bars",
                kind=DataKind.BAR,
                timeframe=minute_timeframe,
                symbols=frozenset({"SYNTH.TEST"}),
                fields=frozenset({"open", "high", "low", "close", "volume"}),
                record_count=100,
                schema_sha256=SHA,
                data_sha256=SHA,
            ),
        ),
        public_or_synthetic=True,
    )


@pytest.fixture
def engine() -> EngineCapabilities:
    return EngineCapabilities(
        engine_id="reference",
        engine_version="0.1.0",
        adapter_version="0.1.0",
        support_level=SupportLevel.CONFORMANCE_VERIFIED,
        profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        data_kinds=frozenset({DataKind.BAR}),
        action_kinds=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        execution=ExecutionSemantics(
            partial_fills=False,
            cancel=True,
            replace="unsupported",
            same_timestamp_ordering="market_then_strategy_then_commands",
            fill_model="reference.bar.next_open.v1",
            fee_model="zero.v1",
            slippage_model="zero.v1",
            latency_model="next_event.v1",
        ),
        sandbox_modes=frozenset({SandboxMode.STRICT_CONTAINER}),
    )


@pytest.fixture
def policy() -> RunPolicy:
    return RunPolicy()
