from __future__ import annotations

import json
import math
from collections import deque
from decimal import Decimal
from hashlib import sha256
from itertools import pairwise
from math import comb

import numpy as np
from numpy.typing import NDArray

from psrc.contract.models import ActionKind, DataKind, StrategyKind, TrainingMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, NoOp, Prediction, SubmitOrder, TargetPosition, TargetWeight
from psrc.domain.market import (
    BarPayload,
    BookSnapshotL2Payload,
    MarketEvent,
    QuoteL1Payload,
)
from psrc.runtime.artifacts import ArtifactManifest, ArtifactStore
from psrc.runtime.training import TrainingRequest
from psrc.strategies.common import (
    bar_requirement,
    canonicalize_numeric,
    event_requirement,
    make_manifest,
    stable_float,
)

Array = NDArray[np.float64]


def fit_binary_logistic(
    x: Array,
    y: Array,
    *,
    learning_rate: float,
    epochs: int,
    l1_penalty: float = 0.0,
) -> tuple[Array, float]:
    """Deterministic full-batch maximum-likelihood logistic regression."""
    weights = np.zeros(x.shape[1], dtype=np.float64)
    intercept = 0.0
    target = (y > 0).astype(np.float64)
    for _ in range(epochs):
        logits = np.clip(x @ weights + intercept, -30, 30)
        probability = 1 / (1 + np.exp(-logits))
        error = probability - target
        gradient = x.T @ error / len(x)
        if l1_penalty:
            gradient += l1_penalty * np.sign(weights)
        weights -= learning_rate * gradient
        intercept -= learning_rate * float(np.mean(error))
    return weights, intercept


def erlang_race_probability(
    own_queue: int,
    opposite_queue: int,
    own_death_rate: float,
    opposite_death_rate: float,
) -> float:
    """P(Erlang(own) < Erlang(opposite)), the constant-rate case of Proposition 2."""
    if min(own_queue, opposite_queue) < 1 or min(own_death_rate, opposite_death_rate) <= 0:
        raise ValueError("queue positions and death rates must be positive")
    total_rate = own_death_rate + opposite_death_rate
    own_share = own_death_rate / total_rate
    opposite_share = opposite_death_rate / total_rate
    return float(
        sum(
            comb(own_queue + k - 1, k) * own_share**own_queue * opposite_share**k
            for k in range(opposite_queue)
        )
    )


def erlang_fill_probability_within_horizon(
    own_queue: int,
    opposite_queue: int,
    own_death_rate: float,
    opposite_death_rate: float,
    horizon_seconds: float,
) -> float:
    """P(own Erlang queue depletes first and by the stated finite horizon)."""
    if horizon_seconds <= 0:
        raise ValueError("fill-probability horizon must be positive")
    if min(own_queue, opposite_queue) < 1 or min(own_death_rate, opposite_death_rate) <= 0:
        raise ValueError("queue positions and death rates must be positive")
    total_rate = own_death_rate + opposite_death_rate
    scaled_horizon = total_rate * horizon_seconds
    own_share = own_death_rate / total_rate
    opposite_share = opposite_death_rate / total_rate
    probability = 0.0
    for opposite_deaths in range(opposite_queue):
        shape = own_queue + opposite_deaths
        regularized_gamma = 1.0 - math.exp(-scaled_horizon) * sum(
            scaled_horizon**power / math.factorial(power) for power in range(shape)
        )
        probability += (
            comb(shape - 1, opposite_deaths)
            * own_share**own_queue
            * opposite_share**opposite_deaths
            * regularized_gamma
        )
    return float(probability)


def three_day_lagged_returns(closes: tuple[Decimal, ...]) -> tuple[float, float, float]:
    """Build a causal three-return window from exactly four consecutive closes."""
    if len(closes) != 4 or any(value <= 0 for value in closes):
        raise ValueError("three lagged returns require four positive closing prices")
    values = tuple(
        stable_float(float(right / left - 1)) for left, right in pairwise(closes)
    )
    return values[0], values[1], values[2]


