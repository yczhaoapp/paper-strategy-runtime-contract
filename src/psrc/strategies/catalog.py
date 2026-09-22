from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    DataKind,
    DatasetManifest,
    StrategyManifest,
    Timeframe,
    TimeframeMode,
)
from psrc.domain.market import BarPayload, MarketEvent
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import (
    cross_sectional_daily_bars,
    daily_bars,
    l1_quotes,
    l2_books,
    manifest_for_events,
    minute_bar_manifest,
    minute_bars,
    twap_bars,
)
from psrc.papers.data import public_daily_bars, public_pair_daily_bars
from psrc.papers.models import DataSourceEvidence
from psrc.runtime.strategy import RuntimeStrategy
from psrc.runtime.training import RLTransition, TrainableRuntimeStrategy, TrainingRequest
from psrc.strategies.common import stable_float
from psrc.strategies.reinforcement_learning import (
    A2CPairsStrategy,
    DoubleQBookInventoryStrategy,
    LinearActorCriticAllocationStrategy,
    RiskAverseContextualBanditStrategy,
    SarsaTrendStrategy,
    TabularQInventoryStrategy,
    allocation_for_action,
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
    three_day_lagged_returns,
)


@dataclass(frozen=True)
class StrategyExample:
    manifest: StrategyManifest
    factory: Callable[[], RuntimeStrategy]
    events: tuple[MarketEvent, ...]
    dataset: DatasetManifest


@dataclass(frozen=True)
class TrainableExample:
    manifest: StrategyManifest
    factory: Callable[[], TrainableRuntimeStrategy]
    events: tuple[MarketEvent, ...]
    dataset: DatasetManifest
    training: TrainingRequest


def _example(
    factory: Callable[[], RuntimeStrategy],
    events: tuple[MarketEvent, ...],
    *,
    dataset_id: str,
    stream_id: str,
    kind: DataKind,
    timeframe: Timeframe,
    fields: frozenset[str],
) -> StrategyExample:
    instance = factory()
    return StrategyExample(
        manifest=instance.manifest,
        factory=factory,
        events=events,
        dataset=manifest_for_events(
            dataset_id=dataset_id,
            stream_id=stream_id,
            kind=kind,
            timeframe=timeframe,
            fields=fields,
            events=events,
        ),
    )


def _public_manifest(
    events: tuple[MarketEvent, ...],
    evidence: DataSourceEvidence,
    *,
    dataset_id: str,
) -> DatasetManifest:
    manifest = manifest_for_events(
        dataset_id=dataset_id,
        stream_id="bars",
        kind=DataKind.BAR,
        timeframe=Timeframe(mode=TimeframeMode.BAR, interval="P1D"),
        fields=frozenset({"open", "high", "low", "close", "volume"}),
        events=events,
    )
    return manifest.model_copy(
        update={
            "extensions": {"org.singularityx.data-provenance": evidence.model_dump(mode="json")}
        }
    )


def _bar_features(event: MarketEvent) -> tuple[float, float, float, float, float]:
    bar = event.payload
    if not isinstance(bar, BarPayload):
        raise TypeError("public daily training requires bars")
    return (
        stable_float(float((bar.close - bar.open) / bar.open)),
        stable_float(float((bar.high - bar.open) / bar.open)),
        stable_float(float((bar.low - bar.open) / bar.open)),
        stable_float(float((bar.high - bar.low) / bar.open)),
        stable_float(math.log1p(float(bar.volume)) / 10),
    )


def _public_logistic_training(events: tuple[MarketEvent, ...]) -> TrainingRequest:
    features = tuple(_bar_features(event) for event in events[:-1])
    closes = []
    for event in events:
        if not isinstance(event.payload, BarPayload):
            raise TypeError("public daily labels require bars")
        closes.append(event.payload.close)
    labels = tuple(1.0 if right > left else -1.0 for left, right in pairwise(closes))
    return TrainingRequest(
        run_id="train.supervised.logistic_direction",
        dataset_id="public.plotly.aapl-2015-2017.train",
        seed=7,
        features=features,
        labels=labels,
        metadata={
            "origin": "public_historical",
            "split": "first-400-bars-train-next-106-bars-test",
            "source_events_sha256": sha256_model(
                {"events": [event.model_dump(mode="json") for event in events]}
            ),
            "label": "next-public-daily-close-direction",
        },
    )


