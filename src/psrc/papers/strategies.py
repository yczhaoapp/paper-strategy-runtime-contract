from __future__ import annotations

import json
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Literal, cast

from psrc.contract.hashing import sha256_model
from psrc.contract.models import StrategyManifest
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, CancelOrder, NoOp, Prediction, SubmitOrder, TargetPosition
from psrc.domain.market import BarPayload, MarketEvent, QuoteL1Payload
from psrc.papers.algorithms import (
    backward_q,
    fit_logistic,
    imbalance,
    probability,
    reservation_quotes,
)
from psrc.runtime.artifacts import ArtifactManifest, ArtifactStore
from psrc.runtime.training import TrainingRequest


class PaperStrategy:
    manifest: StrategyManifest
    parameters: dict[str, float]

    def __init__(self) -> None:
        self.model: dict[str, Any] | None = None
        self.artifact_id: str | None = None
        self.counter = 0

    def on_start(self) -> None:
        self.counter = 0

    def on_finish(self) -> None:
        pass

    def save(
        self, request: TrainingRequest, store: ArtifactStore, model: dict[str, Any]
    ) -> ArtifactManifest:
        provenance = {
            "strategy": self.manifest.strategy_id,
            "parameters": self.parameters,
            "request": request.model_dump(mode="json"),
            "model": model,
        }
        return store.save_bytes(
            run_id=request.run_id,
            artifact_id="sha256-" + sha256_model(provenance),
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.strategy_version,
            artifact_kind="model",
            framework="psrc-paper-json.v1",
            logical_name="model.json",
            media_type="application/json",
            payload=json.dumps(model, sort_keys=True).encode(),
            training_dataset_id=request.dataset_id,
            seed=request.seed,
            training_request_sha256=request.request_sha256,
            metadata={
                "request_sha256": sha256_model(request),
                "parameters_sha256": sha256_model(self.parameters),
            },
        )

    def load(self, manifest: ArtifactManifest, store: ArtifactStore, *, run_id: str) -> None:
        if manifest.strategy_id != self.manifest.strategy_id:
            raise ValueError("artifact belongs to another strategy")
        if manifest.metadata.get("parameters_sha256") != sha256_model(self.parameters):
            raise ValueError("artifact parameters differ from compiled specification")
        self.model = json.loads(
            store.load_bytes(
                run_id=run_id, strategy_id=self.manifest.strategy_id, manifest=manifest
            )["model.json"]
        )
        self.artifact_id = manifest.artifact_id

    @staticmethod
    def quote(event: MarketEvent) -> QuoteL1Payload:
        if not isinstance(event.payload, QuoteL1Payload):
            raise ValueError("paper strategy requires L1 quote events")
        return event.payload

    @staticmethod
    def position(account: AccountSnapshot, symbol: str) -> Decimal:
        return next(
            (p.quantity for p in account.positions if p.instrument_id == symbol), Decimal(0)
        )

    @staticmethod
    def cancellations(account: AccountSnapshot) -> list[Action]:
        return [
            CancelOrder(client_order_id=o.client_order_id, reason_code="paper.refresh")
            for o in account.open_orders
        ]


class AvellanedaStrategy(PaperStrategy):
    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        q = self.quote(event)
        self.counter += 1
        p = self.parameters
        inventory = self.position(account, event.instrument_id)
        remaining = max(0.0, 1 - (self.counter - 1) / p["horizon_events"])
        bid, ask = reservation_quotes(
            float((q.bid_price + q.ask_price) / 2),
            float(inventory),
            p["gamma"],
            p["sigma"],
            p["k"],
            remaining,
        )
        tick = Decimal(str(p["tick"]))
        actions = self.cancellations(account)
        for side, price, rounding in (("buy", bid, ROUND_FLOOR), ("sell", ask, ROUND_CEILING)):
            if (side == "buy" and inventory >= p["inventory_limit"]) or (
                side == "sell" and inventory <= -p["inventory_limit"]
            ):
                continue
            rounded = (Decimal(str(price)) / tick).to_integral_value(rounding=rounding) * tick
            actions.append(
                SubmitOrder(
                    client_order_id=f"paper.as:{self.counter}:{side}",
                    instrument_id=event.instrument_id,
                    side=cast(Literal["buy", "sell"], side),
                    order_type="limit",
                    quantity=Decimal(1),
                    limit_price=rounded,
                    reason_code="paper.as.eq29-30",
                )
            )
        return tuple(actions)