def fit_pooled_ols(x: Array, y: Array) -> tuple[Array, float]:
    """Pooled OLS for the conditional return model in Gu, Kelly and Xiu."""
    design = np.column_stack([np.ones(len(x)), x])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    return coefficients[1:], float(coefficients[0])


class _JsonModelStrategy:
    manifest = make_manifest(
        strategy_id="supervised.abstract",
        kind=StrategyKind.SUPERVISED,
        entrypoint="invalid",
        profiles=frozenset({"training.supervised.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("SYNTH.DAILY",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP}),
        training=TrainingMode.REQUIRED,
    )

    def __init__(self) -> None:
        self.model: dict[str, object] | None = None
        self.artifact_id: str | None = None

    def _save(
        self, request: TrainingRequest, store: ArtifactStore, model: dict[str, object]
    ) -> ArtifactManifest:
        canonical_model = canonicalize_numeric(model)
        if not isinstance(canonical_model, dict):
            raise TypeError("canonical model must remain an object")
        payload = json.dumps(canonical_model, sort_keys=True, separators=(",", ":")).encode()
        artifact_id = f"sha256-{sha256(payload).hexdigest()}"
        return store.save_bytes(
            run_id=request.run_id,
            artifact_id=artifact_id,
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.strategy_version,
            artifact_kind="model",
            framework="numpy-json",
            logical_name="model.json",
            media_type="application/json",
            payload=payload,
            training_dataset_id=request.dataset_id,
            seed=request.seed,
            training_request_sha256=request.request_sha256,
            metadata={"algorithm": str(model["algorithm"])},
        )

    def load(self, manifest: ArtifactManifest, store: ArtifactStore, *, run_id: str) -> None:
        if manifest.strategy_id != self.manifest.strategy_id:
            raise ValueError("artifact strategy_id does not match strategy")
        payload = store.load_bytes(
            run_id=run_id, strategy_id=self.manifest.strategy_id, manifest=manifest
        )["model.json"]
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("model artifact root must be an object")
        self.model = value
        self.artifact_id = manifest.artifact_id

    def _require_model(self) -> tuple[dict[str, object], str]:
        if self.model is None or self.artifact_id is None:
            raise RuntimeError("model artifact has not been loaded")
        return self.model, self.artifact_id

    @staticmethod
    def _arrays(request: TrainingRequest, dimensions: int) -> tuple[Array, Array]:
        x = np.asarray(request.features, dtype=np.float64)
        y = np.asarray(request.labels, dtype=np.float64)
        if x.ndim != 2 or x.shape[1] != dimensions or len(x) != len(y) or len(y) < 4:
            raise ValueError(
                f"expected at least four rows with {dimensions} features and aligned labels"
            )
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ValueError("training data contains non-finite values")
        return x, y

    @staticmethod
    def _linear_score(model: dict[str, object], features: tuple[float, ...]) -> float:
        weights = np.asarray(model["weights"], dtype=np.float64)
        intercept = float(str(model.get("intercept", 0.0)))
        return float(np.dot(weights, np.asarray(features)) + intercept)

    def on_start(self) -> None:
        pass

    def on_finish(self) -> None:
        pass


class LogisticDirectionStrategy(_JsonModelStrategy):
    manifest = make_manifest(
        strategy_id="supervised.logistic_direction",
        kind=StrategyKind.SUPERVISED,
        entrypoint="psrc.strategies.supervised:LogisticDirectionStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1", "training.supervised.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("PUBLIC.AAPL",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("1"),
    )

    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        x, y = self._arrays(request, 5)
        weights, intercept = fit_binary_logistic(x, y, learning_rate=0.01, epochs=1000)
        return self._save(
            request,
            store,
            {
                "algorithm": "ohlcv-logistic-gradient-descent-v1",
                "weights": weights.tolist(),
                "intercept": intercept,
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        model, artifact_id = self._require_model()
        bar = event.payload
        features = (
            float((bar.close - bar.open) / bar.open),
            float((bar.high - bar.open) / bar.open),
            float((bar.low - bar.open) / bar.open),
            float((bar.high - bar.low) / bar.open),
            math.log1p(float(bar.volume)) / 10,
        )
        probability = stable_float(
            1 / (1 + math.exp(-max(-30.0, min(30.0, self._linear_score(model, features)))))
        )
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=Decimal(str(probability)),
                horizon="P1D",
                model_artifact_id=artifact_id,
                reason_code="model.logistic_probability",
            ),
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal("1") if probability >= 0.5 else Decimal("-1"),
                reason_code="allocation.logistic_threshold",
            ),
        )


class RidgeReturnStrategy(_JsonModelStrategy):
    manifest = make_manifest(
        strategy_id="supervised.ridge_return",
        kind=StrategyKind.SUPERVISED,
        entrypoint="psrc.strategies.supervised:RidgeReturnStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1", "training.supervised.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("PUBLIC.AAPL",), lookback=4),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("2"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.closes: deque[Decimal] = deque(maxlen=4)

    def on_start(self) -> None:
        self.closes.clear()

    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        x, y = self._arrays(request, 3)
        design = np.column_stack([np.ones(len(x)), x])
        penalty = np.eye(design.shape[1]) * 0.2
        penalty[0, 0] = 0
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ y)
        return self._save(
            request,
            store,
            {
                "algorithm": "ridge-closed-form-v1",
                "intercept": float(coefficients[0]),
                "weights": coefficients[1:].tolist(),
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        model, artifact_id = self._require_model()
        bar = event.payload
        self.closes.append(bar.close)
        if len(self.closes) < 4:
            return (
                NoOp(
                    reason_code="window.insufficient_history",
                    explanation="four closes are required for three lagged returns",
                ),
            )
        features = three_day_lagged_returns(tuple(self.closes))
        forecast = stable_float(self._linear_score(model, features))
        target = (
            Decimal("2")
            if forecast > 0.00025
            else Decimal("-2")
            if forecast < -0.00025
            else Decimal("0")
        )
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=Decimal(str(forecast)),
                horizon="P1D",
                model_artifact_id=artifact_id,
                reason_code="model.ridge_return",
            ),
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=target,
                reason_code="allocation.forecast_band",
            ),
        )


