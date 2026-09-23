from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, NoReturn

import backtrader as bt  # type: ignore[import-untyped]
import pandas as pd

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
from psrc.domain.account import AccountSnapshot, Fill, Position
from psrc.domain.actions import NoOp, Prediction, TargetPosition
from psrc.domain.market import BarPayload, MarketEvent
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
        engine_id="backtrader",
        engine_version=str(bt.__version__),
        adapter_version="0.2.1",
        support_level=SupportLevel.CONFORMANCE_VERIFIED,
        profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        data_kinds=frozenset({DataKind.BAR}),
        action_kinds=frozenset(
            {ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_POSITION}
        ),
        execution=ExecutionSemantics(
            partial_fills=False,
            cancel=True,
            replace="cancel_new",
            same_timestamp_ordering="broker_notifications_then_strategy_next",
            queue_model=None,
            fill_model="backtrader.default-broker-market-next-bar.v1",
            fee_model="backtrader.percentage-commission.v1",
            slippage_model="backtrader.none.v1",
            latency_model="next-bar.v1",
        ),
        sandbox_modes=frozenset(sandbox_modes),
        extensions={
            "org.backtrader": {
                "cheat_on_open": False,
                "cheat_on_close": False,
                "volume_filler": None,
            }
        },
    )


class BacktraderAdapter(BacktestAdapter):
    """Bar-profile bridge that runs a canonical strategy inside Backtrader."""

    def __init__(
        self,
        *,
        initial_cash: Decimal = Decimal("100000"),
        commission: Decimal = Decimal("0.0005"),
        declared_capabilities: EngineCapabilities | None = None,
    ) -> None:
        self.initial_cash = initial_cash
        self.commission = commission
        self._capabilities = declared_capabilities or capabilities()
        if self._capabilities.engine_id != "backtrader":
            raise ValueError("BacktraderAdapter capabilities must declare engine_id='backtrader'")

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
        started = datetime.now(UTC)
        if not events or any(not isinstance(event.payload, BarPayload) for event in events):
            self._fail(
                plan,
                "Backtrader adapter requires a non-empty canonical bar stream",
                {"event_count": len(events)},
            )
        instruments = {event.instrument_id for event in events}
        if len(instruments) != 1:
            self._fail(
                plan,
                "Backtrader adapter v0.1 supports one canonical instrument per run",
                {"instruments": sorted(instruments)},
            )

        frame = pd.DataFrame(
            [
                {
                    "datetime": event.available_time.astimezone(UTC).replace(tzinfo=None),
                    "open": float(event.payload.open),
                    "high": float(event.payload.high),
                    "low": float(event.payload.low),
                    "close": float(event.payload.close),
                    "volume": float(event.payload.volume),
                    "openinterest": 0.0,
                }
                for event in events
                if isinstance(event.payload, BarPayload)
            ]
        ).set_index("datetime")

        decisions: list[DecisionRecord] = []
        order_events: list[OrderEventRecord] = []
        fills: list[Fill] = []
        snapshots: list[AccountSnapshot] = []
        order_submitted_at: dict[int, datetime] = {}
        no_ops = 0
        realized_pnl = Decimal("0")

        class Bridge(bt.Strategy):  # type: ignore[misc]
            params = (("canonical", None), ("canonical_events", None))

            def __init__(self) -> None:
                self.runtime: RuntimeStrategy = self.p.canonical
                self.canonical_events: tuple[MarketEvent, ...] = self.p.canonical_events
                self.cursor = 0

            def start(self) -> None:
                self.runtime.on_start()

            def next(self) -> None:
                nonlocal no_ops
                event = self.canonical_events[self.cursor]
                position = self.getposition(self.data)
                quantity = Decimal(str(position.size))
                average_price = Decimal(str(position.price))
                assert isinstance(event.payload, BarPayload)
                snapshot = AccountSnapshot(
                    timestamp=event.available_time,
                    cash=Decimal(str(self.broker.getcash())),
                    equity=Decimal(str(self.broker.getvalue())),
                    positions=(
                        Position(
                            instrument_id=event.instrument_id,
                            quantity=quantity,
                            average_price=average_price,
                            realized_pnl=realized_pnl,
                            unrealized_pnl=quantity
                            * (event.payload.close - average_price),
                        ),
                    ),
                )
                snapshots.append(snapshot)
                actions = self.runtime.on_event(event, snapshot)
                validate_actions(plan, self.runtime, actions, frozenset(instruments))
                decisions.append(
                    DecisionRecord(
                        sequence=self.cursor,
                        event_id=event.event_id,
                        event_time=event.available_time,
                        actions=actions,
                    )
                )
                for action in actions:
                    if isinstance(action, NoOp):
                        no_ops += 1
                    elif isinstance(action, Prediction):
                        continue
                    elif isinstance(action, TargetPosition):
                        projected = Decimal(str(position.size))
                        for pending_order in self.broker.get_orders_open(safe=True):
                            projected += Decimal(str(pending_order.created.size))
                        delta = abs(action.quantity - projected)
                        maximum_order = self.runtime.manifest.action_requirements.max_order_quantity
                        if maximum_order is not None and delta > maximum_order:
                            raise ContractViolation(
                                ContractError(
                                    run_id=plan.run_id,
                                    strategy_id=plan.strategy_id,
                                    engine_id=plan.engine_id,
                                    stage=ErrorStage.ACTION_VALIDATION,
                                    code=ErrorCode.ORDER_REJECTED,
                                    message=(
                                        "TargetPosition derives an order above the manifest limit"
                                    ),
                                    details={
                                        "instrument_id": action.instrument_id,
                                        "projected_position": str(projected),
                                        "target_position": str(action.quantity),
                                        "requested": str(delta),
                                        "maximum": str(maximum_order),
                                    },
                                )
                            )
                        order = self.order_target_size(target=float(action.quantity))
                        if order is not None:
                            order_submitted_at[order.ref] = event.available_time
                            order_events.append(
                                OrderEventRecord(
                                    sequence=len(order_events),
                                    client_order_id=f"bt:{order.ref}",
                                    instrument_id=event.instrument_id,
                                    event_time=event.available_time,
                                    status="accepted",
                                    details={"native_ref": str(order.ref)},
                                )
                            )
                    else:
                        BacktraderAdapter._fail(
                            plan,
                            "Canonical action is outside the Backtrader adapter profile",
                            {"action": action.model_dump(mode="json")},
                        )
                self.cursor += 1

            def notify_order(self, order: Any) -> None:
                nonlocal realized_pnl
                if order.status in {order.Margin, order.Rejected}:
                    raise ContractViolation(
                        ContractError(
                            run_id=plan.run_id,
                            strategy_id=plan.strategy_id,
                            engine_id=plan.engine_id,
                            stage=ErrorStage.ACTION_VALIDATION,
                            code=ErrorCode.ORDER_REJECTED,
                            message="Backtrader broker rejected the order",
                            details={
                                "native_status": order.getstatusname(),
                                "native_ref": str(order.ref),
                            },
                        )
                    )
                if order.status != order.Completed:
                    return
                # Backtrader's executed.pnl is the gross price P&L of the closed
                # portion (including a reversal), excluding executed.comm. This
                # matches the Position P&L semantics of the reference engine.
                realized_pnl += Decimal(str(order.executed.pnl))
                maximum_order = self.runtime.manifest.action_requirements.max_order_quantity
                executed_quantity = Decimal(str(abs(order.executed.size)))
                if maximum_order is not None and executed_quantity > maximum_order:
                    raise ContractViolation(
                        ContractError(
                            run_id=plan.run_id,
                            strategy_id=plan.strategy_id,
                            engine_id=plan.engine_id,
                            stage=ErrorStage.ACTION_VALIDATION,
                            code=ErrorCode.ORDER_REJECTED,
                            message="Native fill exceeds the manifest order limit",
                            details={
                                "native_ref": str(order.ref),
                                "requested": str(executed_quantity),
                                "maximum": str(maximum_order),
                            },
                        )
                    )
                resulting = Decimal(str(self.getposition(self.data).size))
                maximum_position = self.runtime.manifest.action_requirements.max_abs_position
                if maximum_position is not None and abs(resulting) > maximum_position:
                    raise ContractViolation(
                        ContractError(
                            run_id=plan.run_id,
                            strategy_id=plan.strategy_id,
                            engine_id=plan.engine_id,
                            stage=ErrorStage.ACTION_VALIDATION,
                            code=ErrorCode.ORDER_REJECTED,
                            message="Native fill exceeds the manifest position limit",
                            details={
                                "native_ref": str(order.ref),
                                "resulting_position": str(resulting),
                                "maximum": str(maximum_position),
                            },
                        )
                    )
                timestamp = bt.num2date(order.executed.dt, tz=UTC)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                side: Literal["buy", "sell"] = "buy" if order.executed.size > 0 else "sell"
                fill = Fill(
                    fill_id=f"bt-fill:{len(fills) + 1}",
                    client_order_id=f"bt:{order.ref}",
                    instrument_id=events[0].instrument_id,
                    timestamp=timestamp,
                    side=side,
                    quantity=Decimal(str(abs(order.executed.size))),
                    price=Decimal(str(order.executed.price)),
                    fee=Decimal(str(order.executed.comm)),
                )
                fills.append(fill)
                order_events.append(
                    OrderEventRecord(
                        sequence=len(order_events),
                        client_order_id=fill.client_order_id,
                        instrument_id=fill.instrument_id,
                        event_time=timestamp,
                        status="filled",
                        details={"fill_id": fill.fill_id},
                    )
                )

            def stop(self) -> None:
                self.runtime.on_finish()

        cerebro = bt.Cerebro(stdstats=False, cheat_on_open=False)
        cerebro.broker.setcash(float(self.initial_cash))
        cerebro.broker.setcommission(commission=float(self.commission))
        cerebro.adddata(bt.feeds.PandasData(dataname=frame))
        cerebro.addstrategy(Bridge, canonical=strategy, canonical_events=events)
        cerebro.run(runonce=True, preload=True)

        final_cash = Decimal(str(cerebro.broker.getcash()))
        final_equity = Decimal(str(cerebro.broker.getvalue()))
        return RunReport(
            run_id=plan.run_id,
            status="succeeded",
            execution_plan=plan,
            sandbox_mode=sandbox_mode,
            started_at=started,
            finished_at=datetime.now(UTC),
            metrics=RunMetrics(
                initial_cash=self.initial_cash,
                final_cash=final_cash,
                final_equity=final_equity,
                total_return=(final_equity - self.initial_cash) / self.initial_cash,
                decisions=len(decisions),
                orders=len(order_submitted_at),
                fills=len(fills),
                no_ops=no_ops,
            ),
            decisions=tuple(decisions),
            orders=tuple(order_events),
            fills=tuple(fills),
            account_snapshots=tuple(snapshots),
            assumptions=(
                "Backtrader default broker executes market orders on the next bar.",
                "cheat_on_open=false; cheat_on_close=false; volume filler disabled.",
                "Adapter v0.1 intentionally supports one bar instrument per run.",
            ),
            logs=(
                RuntimeLogRecord(
                    sequence=0,
                    timestamp=started,
                    level="info",
                    stage="backtest",
                    message="Backtrader native run started",
                ),
                RuntimeLogRecord(
                    sequence=1,
                    timestamp=datetime.now(UTC),
                    level="info",
                    stage="backtest",
                    message="Backtrader native run completed",
                    fields={"decisions": len(decisions), "fills": len(fills)},
                ),
            ),
        )

    @staticmethod
    def _fail(plan: ExecutionPlan, message: str, details: dict[str, object]) -> NoReturn:
        raise ContractViolation(
            ContractError(
                run_id=plan.run_id,
                stage=ErrorStage.BACKTEST,
                code=ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED,
                message=message,
                strategy_id=plan.strategy_id,
                engine_id=plan.engine_id,
                details=details,
            )
        )
