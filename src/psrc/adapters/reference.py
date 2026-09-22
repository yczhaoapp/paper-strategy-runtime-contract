from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, NoReturn

from psrc.adapters.base import BacktestAdapter
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import (
    ActionKind,
    DataKind,
    EngineCapabilities,
    ExecutionPlan,
    ExecutionSemantics,
    SandboxMode,
    SupportLevel,
)
from psrc.domain.account import AccountSnapshot, Fill, OpenOrder, Position
from psrc.domain.actions import (
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
    BookSnapshotL2Payload,
    MarketEvent,
    QuoteL1Payload,
    TradePayload,
)
from psrc.runtime.guards import validate_actions
from psrc.runtime.report import (
    DecisionRecord,
    OrderEventRecord,
    RunMetrics,
    RunReport,
    RuntimeLogRecord,
)
from psrc.runtime.strategy import RuntimeStrategy


def capabilities(*, strict_container: bool = False) -> EngineCapabilities:
    sandbox_modes = {SandboxMode.DEVELOPMENT}
    if strict_container:
        sandbox_modes.add(SandboxMode.STRICT_CONTAINER)
    return EngineCapabilities(
        engine_id="reference",
        engine_version="0.4.0",
        adapter_version="0.4.0",
        support_level=SupportLevel.CONFORMANCE_VERIFIED,
        profiles=frozenset(
            {
                "core.bar.v1",
                "event.trade.v1",
                "event.l1.v1",
                "event.l2.v1",
                "execution.basic.v1",
                "execution.advanced.v1",
                "portfolio.batch.v1",
                "training.supervised.v1",
                "training.rl.v1",
            }
        ),
        data_kinds=frozenset(
            {
                DataKind.BAR,
                DataKind.TRADE,
                DataKind.QUOTE_L1,
                DataKind.BOOK_SNAPSHOT_L2,
            }
        ),
        action_kinds=frozenset(
            {
                ActionKind.NO_OP,
                ActionKind.PREDICTION,
                ActionKind.TARGET_POSITION,
                ActionKind.TARGET_WEIGHT,
                ActionKind.SUBMIT_ORDER,
                ActionKind.CANCEL_ORDER,
                ActionKind.REPLACE_ORDER,
            }
        ),
        execution=ExecutionSemantics(
            partial_fills=False,
            cancel=True,
            replace="native",
            same_timestamp_ordering="existing_orders_then_market_then_strategy_then_new_commands",
            queue_model="none-full-fill-only.v1",
            fill_model="reference.next-event.v2",
            fee_model="proportional.v1",
            slippage_model="fixed-bps-market-only.v1",
            latency_model="next-event.v1",
        ),
        sandbox_modes=frozenset(sandbox_modes),
        extensions={
            "org.psrc.reference": {
                "supported_time_in_force": ["day"],
                "day_session": {
                    "calendar": "24/7",
                    "timezone": "UTC",
                    "expires_before_first_event_on_next_utc_date": True,
                },
            }
        },
    )


@dataclass(frozen=True)
class _PendingOrder:
    client_order_id: str
    instrument_id: str
    order_type: Literal["target", "market", "limit"]
    submitted_at: datetime
    side: Literal["buy", "sell"] | None = None
    quantity: Decimal | None = None
    target_quantity: Decimal | None = None
    limit_price: Decimal | None = None
    time_in_force: Literal["day"] | None = None
    status: Literal["accepted", "replaced", "partially_filled"] = "accepted"