def _public_ridge_training(events: tuple[MarketEvent, ...]) -> TrainingRequest:
    features: list[tuple[float, ...]] = []
    labels: list[float] = []
    closes = []
    for event in events:
        if not isinstance(event.payload, BarPayload):
            raise TypeError("public ridge training requires bars")
        closes.append(event.payload.close)
    for current_index in range(3, len(closes) - 1):
        features.append(
            three_day_lagged_returns(tuple(closes[current_index - 3 : current_index + 1]))
        )
        labels.append(
            stable_float(float(closes[current_index + 1] / closes[current_index] - 1))
        )
    return TrainingRequest(
        run_id="train.supervised.ridge_return",
        dataset_id="public.plotly.aapl-2015-2017.train",
        seed=7,
        features=tuple(features),
        labels=tuple(labels),
        metadata={
            "origin": "public_historical",
            "split": "first-400-bars-train-next-106-bars-test",
            "source_events_sha256": sha256_model(
                {"events": [event.model_dump(mode="json") for event in events]}
            ),
            "features": "three-causal-lagged-daily-close-returns",
            "label": "next-public-daily-close-return",
        },
    )


def _public_pair_rows(
    events: tuple[MarketEvent, ...],
) -> tuple[tuple[MarketEvent, MarketEvent], ...]:
    if len(events) % 2:
        raise ValueError("public pair data must contain synchronized event pairs")
    rows = []
    for index in range(0, len(events), 2):
        left, right = events[index : index + 2]
        if (
            left.instrument_id != "PUBLIC.AAPL"
            or right.instrument_id != "PUBLIC.MSFT"
            or left.available_time != right.available_time
        ):
            raise ValueError("public pair data must be ordered AAPL/MSFT and synchronized")
        if not isinstance(left.payload, BarPayload) or not isinstance(right.payload, BarPayload):
            raise TypeError("public pair training requires bars")
        rows.append((left, right))
    return tuple(rows)


def _pair_state(spread: float, history: tuple[float, ...], inventory: int) -> tuple[float, ...]:
    mean = sum(history) / len(history) if history else spread
    variance = (
        sum((value - mean) ** 2 for value in history) / len(history) if len(history) > 1 else 1.0
    )
    deviation = math.sqrt(variance)
    change = spread - history[-1] if history else 0.0
    return (
        stable_float((spread - mean) / max(deviation, 1e-6)),
        float(inventory),
        stable_float(change / 5),
    )


def _public_a2c_pair_training(events: tuple[MarketEvent, ...]) -> TrainingRequest:
    rows = _public_pair_rows(events)
    history: deque[float] = deque(maxlen=6)
    transitions: list[RLTransition] = []
    inventory = 0
    for index, (current, nxt) in enumerate(pairwise(rows)):
        left, right = current[0].payload, current[1].payload
        next_left, next_right = nxt[0].payload, nxt[1].payload
        assert isinstance(left, BarPayload) and isinstance(right, BarPayload)
        assert isinstance(next_left, BarPayload) and isinstance(next_right, BarPayload)
        spread = float(left.close - 2 * right.close)
        state = _pair_state(spread, tuple(history), inventory)
        action = index % 3
        target = action - 1
        history.append(spread)
        next_spread = float(next_left.close - 2 * next_right.close)
        next_state = _pair_state(next_spread, tuple(history), target)
        pair_return = float(next_left.close / left.close - next_right.close / right.close)
        turnover_cost = abs(target - inventory) * 0.0005
        transitions.append(
            RLTransition(
                episode_id=f"public-aapl-msft:{index // 20}",
                step=index % 20,
                state=state,
                action=action,
                reward=stable_float(target * pair_return - turnover_cost),
                next_state=next_state,
                next_action=(index + 1) % 3,
                terminated=index % 20 == 19 or index == len(rows) - 2,
            )
        )
        inventory = target
    return TrainingRequest(
        run_id="train.reinforcement_learning.a2c_pairs",
        dataset_id="public.plotly.aapl-msft-2007-2016.train",
        seed=7,
        transitions=tuple(transitions),
        metadata={
            "origin": "public_historical",
            "split": "first-400-paired-days-train-next-106-paired-days-test",
            "source_events_sha256": sha256_model(
                {"events": [event.model_dump(mode="json") for event in events]}
            ),
            "reward": "next-day-aapl-minus-msft-return-minus-5bps-turnover",
            "actions": "deterministic offline exploration cycle {-1,0,1}",
        },
    )


