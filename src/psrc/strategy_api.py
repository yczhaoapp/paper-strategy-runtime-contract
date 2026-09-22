"""Minimal import surface available to package-local strategy code.

The runtime owns this module. Strategy manifests cannot add other ``psrc``
modules to their allow-list.
"""

from __future__ import annotations

from typing import Any

from psrc.contract.models import ActionKind, DataKind, StrategyManifest
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import (
    Action,
    ActionEnvelope,
    CancelOrder,
    NoOp,
    Prediction,
    ReplaceOrder,
    SubmitOrder,
    TargetPosition,
    TargetWeight,
)
from psrc.domain.market import (
    BarPayload,
    BookLevel,
    BookSnapshotL2Payload,
    MarketEvent,
    QuoteL1Payload,
    TradePayload,
)
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.papers.strategies import (
    AvellanedaStrategy,
    ExecutionQStrategy,
    MovingAverageBandStrategy,
    QueueLogisticStrategy,
)
from psrc.strategies.reinforcement_learning import (
    A2CPairsStrategy,
    DoubleQBookInventoryStrategy,
    LinearActorCriticAllocationStrategy,
    RiskAverseContextualBanditStrategy,
    SarsaTrendStrategy,
    TabularQInventoryStrategy,
)
from psrc.strategies.rule import (
    DonchianBreakoutStrategy,
    L1MicropriceStrategy,
    L2ImbalanceMakerStrategy,
    PairsZScoreStrategy,
    TwapExecutionStrategy,
)
from psrc.strategies.supervised import (
    CrossSectionalRankerStrategy,
    GaussianVolumeBreakoutStrategy,
    L1AdverseSelectionStrategy,
    L2FillProbabilityStrategy,
    LogisticDirectionStrategy,
    RidgeReturnStrategy,
)

_BUNDLED_STRATEGIES: dict[str, type[Any]] = {
    strategy.manifest.strategy_id: strategy
    for strategy in (
        A2CPairsStrategy,
        CrossSectionalRankerStrategy,
        DonchianBreakoutStrategy,
        DoubleQBookInventoryStrategy,
        GaussianVolumeBreakoutStrategy,
        L1AdverseSelectionStrategy,
        L1MicropriceStrategy,
        L2FillProbabilityStrategy,
        L2ImbalanceMakerStrategy,
        LinearActorCriticAllocationStrategy,
        LogisticDirectionStrategy,
        PairsZScoreStrategy,
        RidgeReturnStrategy,
        RiskAverseContextualBanditStrategy,
        SmaCrossStrategy,
        SarsaTrendStrategy,
        TabularQInventoryStrategy,
        TwapExecutionStrategy,
    )
}


def bundled_strategy_class(strategy_id: str) -> type[Any]:
    """Return the trusted implementation class for an exported catalog package."""
    try:
        return _BUNDLED_STRATEGIES[strategy_id]
    except KeyError as exc:
        raise ValueError(f"unknown bundled strategy: {strategy_id}") from exc


__all__ = [
    "AccountSnapshot",
    "Action",
    "ActionEnvelope",
    "ActionKind",
    "AvellanedaStrategy",
    "BarPayload",
    "BookLevel",
    "BookSnapshotL2Payload",
    "CancelOrder",
    "DataKind",
    "ExecutionQStrategy",
    "MarketEvent",
    "MovingAverageBandStrategy",
    "NoOp",
    "Prediction",
    "QueueLogisticStrategy",
    "QuoteL1Payload",
    "ReplaceOrder",
    "StrategyManifest",
    "SubmitOrder",
    "TargetPosition",
    "TargetWeight",
    "TradePayload",
    "bundled_strategy_class",
]
