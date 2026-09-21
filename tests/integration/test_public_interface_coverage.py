from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import (
    ActionKind,
    DataKind,
    RunPolicy,
    SandboxMode,
    StrategyKind,
    Timeframe,
    TimeframeMode,
    TrainingMode,
)
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import NoOp, ReplaceOrder, SubmitOrder
from psrc.domain.market import MarketEvent, TradePayload
from psrc.examples.synthetic import manifest_for_events
from psrc.strategies.catalog import all_examples
from psrc.strategies.common import event_requirement, make_manifest


def _trade_events() -> tuple[MarketEvent, ...]:
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    return tuple(
        MarketEvent(
            event_id=f"trade:{index}",
            instrument_id="PUBLIC.TEST",
            event_time=start + timedelta(seconds=index),
            available_time=start + timedelta(seconds=index),
            receive_time=start + timedelta(seconds=index),
            sequence=index - 1,
            source="public-interface-fixture.v1",
            payload=TradePayload(
                price=Decimal("100"),
                size=Decimal("5"),
                aggressor_side="buy" if index % 2 else "sell",
                trade_id=f"venue:{index}",
            ),
        )
        for index in range(1, 5)
    )


class _OrderLifecycleStrategy:
    manifest = make_manifest(
        strategy_id="rule.public_interface_order_lifecycle",
        kind=StrategyKind.RULE,
        entrypoint="tests.integration.test_public_interface_coverage:_OrderLifecycleStrategy",
        profiles=frozenset({"event.trade.v1", "execution.advanced.v1"}),
        data=(
            event_requirement(
                stream_id="trades",
                kind=DataKind.TRADE,
                symbols=("PUBLIC.TEST",),
                fields=frozenset({"price", "size", "aggressor_side", "trade_id"}),
            ),
        ),
        actions=frozenset(
            {ActionKind.NO_OP, ActionKind.SUBMIT_ORDER, ActionKind.REPLACE_ORDER}
        ),
        training=TrainingMode.NOT_REQUIRED,
    )

    def __init__(self) -> None:
        self.accounts: list[AccountSnapshot] = []

    def on_start(self) -> None:
        self.accounts.clear()

    def on_event(
        self, event: MarketEvent, account: AccountSnapshot
    ) -> tuple[NoOp | ReplaceOrder | SubmitOrder, ...]:
        self.accounts.append(account)
        if event.sequence == 0:
            return (
                SubmitOrder(
                    client_order_id="external-limit-1",
                    instrument_id=event.instrument_id,
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("1"),
                    limit_price=Decimal("1"),
                    reason_code="interface.submit",
                ),
            )
        if event.sequence == 1:
            return (
                ReplaceOrder(
                    client_order_id="external-limit-1",
                    new_limit_price=Decimal("2"),
                    reason_code="interface.replace_pending",
                ),
            )
        if event.sequence == 2:
            return (
                ReplaceOrder(
                    client_order_id="external-limit-1",
                    new_limit_price=Decimal("101"),
                    reason_code="interface.replace_marketable",
                ),
            )
        return (NoOp(reason_code="interface.done", explanation="order filled"),)

    def on_finish(self) -> None:
        pass


def test_trade_tick_account_and_order_status_are_executable() -> None:
    events = _trade_events()
    strategy = _OrderLifecycleStrategy()
    dataset = manifest_for_events(
        dataset_id="public-interface.trade-events",
        stream_id="trades",
        kind=DataKind.TRADE,
        timeframe=Timeframe(mode=TimeframeMode.EVENT),
        fields=frozenset({"price", "size", "aggressor_side", "trade_id"}),
        events=events,
    )
    plan = compile_run(
        run_id="public-interface.trade-order-lifecycle",
        strategy=strategy.manifest,
        dataset=dataset,
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )

    report = ReferenceEngine().run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )

    assert dataset.streams[0].timeframe.mode == TimeframeMode.EVENT
    assert dataset.streams[0].kind == DataKind.TRADE
    assert strategy.accounts[1].open_orders[0].status == "accepted"
    assert strategy.accounts[2].open_orders[0].status == "replaced"
    assert strategy.accounts[3].open_orders == ()
    assert strategy.accounts[3].positions[0].quantity == Decimal("1")
    assert strategy.accounts[3].cash < report.metrics.initial_cash
    assert strategy.accounts[3].equity > 0
    assert [order.status for order in report.orders] == [
        "accepted",
        "replaced",
        "replaced",
        "filled",
    ]
    assert report.metrics.fills == 1


def test_catalog_declares_all_strategy_shapes_and_required_time_granularities() -> None:
    examples = all_examples()
    kinds = {StrategyKind(item.manifest.kind) for item in examples}
    timeframes = {
        (
            TimeframeMode(requirement.timeframe.mode),
            requirement.timeframe.interval,
        )
        for item in examples
        for requirement in item.manifest.data_requirements
    }

    assert kinds == set(StrategyKind)
    assert (TimeframeMode.EVENT, None) in timeframes
    assert (TimeframeMode.BAR, "PT1M") in timeframes
    assert (TimeframeMode.BAR, "P1D") in timeframes