class GaussianVolumeBreakoutStrategy(_JsonModelStrategy):
    manifest = make_manifest(
        strategy_id="supervised.gaussian_volume_breakout",
        kind=StrategyKind.SUPERVISED,
        entrypoint="psrc.strategies.supervised:GaussianVolumeBreakoutStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1", "training.supervised.v1"}),
        data=(bar_requirement(interval="PT1M", symbols=("SYNTH.TWAP",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("1"),
    )

    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        x, y = self._arrays(request, 2)
        labels = (y > 0).astype(int)
        if set(labels.tolist()) != {0, 1}:
            raise ValueError("Gaussian classifier requires both label classes")
        classes: dict[str, object] = {}
        for label in (0, 1):
            subset = x[labels == label]
            classes[str(label)] = {
                "mean": subset.mean(axis=0).tolist(),
                "variance": (subset.var(axis=0) + 1e-6).tolist(),
                "prior": float(len(subset) / len(x)),
            }
        return self._save(
            request,
            store,
            {"algorithm": "gaussian-naive-bayes-v1", "classes": classes},
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires minute bars"),)
        model, artifact_id = self._require_model()
        bar = event.payload
        features = np.asarray(
            [math.log1p(float(bar.volume)) / 10, float((bar.high - bar.low) / bar.open)]
        )
        scores: dict[int, float] = {}
        classes = model["classes"]
        assert isinstance(classes, dict)
        for label in (0, 1):
            parameters = classes[str(label)]
            assert isinstance(parameters, dict)
            mean = np.asarray(parameters["mean"])
            variance = np.asarray(parameters["variance"])
            scores[label] = float(
                math.log(float(parameters["prior"]))
                - 0.5 * np.sum(np.log(2 * math.pi * variance) + (features - mean) ** 2 / variance)
            )
        probability = stable_float(1 / (1 + math.exp(scores[0] - scores[1])))
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=Decimal(str(probability)),
                horizon="PT5M",
                model_artifact_id=artifact_id,
                reason_code="model.gaussian_breakout_probability",
            ),
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal("1") if probability > 0.55 else Decimal("0"),
                reason_code="allocation.breakout_probability",
            ),
        )