class QueueLogisticStrategy(PaperStrategy):
    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        model = fit_logistic(
            request.features,
            request.labels,
            int(self.parameters["iterations"]),
            self.parameters["tolerance"],
        )
        return self.save(request, store, model)

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if self.model is None or self.artifact_id is None:
            raise ValueError("load a trained queue model before inference")
        q = self.quote(event)
        value = imbalance(float(q.bid_size), float(q.ask_size))
        prob = probability(self.model["intercept"], self.model["slope"], value)
        threshold = self.parameters["trade_threshold"]
        target = 1 if prob > threshold else -1 if prob < 1 - threshold else 0
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=Decimal(str(prob)),
                horizon="next-midprice-change",
                model_artifact_id=self.artifact_id,
                reason_code="paper.queue.eq17",
            ),
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal(target),
                reason_code="extension.probability-threshold",
            ),
        )


class ExecutionQStrategy(PaperStrategy):
    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest:
        table = backward_q(request.transitions)
        return self.save(
            request,
            store,
            {
                "algorithm": "finite-horizon-empirical-bellman-v1",
                "q": table,
                "unseen_state": "error",
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if self.model is None:
            raise ValueError("load a trained execution policy before inference")
        q = self.quote(event)
        remaining = int(
            Decimal(str(self.parameters["target_quantity"]))
            - self.position(account, event.instrument_id)
        )
        periods = int(self.parameters["horizon_events"]) - self.counter
        self.counter += 1
        actions = self.cancellations(account)
        if remaining == 0:
            return (*actions, NoOp(reason_code="execution.complete", explanation="target reached"))
        if remaining < 0 or periods <= 0:
            raise ValueError("execution horizon expired with incomplete or excess inventory")
        market = 1 if q.bid_size >= q.ask_size else -1
        key = f"{periods},{remaining},{market}"
        if key not in self.model["q"]:
            raise ValueError(f"unseen execution state: {key}")
        values = self.model["q"][key]
        action = max(range(3), key=lambda a: (values[a], -a))
        # Deadline market execution is part of the declared environment, not a fallback.
        market_order = periods == 1 or action == 2
        limit = q.bid_price + (Decimal(str(self.parameters["tick"])) if action == 1 else 0)
        actions.append(
            SubmitOrder(
                client_order_id=f"paper.execution:{self.counter}",
                instrument_id=event.instrument_id,
                side="buy",
                order_type="market" if market_order else "limit",
                quantity=Decimal(remaining),
                limit_price=None if market_order else limit,
                reason_code="paper.execution.deadline"
                if periods == 1
                else "paper.execution.bellman",
            )
        )
        return tuple(actions)


class MovingAverageBandStrategy(PaperStrategy):
    def __init__(self) -> None:
        super().__init__()
        self.closes: list[Decimal] = []

    def on_start(self) -> None:
        self.closes.clear()

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            raise ValueError("moving averages require completed bars")
        self.closes.append(event.payload.close)
        short = int(self.parameters["short_window"])
        long = int(self.parameters["long_window"])
        self.closes = self.closes[-long:]
        if len(self.closes) < long:
            return (NoOp(reason_code="paper.ma.warmup", explanation="long window incomplete"),)
        short_mean = sum(self.closes[-short:], Decimal(0)) / short
        long_mean = sum(self.closes, Decimal(0)) / long
        band = Decimal(str(self.parameters["band"]))
        target = (
            1
            if short_mean > long_mean * (1 + band)
            else -1
            if short_mean < long_mean * (1 - band)
            else 0
        )
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal(target),
                reason_code="paper.ma.eq1-4",
            ),
        )