def _public_actor_critic_training(events: tuple[MarketEvent, ...]) -> TrainingRequest:
    transitions: list[RLTransition] = []
    allocation = 0.0
    for index, (current, nxt) in enumerate(pairwise(events)):
        current_bar, next_bar = current.payload, nxt.payload
        if not isinstance(current_bar, BarPayload) or not isinstance(next_bar, BarPayload):
            raise TypeError("public actor-critic training requires bars")
        action = index % 3
        target_allocation = allocation_for_action(action)
        state = (
            float((current_bar.close - current_bar.open) / current_bar.open * 10),
            float((current_bar.high - current_bar.low) / current_bar.open * 10),
            allocation,
        )
        next_state = (
            float((next_bar.close - next_bar.open) / next_bar.open * 10),
            float((next_bar.high - next_bar.low) / next_bar.open * 10),
            target_allocation,
        )
        market_return = float(next_bar.close / current_bar.close - 1)
        turnover_cost = abs(target_allocation - allocation) * 0.0005
        transitions.append(
            RLTransition(
                episode_id=f"public-aapl:{index // 20}",
                step=index % 20,
                state=state,
                action=action,
                reward=target_allocation * market_return - turnover_cost,
                next_state=next_state,
                next_action=(index + 1) % 3,
                terminated=index % 20 == 19 or index == len(events) - 2,
            )
        )
        allocation = target_allocation
    return TrainingRequest(
        run_id="train.reinforcement_learning.linear_actor_critic_allocation",
        dataset_id="public.plotly.aapl-2015-2017.train",
        seed=7,
        transitions=tuple(transitions),
        metadata={
            "origin": "public_historical",
            "split": "first-400-bars-train-next-106-bars-test",
            "source_events_sha256": sha256_model(
                {"events": [event.model_dump(mode="json") for event in events]}
            ),
            "reward": "next-day-return-minus-5bps-turnover",
            "actions": "deterministic offline exploration cycle {-0.8,0,0.8}",
        },
    )


def rule_examples() -> tuple[StrategyExample, ...]:
    minute = minute_bars()
    public_daily, public_evidence = public_daily_bars()
    public_pairs, public_pair_evidence = public_pair_daily_bars()
    public_pair_sample = public_pairs[: 506 * 2]
    quotes = l1_quotes()
    books = l2_books()
    twap = twap_bars()
    bar_fields = frozenset({"open", "high", "low", "close", "volume"})
    return (
        StrategyExample(
            manifest=SmaCrossStrategy.manifest,
            factory=SmaCrossStrategy,
            events=minute,
            dataset=minute_bar_manifest(minute),
        ),
        StrategyExample(
            manifest=DonchianBreakoutStrategy.manifest,
            factory=DonchianBreakoutStrategy,
            events=public_daily,
            dataset=_public_manifest(
                public_daily,
                public_evidence,
                dataset_id="public.plotly.aapl-2015-2017.donchian",
            ),
        ),
        StrategyExample(
            manifest=PairsZScoreStrategy.manifest,
            factory=PairsZScoreStrategy,
            events=public_pair_sample,
            dataset=_public_manifest(
                public_pair_sample,
                public_pair_evidence,
                dataset_id="public.plotly.aapl-msft-2007-2016.pairs-zscore",
            ),
        ),
        _example(
            L1MicropriceStrategy,
            quotes,
            dataset_id="synthetic.l1-quotes",
            stream_id="quotes",
            kind=DataKind.QUOTE_L1,
            timeframe=Timeframe(mode=TimeframeMode.EVENT),
            fields=frozenset({"bid_price", "bid_size", "ask_price", "ask_size"}),
        ),
        _example(
            L2ImbalanceMakerStrategy,
            books,
            dataset_id="synthetic.l2-books",
            stream_id="book",
            kind=DataKind.BOOK_SNAPSHOT_L2,
            timeframe=Timeframe(mode=TimeframeMode.EVENT),
            fields=frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
        ),
        _example(
            TwapExecutionStrategy,
            twap,
            dataset_id="synthetic.twap-bars",
            stream_id="bars",
            kind=DataKind.BAR,
            timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
            fields=bar_fields,
        ),
    )


def _training(strategy_id: str, dimensions: int) -> TrainingRequest:
    rows: list[tuple[float, ...]] = []
    labels: list[float] = []
    for index in range(18):
        base = (index - 8.5) / 10
        row = tuple(
            base * (feature + 1) + ((index + feature) % 3 - 1) * 0.05
            for feature in range(dimensions)
        )
        rows.append(row)
        labels.append(1.0 if sum(row) + (0.15 if index % 4 == 0 else -0.05) > 0 else -1.0)
    return TrainingRequest(
        run_id=f"train.{strategy_id}",
        dataset_id="synthetic.training-matrix",
        seed=7,
        features=tuple(rows),
        labels=tuple(labels),
        metadata={"split": "chronological-synthetic-v1"},
    )