class L1AdverseSelectionStrategy(_JsonModelStrategy):
    manifest = make_manifest(
        strategy_id="supervised.l1_adverse_selection",
        kind=StrategyKind.SUPERVISED,
        entrypoint="psrc.strategies.supervised:L1AdverseSelectionStrategy",
        profiles=frozenset({"event.l1.v1", "execution.basic.v1", "training.supervised.v1"}),
        data=(
            event_requirement(
                stream_id="quotes",
                kind=DataKind.QUOTE_L1,
                symbols=("SYNTH.L1",),
                fields=frozenset({"bid_price", "bid_size", "ask_price", "ask_size"}),
            ),
        ),
        actions=frozenset({ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("1"),
    )

    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        x, y = self._arrays(request, 3)
        # Gould and Bonart regress the next-move indicator on queue imbalance alone.
        imbalance = x[:, :1]
        fitted, intercept = fit_binary_logistic(imbalance, y, learning_rate=0.1, epochs=1000)
        return self._save(
            request,
            store,
            {
                "algorithm": "queue-imbalance-logistic-mle-v1",
                "weights": fitted.tolist(),
                "intercept": intercept,
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, QuoteL1Payload):
            return (NoOp(reason_code="event.not_l1", explanation="requires L1 quotes"),)
        model, artifact_id = self._require_model()
        quote = event.payload
        total = max(quote.bid_size + quote.ask_size, Decimal("1"))
        features = (float((quote.bid_size - quote.ask_size) / total),)
        margin = self._linear_score(model, features)
        probability = stable_float(1 / (1 + math.exp(-max(-30.0, min(30.0, margin)))))
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=Decimal(str(probability)),
                horizon="PT1S",
                model_artifact_id=artifact_id,
                reason_code="model.queue_imbalance_logistic",
            ),
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal("-1") if probability > 0.5 else Decimal("1"),
                reason_code="allocation.avoid_adverse_side",
            ),
        )


class L2FillProbabilityStrategy(_JsonModelStrategy):
    manifest = make_manifest(
        strategy_id="supervised.l2_fill_probability",
        kind=StrategyKind.SUPERVISED,
        entrypoint="psrc.strategies.supervised:L2FillProbabilityStrategy",
        profiles=frozenset({"event.l2.v1", "execution.basic.v1", "training.supervised.v1"}),
        data=(
            event_requirement(
                stream_id="book",
                kind=DataKind.BOOK_SNAPSHOT_L2,
                symbols=("SYNTH.L2",),
                fields=frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
                depth=3,
            ),
        ),
        actions=frozenset({ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.SUBMIT_ORDER}),
        training=TrainingMode.REQUIRED,
        max_order=Decimal("1"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.counter = 0

    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        x, y = self._arrays(request, 3)
        del y
        exposure = x[:, 2]
        if np.any(x[:, :2] < 0) or np.any(exposure <= 0):
            raise ValueError("queue-death counts must be non-negative and exposure positive")
        total_exposure = float(exposure.sum())
        own_rate = float(x[:, 0].sum() / total_exposure)
        opposite_rate = float(x[:, 1].sum() / total_exposure)
        if min(own_rate, opposite_rate) <= 0:
            raise ValueError("calibrated queue-death rates must be positive")
        return self._save(
            request,
            store,
            {
                "algorithm": "constant-rate-queue-race-v1",
                "own_death_rate": own_rate,
                "opposite_death_rate": opposite_rate,
            },
        )

    def on_start(self) -> None:
        self.counter = 0

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BookSnapshotL2Payload):
            return (NoOp(reason_code="event.not_l2", explanation="requires L2 book"),)
        model, artifact_id = self._require_model()
        book = event.payload
        own_death_rate = model["own_death_rate"]
        opposite_death_rate = model["opposite_death_rate"]
        if not isinstance(own_death_rate, (int, float)) or not isinstance(
            opposite_death_rate, (int, float)
        ):
            raise ValueError("queue-race artifact rates must be numeric")
        own_queue = max(1, round(float(book.bids[0].size)))
        opposite_queue = max(1, round(float(book.asks[0].size)))
        probability = stable_float(
            erlang_fill_probability_within_horizon(
                own_queue,
                opposite_queue,
                float(own_death_rate),
                float(opposite_death_rate),
                0.5,
            )
        )
        self.counter += 1
        passive = probability >= 0.5
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=Decimal(str(probability)),
                horizon="PT500MS",
                model_artifact_id=artifact_id,
                reason_code="model.limit_fill_probability",
            ),
            SubmitOrder(
                client_order_id=f"fill:{self.counter}",
                instrument_id=event.instrument_id,
                side="buy",
                order_type="limit" if passive else "market",
                quantity=Decimal("1"),
                limit_price=book.bids[0].price if passive else None,
                reason_code=(
                    "execution.passive_fill_probability"
                    if passive
                    else "execution.aggressive_fill_probability"
                ),
            ),
        )


