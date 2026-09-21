from __future__ import annotations

from collections import deque
from decimal import Decimal

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
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, NoOp, TargetPosition
from psrc.domain.market import BarPayload, MarketEvent


def manifest() -> StrategyManifest:
    return StrategyManifest(
        strategy_id="rule.sma_cross",
        strategy_version="1.0.0",
        kind=StrategyKind.RULE,
        entrypoint="psrc.examples.sma_cross:SmaCrossStrategy",
        required_profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        lifecycle=LifecycleRequirements(training=TrainingMode.NOT_REQUIRED),
        data_requirements=(
            DataRequirement(
                stream_id="bars",
                kind=DataKind.BAR,
                timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
                symbols=("SYNTH.TEST",),
                required_fields=frozenset({"open", "high", "low", "close", "volume"}),
                lookback=5,
            ),
        ),
        action_requirements=ActionRequirements(
            allowed=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
            max_abs_position=Decimal("1"),
        ),
        resources=ResourcePolicy(sandbox=SandboxMode.DEVELOPMENT),
    )


class SmaCrossStrategy:
    manifest = manifest()

    def __init__(self, *, short_window: int = 3, long_window: int = 5) -> None:
        if short_window < 1 or long_window <= short_window:
            raise ValueError("windows must satisfy 1 <= short < long")
        self.short_window = short_window
        self.long_window = long_window
        self._closes: deque[Decimal] = deque(maxlen=long_window)

    def on_start(self) -> None:
        self._closes.clear()

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (
                NoOp(
                    reason_code="unsupported.event",
                    explanation="SMA strategy accepts bar events only",
                ),
            )
        self._closes.append(event.payload.close)
        if len(self._closes) < self.long_window:
            return (
                NoOp(
                    reason_code="warmup.incomplete",
                    explanation=f"requires {self.long_window} closes; has {len(self._closes)}",
                ),
            )
        closes = tuple(self._closes)
        short_mean = sum(closes[-self.short_window :], Decimal("0")) / self.short_window
        long_mean = sum(closes, Decimal("0")) / self.long_window
        target = Decimal("1") if short_mean > long_mean else Decimal("-1")
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=target,
                reason_code="signal.sma_cross",
            ),
        )

    def on_finish(self) -> None:
        pass