def _continuous_return_training(strategy_id: str) -> TrainingRequest:
    base = _training(strategy_id, 3)
    labels = tuple(0.6 * row[0] - 0.25 * row[1] + 0.4 * row[2] for row in base.features)
    return base.model_copy(update={"labels": labels})


def _fill_training(strategy_id: str) -> TrainingRequest:
    features = tuple(
        (
            float(1 + index % 4),
            float(1 + (index * 3) % 5),
            0.5 + (index % 4) * 0.25,
        )
        for index in range(24)
    )
    labels = tuple(1.0 if row[0] > row[1] else 0.0 for row in features)
    return TrainingRequest(
        run_id=f"train.{strategy_id}",
        dataset_id="synthetic.queue-race-events",
        seed=7,
        features=features,
        labels=labels,
        metadata={
            "feature_semantics": "own-deaths,opposite-deaths,exposure-seconds",
            "label": "diagnostic-own-death-count-exceeds-opposite",
            "estimator": "constant-intensity-poisson-mle",
            "prediction_horizon_seconds": "0.5",
        },
    )


def supervised_examples() -> tuple[TrainableExample, ...]:
    public_daily, public_evidence = public_daily_bars()
    public_train, public_test = public_daily[:400], public_daily[400:]
    twap = twap_bars()
    quotes = l1_quotes()
    books = l2_books()
    cross_section = cross_sectional_daily_bars()
    bar_fields = frozenset({"open", "high", "low", "close", "volume"})

    def build(
        factory: Callable[[], TrainableRuntimeStrategy],
        events: tuple[MarketEvent, ...],
        *,
        dataset_id: str,
        stream_id: str,
        kind: DataKind,
        timeframe: Timeframe,
        fields: frozenset[str],
        dimensions: int,
        training: TrainingRequest | None = None,
    ) -> TrainableExample:
        strategy = factory()
        return TrainableExample(
            manifest=strategy.manifest,
            factory=factory,
            events=events,
            dataset=manifest_for_events(
                dataset_id=dataset_id,
                stream_id=stream_id,
                kind=kind,
                timeframe=timeframe,
                fields=fields,
                events=events,
            ),
            training=training or _training(strategy.manifest.strategy_id, dimensions),
        )

    return (
        TrainableExample(
            manifest=LogisticDirectionStrategy.manifest,
            factory=LogisticDirectionStrategy,
            events=public_test,
            dataset=_public_manifest(
                public_test,
                public_evidence,
                dataset_id="public.plotly.aapl-2015-2017.logistic-test",
            ),
            training=_public_logistic_training(public_train),
        ),
        TrainableExample(
            manifest=RidgeReturnStrategy.manifest,
            factory=RidgeReturnStrategy,
            events=public_test,
            dataset=_public_manifest(
                public_test,
                public_evidence,
                dataset_id="public.plotly.aapl-2015-2017.ridge-test",
            ),
            training=_public_ridge_training(public_train),
        ),
        build(
            GaussianVolumeBreakoutStrategy,
            twap,
            dataset_id="synthetic.twap-bars",
            stream_id="bars",
            kind=DataKind.BAR,
            timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
            fields=bar_fields,
            dimensions=2,
        ),
        build(
            L1AdverseSelectionStrategy,
            quotes,
            dataset_id="synthetic.l1-quotes",
            stream_id="quotes",
            kind=DataKind.QUOTE_L1,
            timeframe=Timeframe(mode=TimeframeMode.EVENT),
            fields=frozenset({"bid_price", "bid_size", "ask_price", "ask_size"}),
            dimensions=3,
        ),
        build(
            L2FillProbabilityStrategy,
            books,
            dataset_id="synthetic.l2-books",
            stream_id="book",
            kind=DataKind.BOOK_SNAPSHOT_L2,
            timeframe=Timeframe(mode=TimeframeMode.EVENT),
            fields=frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
            dimensions=3,
            training=_fill_training("supervised.l2_fill_probability"),
        ),
        build(
            CrossSectionalRankerStrategy,
            cross_section,
            dataset_id="synthetic.cross-sectional-bars",
            stream_id="bars",
            kind=DataKind.BAR,
            timeframe=Timeframe(mode=TimeframeMode.BAR, interval="P1D"),
            fields=bar_fields,
            dimensions=3,
            training=_continuous_return_training("supervised.cross_sectional_ranker"),
        ),
    )