class ReferenceEngine(BacktestAdapter):
    """Deterministic event engine with explicit next-event execution semantics."""

    def __init__(
        self,
        *,
        initial_cash: Decimal = Decimal("100000"),
        fee_rate: Decimal = Decimal("0.0005"),
        slippage_bps: Decimal = Decimal("1"),
        declared_capabilities: EngineCapabilities | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if fee_rate < 0 or slippage_bps < 0:
            raise ValueError("fees and slippage must be non-negative")
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self._capabilities = declared_capabilities or capabilities()
        if self._capabilities.engine_id != "reference":
            raise ValueError("ReferenceEngine capabilities must declare engine_id='reference'")

    @property
    def capabilities(self) -> EngineCapabilities:
        return self._capabilities

    def _run_validated(
        self,
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        events: tuple[MarketEvent, ...],
        sandbox_mode: SandboxMode,
    ) -> RunReport:
        started_at = datetime.now(UTC)
        self._validate_direct_order_session(plan, strategy)
        universe = frozenset(event.instrument_id for event in events)

        cash = self.initial_cash
        quantities: dict[str, Decimal] = {}
        average_prices: dict[str, Decimal] = {}
        realized_pnl: dict[str, Decimal] = {}
        marks: dict[str, Decimal] = {}
        pending: list[_PendingOrder] = []
        fills: list[Fill] = []
        decisions: list[DecisionRecord] = []
        order_events: list[OrderEventRecord] = []
        snapshots: list[AccountSnapshot] = []
        order_count = 0
        no_op_count = 0

        strategy.on_start()
        for event_index, event in enumerate(events):
            marks[event.instrument_id] = self._mark(event)
            pending, cash = self._settle_pending(
                plan=plan,
                strategy=strategy,
                event=event,
                pending=pending,
                cash=cash,
                quantities=quantities,
                average_prices=average_prices,
                realized_pnl=realized_pnl,
                fills=fills,
                order_events=order_events,
            )

            snapshot = self._snapshot(
                timestamp=event.available_time,
                cash=cash,
                quantities=quantities,
                average_prices=average_prices,
                realized_pnl=realized_pnl,
                marks=marks,
                pending=pending,
            )
            snapshots.append(snapshot)
            actions = strategy.on_event(event, snapshot)
            validate_actions(plan, strategy, actions, universe)
            decisions.append(
                DecisionRecord(
                    sequence=event_index,
                    event_id=event.event_id,
                    event_time=event.available_time,
                    actions=actions,
                )
            )

            for action in actions:
                if isinstance(action, NoOp):
                    no_op_count += 1
                    continue
                if isinstance(action, Prediction):
                    continue
                if isinstance(action, CancelOrder):
                    pending = self._cancel(plan, event, action, pending, order_events)
                    continue
                if isinstance(action, ReplaceOrder):
                    pending = self._replace(
                        plan,
                        strategy,
                        event,
                        action,
                        pending,
                        quantities,
                        order_events,
                    )
                    continue

                instrument_id = action.instrument_id
                if instrument_id not in universe:
                    self._fail_action(
                        plan,
                        "Action refers to an instrument absent from the run universe",
                        {"instrument_id": instrument_id},
                    )

                if isinstance(action, TargetWeight):
                    mark = marks.get(instrument_id)
                    if mark is None or mark <= 0:
                        self._fail_action(
                            plan,
                            "TargetWeight cannot be resolved without a current positive mark",
                            {"instrument_id": instrument_id},
                        )
                    target = (action.weight * snapshot.equity / mark).quantize(Decimal("0.000001"))
                    action = TargetPosition(
                        instrument_id=instrument_id,
                        quantity=target,
                        reason_code=action.reason_code,
                    )

                if isinstance(action, TargetPosition):
                    self._validate_target(plan, strategy, action)
                    order_count += 1
                    order = _PendingOrder(
                        client_order_id=f"order:{order_count}",
                        instrument_id=action.instrument_id,
                        order_type="target",
                        submitted_at=event.available_time,
                        target_quantity=action.quantity,
                    )
                elif isinstance(action, SubmitOrder):
                    if action.time_in_force != "day":
                        self._fail_action(
                            plan,
                            "Reference engine supports day time-in-force only",
                            {
                                "time_in_force": action.time_in_force,
                                "supported": ["day"],
                            },
                            code=ErrorCode.ORDER_REJECTED,
                        )
                    if action.order_type == "market":
                        direct_order_type: Literal["market", "limit"] = "market"
                    elif action.order_type == "limit":
                        direct_order_type = "limit"
                    else:
                        self._fail_action(
                            plan,
                            "Reference engine supports direct market and limit orders only",
                            {"order_type": action.order_type},
                            code=ErrorCode.ORDER_REJECTED,
                        )
                    if any(item.client_order_id == action.client_order_id for item in pending):
                        self._fail_action(
                            plan,
                            "Duplicate client_order_id is not allowed",
                            {"client_order_id": action.client_order_id},
                            code=ErrorCode.ORDER_REJECTED,
                        )
                    maximum = strategy.manifest.action_requirements.max_order_quantity
                    if maximum is not None and action.quantity > maximum:
                        self._fail_action(
                            plan,
                            "Order quantity exceeds the manifest limit",
                            {"requested": str(action.quantity), "maximum": str(maximum)},
                            code=ErrorCode.ORDER_REJECTED,
                        )
                    order_count += 1
                    order = _PendingOrder(
                        client_order_id=action.client_order_id,
                        instrument_id=action.instrument_id,
                        order_type=direct_order_type,
                        submitted_at=event.available_time,
                        side=action.side,
                        quantity=action.quantity,
                        limit_price=action.limit_price,
                        time_in_force="day",
                    )
                else:
                    self._fail_action(
                        plan,
                        "Action variant is not implemented by the reference engine",
                        {"action": action.model_dump(mode="json")},
                    )
                self._validate_projected_position(
                    plan=plan,
                    strategy=strategy,
                    pending=[*pending, order],
                    quantities=quantities,
                    instrument_id=order.instrument_id,
                )
                pending.append(order)
                order_events.append(
                    OrderEventRecord(
                        sequence=len(order_events),
                        client_order_id=order.client_order_id,
                        instrument_id=order.instrument_id,
                        event_time=event.available_time,
                        status="accepted",
                        details={"order_type": order.order_type},
                    )
                )

        strategy.on_finish()
        final_snapshot = snapshots[-1]
        for order in pending:
            order_events.append(
                OrderEventRecord(
                    sequence=len(order_events),
                    client_order_id=order.client_order_id,
                    instrument_id=order.instrument_id,
                    event_time=events[-1].available_time,
                    status="expired",
                    details={"reason": "end_of_backtest"},
                )
            )
        total_return = (final_snapshot.equity - self.initial_cash) / self.initial_cash
        return RunReport(
            run_id=plan.run_id,
            status="succeeded",
            execution_plan=plan,
            sandbox_mode=sandbox_mode,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            metrics=RunMetrics(
                initial_cash=self.initial_cash,
                final_cash=final_snapshot.cash,
                final_equity=final_snapshot.equity,
                total_return=total_return,
                decisions=len(decisions),
                orders=order_count,
                fills=len(fills),
                no_ops=no_op_count,
            ),
            decisions=tuple(decisions),
            orders=tuple(order_events),
            fills=tuple(fills),
            account_snapshots=tuple(snapshots),
            assumptions=(
                "Decisions become eligible on the next event for their instrument.",
                "The reference engine uses full fills only and has no queue-position model.",
                "Resting limit orders fill only when the next event proves their price condition.",
                "Direct day orders expire before matching on the first event of a later UTC date.",
                f"fee_rate={self.fee_rate}; market_slippage_bps={self.slippage_bps}",
            ),
            logs=(
                RuntimeLogRecord(
                    sequence=0,
                    timestamp=started_at,
                    level="info",
                    stage="backtest",
                    message="Reference engine run started",
                ),
                RuntimeLogRecord(
                    sequence=1,
                    timestamp=datetime.now(UTC),
                    level="info",
                    stage="backtest",
                    message="Reference engine run completed",
                    fields={"decisions": len(decisions), "fills": len(fills)},
                ),
            ),
        )

    def _settle_pending(
        self,
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        event: MarketEvent,
        pending: list[_PendingOrder],
        cash: Decimal,
        quantities: dict[str, Decimal],
        average_prices: dict[str, Decimal],
        realized_pnl: dict[str, Decimal],
        fills: list[Fill],
        order_events: list[OrderEventRecord],
    ) -> tuple[list[_PendingOrder], Decimal]:
        remaining: list[_PendingOrder] = []
        for order in pending:
            if self._day_order_expired(order, event.available_time):
                order_events.append(
                    OrderEventRecord(
                        sequence=len(order_events),
                        client_order_id=order.client_order_id,
                        instrument_id=order.instrument_id,
                        event_time=event.available_time,
                        status="expired",
                        details={"reason": "day_session_end"},
                    )
                )
                continue
            if order.instrument_id != event.instrument_id:
                remaining.append(order)
                continue
            resolved = self._resolve_fill(order, event, quantities)
            if resolved is None:
                remaining.append(order)
                continue
            if resolved == "satisfied":
                order_events.append(
                    OrderEventRecord(
                        sequence=len(order_events),
                        client_order_id=order.client_order_id,
                        instrument_id=order.instrument_id,
                        event_time=event.available_time,
                        status="satisfied",
                        details={"reason": "target_already_reached"},
                    )
                )
                continue
            side, quantity, raw_price = resolved
            price = (
                self._apply_slippage(raw_price, side) if order.order_type != "limit" else raw_price
            )
            fee = quantity * price * self.fee_rate
            delta = quantity if side == "buy" else -quantity
            maximum = strategy.manifest.action_requirements.max_abs_position
            resulting = quantities.get(order.instrument_id, Decimal("0")) + delta
            if maximum is not None and abs(resulting) > maximum:
                self._fail_action(
                    plan,
                    "Order fill would exceed the manifest position limit",
                    {
                        "client_order_id": order.client_order_id,
                        "current": str(quantities.get(order.instrument_id, Decimal("0"))),
                        "delta": str(delta),
                        "resulting": str(resulting),
                        "maximum": str(maximum),
                    },
                    code=ErrorCode.ORDER_REJECTED,
                )
            cash += (-quantity * price if side == "buy" else quantity * price) - fee
            self._update_position(
                instrument_id=order.instrument_id,
                delta=delta,
                price=price,
                quantities=quantities,
                average_prices=average_prices,
                realized_pnl=realized_pnl,
            )
            fills.append(
                Fill(
                    fill_id=f"fill:{len(fills) + 1}",
                    client_order_id=order.client_order_id,
                    instrument_id=order.instrument_id,
                    timestamp=event.available_time,
                    side=side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                )
            )
            order_events.append(
                OrderEventRecord(
                    sequence=len(order_events),
                    client_order_id=order.client_order_id,
                    instrument_id=order.instrument_id,
                    event_time=event.available_time,
                    status="filled",
                    details={"fill_id": fills[-1].fill_id},
                )
            )
        return remaining, cash

    @staticmethod
    def _resolve_fill(
        order: _PendingOrder,
        event: MarketEvent,
        quantities: dict[str, Decimal],
    ) -> tuple[Literal["buy", "sell"], Decimal, Decimal] | Literal["satisfied"] | None:
        if order.order_type == "target":
            assert order.target_quantity is not None
            delta = order.target_quantity - quantities.get(order.instrument_id, Decimal("0"))
            if delta == 0:
                return "satisfied"
            side: Literal["buy", "sell"] = "buy" if delta > 0 else "sell"
            return side, abs(delta), ReferenceEngine._market_price(event, side)

        assert order.side is not None and order.quantity is not None
        if order.order_type == "market":
            return order.side, order.quantity, ReferenceEngine._market_price(event, order.side)

        assert order.limit_price is not None
        fill_price = ReferenceEngine._limit_fill_price(event, order.side, order.limit_price)
        if fill_price is None:
            return None
        return order.side, order.quantity, fill_price

    @staticmethod
    def _mark(event: MarketEvent) -> Decimal:
        payload = event.payload
        if isinstance(payload, BarPayload):
            return payload.close
        if isinstance(payload, TradePayload):
            return payload.price
        if isinstance(payload, QuoteL1Payload):
            return (payload.bid_price + payload.ask_price) / 2
        if isinstance(payload, BookSnapshotL2Payload):
            return (payload.bids[0].price + payload.asks[0].price) / 2
        raise AssertionError("validated market payload is unsupported")

    @staticmethod
    def _market_price(event: MarketEvent, side: Literal["buy", "sell"]) -> Decimal:
        payload = event.payload
        if isinstance(payload, BarPayload):
            return payload.open
        if isinstance(payload, TradePayload):
            return payload.price
        if isinstance(payload, QuoteL1Payload):
            return payload.ask_price if side == "buy" else payload.bid_price
        if isinstance(payload, BookSnapshotL2Payload):
            return payload.asks[0].price if side == "buy" else payload.bids[0].price
        raise AssertionError("validated market payload is unsupported")

    @staticmethod
    def _limit_fill_price(
        event: MarketEvent, side: Literal["buy", "sell"], limit: Decimal
    ) -> Decimal | None:
        payload = event.payload
        if isinstance(payload, BarPayload):
            touched = payload.low <= limit if side == "buy" else payload.high >= limit
            return limit if touched else None
        if isinstance(payload, TradePayload):
            touched = payload.price <= limit if side == "buy" else payload.price >= limit
            return payload.price if touched else None
        market = ReferenceEngine._market_price(event, side)
        marketable = market <= limit if side == "buy" else market >= limit
        return market if marketable else None

    def _apply_slippage(self, raw_price: Decimal, side: Literal["buy", "sell"]) -> Decimal:
        slip = self.slippage_bps / Decimal("10000")
        return raw_price * (Decimal("1") + slip if side == "buy" else Decimal("1") - slip)

    @staticmethod
    def _cancel(
        plan: ExecutionPlan,
        event: MarketEvent,
        action: CancelOrder,
        pending: list[_PendingOrder],
        order_events: list[OrderEventRecord],
    ) -> list[_PendingOrder]:
        matched = next(
            (order for order in pending if order.client_order_id == action.client_order_id), None
        )
        if matched is None:
            ReferenceEngine._fail_action(
                plan,
                "Cannot cancel an unknown or terminal order",
                {"client_order_id": action.client_order_id},
            )
        order_events.append(
            OrderEventRecord(
                sequence=len(order_events),
                client_order_id=matched.client_order_id,
                instrument_id=matched.instrument_id,
                event_time=event.available_time,
                status="canceled",
                details={"reason_code": action.reason_code},
            )
        )
        return [order for order in pending if order.client_order_id != action.client_order_id]

    @staticmethod
    def _replace(
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        event: MarketEvent,
        action: ReplaceOrder,
        pending: list[_PendingOrder],
        quantities: dict[str, Decimal],
        order_events: list[OrderEventRecord],
    ) -> list[_PendingOrder]:
        matched = next(
            (order for order in pending if order.client_order_id == action.client_order_id), None
        )
        if matched is None or matched.order_type != "limit":
            ReferenceEngine._fail_action(
                plan,
                "Only a live limit order can be replaced",
                {"client_order_id": action.client_order_id},
            )
        updated = replace(
            matched,
            quantity=(action.new_quantity if action.new_quantity is not None else matched.quantity),
            limit_price=(
                action.new_limit_price
                if action.new_limit_price is not None
                else matched.limit_price
            ),
            status="replaced",
        )
        maximum = strategy.manifest.action_requirements.max_order_quantity
        if maximum is not None and updated.quantity is not None and updated.quantity > maximum:
            ReferenceEngine._fail_action(
                plan,
                "Replacement quantity exceeds the manifest limit",
                {
                    "client_order_id": action.client_order_id,
                    "requested": str(updated.quantity),
                    "maximum": str(maximum),
                },
                code=ErrorCode.ORDER_REJECTED,
            )
        updated_pending = [
            updated if order.client_order_id == action.client_order_id else order
            for order in pending
        ]
        ReferenceEngine._validate_projected_position(
            plan=plan,
            strategy=strategy,
            pending=updated_pending,
            quantities=quantities,
            instrument_id=updated.instrument_id,
        )
        order_events.append(
            OrderEventRecord(
                sequence=len(order_events),
                client_order_id=matched.client_order_id,
                instrument_id=matched.instrument_id,
                event_time=event.available_time,
                status="replaced",
                details={"reason_code": action.reason_code},
            )
        )
        return updated_pending

    @staticmethod
    def _validate_direct_order_session(
        plan: ExecutionPlan, strategy: RuntimeStrategy
    ) -> None:
        if ActionKind.SUBMIT_ORDER not in strategy.manifest.action_requirements.allowed:
            return
        unsupported = [
            stream.stream_id
            for stream in plan.dataset_streams
            if stream.timeframe.timezone != "UTC" or stream.timeframe.calendar != "24/7"
        ]
        if unsupported:
            raise ContractViolation(
                ContractError(
                    run_id=plan.run_id,
                    stage=ErrorStage.CAPABILITY_NEGOTIATION,
                    code=ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED,
                    message="Reference day orders require the UTC 24/7 session model",
                    strategy_id=plan.strategy_id,
                    engine_id=plan.engine_id,
                    details={
                        "unsupported_streams": unsupported,
                        "supported_calendar": "24/7",
                        "supported_timezone": "UTC",
                        "fallback_used": False,
                    },
                )
            )

    @staticmethod
    def _day_order_expired(order: _PendingOrder, current_time: datetime) -> bool:
        return bool(
            order.time_in_force == "day"
            and current_time.astimezone(UTC).date()
            > order.submitted_at.astimezone(UTC).date()
        )

    @staticmethod
    def _validate_projected_position(
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        pending: list[_PendingOrder],
        quantities: dict[str, Decimal],
        instrument_id: str,
    ) -> None:
        maximum = strategy.manifest.action_requirements.max_abs_position
        if maximum is None:
            return
        current = quantities.get(instrument_id, Decimal("0"))
        minimum_projected = current
        maximum_projected = current
        for order in pending:
            if order.instrument_id != instrument_id:
                continue
            if order.order_type == "target":
                assert order.target_quantity is not None
                minimum_projected = min(minimum_projected, order.target_quantity)
                maximum_projected = max(maximum_projected, order.target_quantity)
            else:
                assert order.side is not None and order.quantity is not None
                delta = order.quantity if order.side == "buy" else -order.quantity
                minimum_projected = min(minimum_projected, minimum_projected + delta)
                maximum_projected = max(maximum_projected, maximum_projected + delta)
            if minimum_projected < -maximum or maximum_projected > maximum:
                breached = (
                    maximum_projected if maximum_projected > maximum else minimum_projected
                )
                ReferenceEngine._fail_action(
                    plan,
                    "Accepted and pending orders could exceed the manifest position limit",
                    {
                        "client_order_id": order.client_order_id,
                        "instrument_id": instrument_id,
                        "projected": str(breached),
                        "projected_minimum": str(minimum_projected),
                        "projected_maximum": str(maximum_projected),
                        "maximum": str(maximum),
                        "pending_order_ids": [
                            item.client_order_id
                            for item in pending
                            if item.instrument_id == instrument_id
                        ],
                    },
                    code=ErrorCode.ORDER_REJECTED,
                )

    @staticmethod
    def _validate_target(
        plan: ExecutionPlan, strategy: RuntimeStrategy, action: TargetPosition
    ) -> None:
        maximum = strategy.manifest.action_requirements.max_abs_position
        if maximum is not None and abs(action.quantity) > maximum:
            ReferenceEngine._fail_action(
                plan,
                "TargetPosition exceeds the manifest position limit",
                {"requested": str(action.quantity), "maximum": str(maximum)},
            )

    @staticmethod
    def _update_position(
        *,
        instrument_id: str,
        delta: Decimal,
        price: Decimal,
        quantities: dict[str, Decimal],
        average_prices: dict[str, Decimal],
        realized_pnl: dict[str, Decimal],
    ) -> None:
        old_quantity = quantities.get(instrument_id, Decimal("0"))
        old_average = average_prices.get(instrument_id, Decimal("0"))
        new_quantity = old_quantity + delta
        realized = realized_pnl.get(instrument_id, Decimal("0"))
        if old_quantity == 0 or old_quantity * delta > 0:
            gross = abs(old_quantity) * old_average + abs(delta) * price
            average_prices[instrument_id] = (
                gross / abs(new_quantity) if new_quantity else Decimal("0")
            )
        else:
            closing = min(abs(old_quantity), abs(delta))
            direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
            realized_pnl[instrument_id] = realized + closing * (price - old_average) * direction
            if new_quantity == 0:
                average_prices[instrument_id] = Decimal("0")
            elif old_quantity * new_quantity < 0:
                average_prices[instrument_id] = price
        quantities[instrument_id] = new_quantity

    @staticmethod
    def _snapshot(
        *,
        timestamp: datetime,
        cash: Decimal,
        quantities: dict[str, Decimal],
        average_prices: dict[str, Decimal],
        realized_pnl: dict[str, Decimal],
        marks: dict[str, Decimal],
        pending: list[_PendingOrder],
    ) -> AccountSnapshot:
        positions: list[Position] = []
        market_value = Decimal("0")
        for instrument_id in sorted(quantities):
            quantity = quantities[instrument_id]
            mark = marks.get(instrument_id, average_prices.get(instrument_id, Decimal("0")))
            average = average_prices.get(instrument_id, Decimal("0"))
            market_value += quantity * mark
            positions.append(
                Position(
                    instrument_id=instrument_id,
                    quantity=quantity,
                    average_price=average,
                    realized_pnl=realized_pnl.get(instrument_id, Decimal("0")),
                    unrealized_pnl=quantity * (mark - average),
                )
            )
        return AccountSnapshot(
            timestamp=timestamp,
            cash=cash,
            equity=cash + market_value,
            positions=tuple(positions),
            open_orders=tuple(
                OpenOrder(
                    client_order_id=order.client_order_id,
                    instrument_id=order.instrument_id,
                    order_type=order.order_type,
                    side=order.side,
                    quantity=order.quantity,
                    target_quantity=order.target_quantity,
                    limit_price=order.limit_price,
                    status=order.status,
                )
                for order in pending
            ),
        )

    @staticmethod
    def _fail_action(
        plan: ExecutionPlan,
        message: str,
        details: dict[str, object],
        *,
        code: ErrorCode = ErrorCode.ACTION_INVALID,
    ) -> NoReturn:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                stage=ErrorStage.ACTION_VALIDATION,
                code=code,
                message=message,
                engine_id=plan.engine_id,
                strategy_id=plan.strategy_id,
                details=details,
            )
        )
