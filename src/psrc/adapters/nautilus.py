from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, NoReturn

import nautilus_trader
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments.equity import Equity
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

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
from psrc.domain.account import AccountSnapshot, Fill
from psrc.domain.actions import NoOp, Prediction, TargetPosition
from psrc.domain.market import BarPayload, MarketEvent
from psrc.domain.position_ledger import PositionLedger
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
        engine_id="nautilus-trader",
        engine_version=str(nautilus_trader.__version__),
        adapter_version="0.2.0",
        support_level=SupportLevel.ADAPTER_AVAILABLE,
        profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        data_kinds=frozenset({DataKind.BAR}),
        action_kinds=frozenset(
            {ActionKind.NO_OP, ActionKind.PREDICTION, ActionKind.TARGET_POSITION}
        ),
        execution=ExecutionSemantics(
            partial_fills=True,
            cancel=True,
            replace="native",
            same_timestamp_ordering="native_event_loop_then_strategy_commands",
            queue_model=None,
            fill_model="nautilus.bar-execution.fixed-ohlc.v1",
            fee_model="nautilus.maker-taker.v1",
            slippage_model="nautilus.none.v1",
            latency_model="psrc.adapter.one-event-command-delay+nautilus.message-queue.v1",
        ),
        sandbox_modes=frozenset(sandbox_modes),
        extensions={
            "org.nautilustrader": {
                "bar_execution": True,
                "bar_adaptive_high_low_ordering": False,
                "use_message_queue": True,
                "adapter_command_delay_events": 1,
                "canonical_symbol_mapping": "single-symbol -> SYNTH-TEST.SIM",
            }
        },
    )


class _BridgeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType


class NautilusAdapter(BacktestAdapter):
    """Run the canonical bar profile through NautilusTrader's native engine."""

    _venue = Venue("SIM")
    _currency = Currency.from_str("USD")
    _instrument_id = InstrumentId.from_str("SYNTH-TEST.SIM")
    _bar_type = BarType.from_str("SYNTH-TEST.SIM-1-MINUTE-LAST-EXTERNAL")

    def __init__(
        self,
        *,
        initial_cash: Decimal = Decimal("100000"),
        declared_capabilities: EngineCapabilities | None = None,
    ) -> None:
        self.initial_cash = initial_cash
        self._capabilities = declared_capabilities or capabilities()
        if self._capabilities.engine_id != "nautilus-trader":
            raise ValueError(
                "NautilusAdapter capabilities must declare engine_id='nautilus-trader'"
            )

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
                "Nautilus adapter requires a non-empty canonical bar stream",
                {"event_count": len(events)},
            )
        canonical_instruments = {event.instrument_id for event in events}
        if len(canonical_instruments) != 1:
            self._fail(
                plan,
                "Nautilus adapter v0.1 supports one canonical instrument per run",
                {"instruments": sorted(canonical_instruments)},
            )

        native_bars = [self._to_native_bar(event) for event in events]
        decisions: list[DecisionRecord] = []
        order_events: list[OrderEventRecord] = []
        fills: list[Fill] = []
        snapshots: list[AccountSnapshot] = []
        no_ops = 0
        canonical_instrument = events[0].instrument_id
        ledger = PositionLedger()
        adapter = self

        class Bridge(Strategy):
            def __init__(self, config: _BridgeConfig) -> None:
                super().__init__(config)
                self.cursor = 0
                self.pending_target: Decimal | None = None

            def on_start(self) -> None:
                strategy.on_start()
                self.subscribe_bars(self.config.bar_type)

            def on_bar(self, _bar: Bar) -> None:
                nonlocal no_ops
                event = events[self.cursor]
                assert isinstance(event.payload, BarPayload)
                native_quantity = Decimal(str(self.portfolio.net_position(adapter._instrument_id)))
                state = ledger.state(canonical_instrument)
                if abs(state.quantity - native_quantity) > Decimal("0.000001"):
                    NautilusAdapter._fail_account(
                        plan,
                        "Nautilus position differs from observed fills",
                        {
                            "native_quantity": str(native_quantity),
                            "ledger_quantity": str(state.quantity),
                        },
                    )
                if self.pending_target is not None:
                    delta = self.pending_target - native_quantity
                    self.pending_target = None
                    if delta != 0:
                        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
                        native_order = self.order_factory.market(
                            instrument_id=adapter._instrument_id,
                            order_side=side,
                            quantity=adapter._instrument().make_qty(abs(delta)),
                        )
                        self.submit_order(native_order)
                        order_events.append(
                            OrderEventRecord(
                                sequence=len(order_events),
                                client_order_id=str(native_order.client_order_id),
                                instrument_id=canonical_instrument,
                                event_time=event.available_time,
                                status="accepted",
                                details={"native_status": "submitted"},
                            )
                        )
                account = self.portfolio.account(adapter._venue)
                cash = account.balance_total(adapter._currency).as_decimal()
                pnl = self.portfolio.unrealized_pnl(adapter._instrument_id)
                equity = cash + (pnl.as_decimal() if pnl is not None else Decimal(0))
                snapshot = AccountSnapshot(
                    timestamp=event.available_time,
                    cash=cash,
                    equity=equity,
                    positions=(state.position(canonical_instrument, event.payload.close),),
                )
                snapshots.append(snapshot)
                actions = strategy.on_event(event, snapshot)
                validate_actions(plan, strategy, actions, frozenset({canonical_instrument}))
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
                        self.pending_target = action.quantity
                    else:
                        NautilusAdapter._fail(
                            plan,
                            "Canonical action is outside the Nautilus adapter profile",
                            {"action": action.model_dump(mode="json")},
                        )
                self.cursor += 1

            def on_order_filled(self, event: OrderFilled) -> None:
                timestamp = datetime.fromtimestamp(event.ts_event / 1_000_000_000, tz=UTC)
                side: Literal["buy", "sell"] = "buy" if event.is_buy else "sell"
                fill = Fill(
                    fill_id=str(event.trade_id),
                    client_order_id=str(event.client_order_id),
                    instrument_id=canonical_instrument,
                    timestamp=timestamp,
                    side=side,
                    quantity=event.last_qty.as_decimal(),
                    price=event.last_px.as_decimal(),
                    fee=event.commission.as_decimal(),
                )
                ledger.apply_fill(
                    canonical_instrument,
                    fill.quantity if side == "buy" else -fill.quantity,
                    fill.price,
                )
                fills.append(fill)
                order_events.append(
                    OrderEventRecord(
                        sequence=len(order_events),
                        client_order_id=fill.client_order_id,
                        instrument_id=canonical_instrument,
                        event_time=timestamp,
                        status="filled",
                        details={"fill_id": fill.fill_id},
                    )
                )

            def on_stop(self) -> None:
                strategy.on_finish()

        engine = BacktestEngine(
            BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR"), run_analysis=True)
        )
        try:
            engine.add_venue(
                venue=self._venue,
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                starting_balances=[Money(self.initial_cash, self._currency)],
                base_currency=self._currency,
                default_leverage=Decimal(1),
                use_message_queue=True,
                bar_execution=True,
                bar_adaptive_high_low_ordering=False,
            )
            engine.add_instrument(self._instrument())
            engine.add_data(native_bars)
            engine.add_strategy(
                Bridge(
                    _BridgeConfig(
                        instrument_id=self._instrument_id,
                        bar_type=self._bar_type,
                    )
                )
            )
            engine.run()
            native_result = engine.get_result()
            final_account = engine.portfolio.account(self._venue)
            final_cash = final_account.balance_total(self._currency).as_decimal()
            final_pnl = engine.portfolio.unrealized_pnl(self._instrument_id)
            final_equity = final_cash + (
                final_pnl.as_decimal() if final_pnl is not None else Decimal(0)
            )
            total_orders = int(native_result.summary["orders.total"])
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation(
                ContractError(
                    run_id=plan.run_id,
                    stage=ErrorStage.BACKTEST,
                    code=ErrorCode.BACKTEST_FAILED,
                    message="NautilusTrader native engine failed",
                    strategy_id=plan.strategy_id,
                    engine_id=plan.engine_id,
                    cause_chain=(f"{type(exc).__name__}: {exc}",),
                )
            ) from exc
        finally:
            engine.dispose()

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
                orders=total_orders,
                fills=len(fills),
                no_ops=no_ops,
            ),
            decisions=tuple(decisions),
            orders=tuple(order_events),
            fills=tuple(fills),
            account_snapshots=tuple(snapshots),
            assumptions=(
                "Canonical single symbol is explicitly mapped to SYNTH-TEST.SIM.",
                "The adapter delays canonical commands by one event before native submission.",
                "Nautilus use_message_queue=true is retained for deterministic sequencing.",
                "Fixed Open-High-Low-Close bar execution ordering is enabled.",
            ),
            logs=(
                RuntimeLogRecord(
                    sequence=0,
                    timestamp=started,
                    level="info",
                    stage="backtest",
                    message="NautilusTrader native run started",
                ),
                RuntimeLogRecord(
                    sequence=1,
                    timestamp=datetime.now(UTC),
                    level="info",
                    stage="backtest",
                    message="NautilusTrader native run completed",
                    fields={"decisions": len(decisions), "fills": len(fills)},
                ),
            ),
        )

    @classmethod
    def _instrument(cls) -> Equity:
        return Equity(
            instrument_id=cls._instrument_id,
            raw_symbol=Symbol("SYNTH-TEST"),
            currency=cls._currency,
            price_precision=4,
            price_increment=Price.from_str("0.0001"),
            lot_size=Quantity.from_int(1),
            ts_event=0,
            ts_init=0,
            margin_init=Decimal(0),
            margin_maint=Decimal(0),
            maker_fee=Decimal(0),
            taker_fee=Decimal(0),
        )

    @classmethod
    def _to_native_bar(cls, event: MarketEvent) -> Bar:
        payload = event.payload
        if not isinstance(payload, BarPayload):
            raise TypeError("bar payload required")
        timestamp_ns = int(event.available_time.timestamp() * 1_000_000_000)
        return Bar(
            cls._bar_type,
            Price(float(payload.open), 4),
            Price(float(payload.high), 4),
            Price(float(payload.low), 4),
            Price(float(payload.close), 4),
            Quantity(int(payload.volume), 0),
            timestamp_ns,
            timestamp_ns,
        )

    @staticmethod
    def _fail(plan: ExecutionPlan, message: str, details: dict[str, Any]) -> NoReturn:
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