def _rl_training(strategy_id: str) -> TrainingRequest:
    transitions: list[RLTransition] = []
    for index in range(30):
        state = (
            float(index % 5 - 2),
            float((index // 2) % 5 - 2),
            float((index // 3) % 5 - 2),
        )
        action = index % 3
        next_state = (
            float((index + 1) % 5 - 2),
            float(((index + 1) // 2) % 5 - 2),
            float(((index + 1) // 3) % 5 - 2),
        )
        preferred = int(max(0, min(2, round(state[0]) + 1)))
        reward = 1.0 if action == preferred else -0.4 - abs(action - preferred) * 0.1
        transitions.append(
            RLTransition(
                episode_id=f"episode:{index // 10}",
                step=index % 10,
                state=state,
                action=action,
                reward=reward,
                next_state=next_state,
                next_action=(index + 1) % 3,
                terminated=index % 10 == 9,
            )
        )
    return TrainingRequest(
        run_id=f"train.{strategy_id}",
        dataset_id="synthetic.rl-transitions",
        seed=7,
        transitions=tuple(transitions),
        metadata={
            "observation_space": "Box(3)",
            "action_space": "Discrete(3)",
            "reward": "preferred-action-minus-distance-v1",
        },
    )


def reinforcement_learning_examples() -> tuple[TrainableExample, ...]:
    daily = daily_bars()
    public_daily, public_evidence = public_daily_bars()
    public_train, public_test = public_daily[:400], public_daily[400:]
    public_pairs, public_pair_evidence = public_pair_daily_bars()
    public_pair_train = public_pairs[: 400 * 2]
    public_pair_test = public_pairs[400 * 2 : 506 * 2]
    minute = minute_bars()
    books = l2_books()
    bar_fields = frozenset({"open", "high", "low", "close", "volume"})

    def build(
        factory: Callable[[], TrainableRuntimeStrategy],
        events: tuple[MarketEvent, ...],
        *,
        dataset_id: str,
        stream_id: str,
        kind: DataKind,
        timeframe: Timeframe,
        fields: frozenset[str],
    ) -> TrainableExample:
        strategy = factory()
        return TrainableExample(
            manifest=strategy.manifest,
            factory=factory,
            events=events,
            dataset=manifest_for_events(
                dataset_id=dataset_id,
                stream_id=stream_id,
                kind=kind,
                timeframe=timeframe,
                fields=fields,
                events=events,
            ),
            training=_rl_training(strategy.manifest.strategy_id),
        )

    return (
        build(
            TabularQInventoryStrategy,
            daily,
            dataset_id="synthetic.daily-bars",
            stream_id="bars",
            kind=DataKind.BAR,
            timeframe=Timeframe(mode=TimeframeMode.BAR, interval="P1D"),
            fields=bar_fields,
        ),
        build(
            SarsaTrendStrategy,
            minute,
            dataset_id="synthetic.minute-bars",
            stream_id="bars",
            kind=DataKind.BAR,
            timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
            fields=bar_fields,
        ),
        build(
            RiskAverseContextualBanditStrategy,
            daily,
            dataset_id="synthetic.daily-bars",
            stream_id="bars",
            kind=DataKind.BAR,
            timeframe=Timeframe(mode=TimeframeMode.BAR, interval="P1D"),
            fields=bar_fields,
        ),
        build(
            DoubleQBookInventoryStrategy,
            books,
            dataset_id="synthetic.l2-books",
            stream_id="book",
            kind=DataKind.BOOK_SNAPSHOT_L2,
            timeframe=Timeframe(mode=TimeframeMode.EVENT),
            fields=frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
        ),
        TrainableExample(
            manifest=A2CPairsStrategy.manifest,
            factory=A2CPairsStrategy,
            events=public_pair_test,
            dataset=_public_manifest(
                public_pair_test,
                public_pair_evidence,
                dataset_id="public.plotly.aapl-msft-2007-2016.a2c-pairs-test",
            ),
            training=_public_a2c_pair_training(public_pair_train),
        ),
        TrainableExample(
            manifest=LinearActorCriticAllocationStrategy.manifest,
            factory=LinearActorCriticAllocationStrategy,
            events=public_test,
            dataset=_public_manifest(
                public_test,
                public_evidence,
                dataset_id="public.plotly.aapl-2015-2017.actor-critic-test",
            ),
            training=_public_actor_critic_training(public_train),
        ),
    )


def all_examples() -> tuple[StrategyExample | TrainableExample, ...]:
    return rule_examples() + supervised_examples() + reinforcement_learning_examples()