class CrossSectionalRankerStrategy(_JsonModelStrategy):
    manifest = make_manifest(
        strategy_id="supervised.cross_sectional_ranker",
        kind=StrategyKind.SUPERVISED,
        entrypoint="psrc.strategies.supervised:CrossSectionalRankerStrategy",
        profiles=frozenset({"core.bar.v1", "portfolio.batch.v1", "training.supervised.v1"}),
        data=(
            bar_requirement(
                interval="P1D",
                symbols=("SYNTH.XS-A", "SYNTH.XS-B", "SYNTH.XS-C"),
                lookback=2,
            ),
        ),
        actions=frozenset({ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_WEIGHT}),
        training=TrainingMode.REQUIRED,
        max_position=None,
    )

    def __init__(self) -> None:
        super().__init__()
        self.symbols = ("SYNTH.XS-A", "SYNTH.XS-B", "SYNTH.XS-C")
        self.current: dict[str, tuple[object, tuple[float, ...]]] = {}

    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        x, y = self._arrays(request, 3)
        weights, intercept = fit_pooled_ols(x, y)
        return self._save(
            request,
            store,
            {
                "algorithm": "pooled-ols-cross-sectional-ranker-v1",
                "weights": weights.tolist(),
                "intercept": intercept,
            },
        )

    def on_start(self) -> None:
        self.current.clear()

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        model, artifact_id = self._require_model()
        bar = event.payload
        features = (
            float(bar.close / bar.open - 1),
            float((bar.high - bar.low) / bar.open),
            math.log1p(float(bar.volume)) / 10,
        )
        self.current[event.instrument_id] = (event.available_time, features)
        if any(symbol not in self.current for symbol in self.symbols):
            return (
                NoOp(reason_code="cross_section.awaiting_symbols", explanation="batch incomplete"),
            )
        if len({self.current[symbol][0] for symbol in self.symbols}) != 1:
            return (NoOp(reason_code="cross_section.not_synchronized", explanation="times differ"),)
        scores = {
            symbol: stable_float(self._linear_score(model, self.current[symbol][1]))
            for symbol in self.symbols
        }
        ranked = sorted(scores, key=lambda symbol: scores[symbol])
        weights = {ranked[0]: Decimal("-0.5"), ranked[1]: Decimal("0"), ranked[2]: Decimal("0.5")}
        actions: list[Action] = []
        for symbol in self.symbols:
            actions.extend(
                (
                    Prediction(
                        instrument_id=symbol,
                        value=Decimal(str(scores[symbol])),
                        horizon="P1D",
                        model_artifact_id=artifact_id,
                        reason_code="model.cross_section_score",
                    ),
                    TargetWeight(
                        instrument_id=symbol,
                        weight=weights[symbol],
                        reason_code="allocation.long_short_rank",
                    ),
                )
            )
        return tuple(actions)
