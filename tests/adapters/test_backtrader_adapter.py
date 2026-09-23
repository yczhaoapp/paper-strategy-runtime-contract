from __future__ import annotations

from decimal import Decimal

from psrc.adapters.backtrader import BacktraderAdapter, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import RunPolicy, SandboxMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import TargetPosition
from psrc.domain.market import MarketEvent
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars


def test_backtrader_adapter_runs_canonical_sma_strategy() -> None:
    events = minute_bars()
    strategy = SmaCrossStrategy()
    plan = compile_run(
        run_id="test.backtrader-sma",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    report = BacktraderAdapter().run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert report.status == "succeeded"
    assert report.metrics.decisions == len(events)
    assert report.metrics.fills >= 1
    assert report.fills[0].timestamp > events[4].available_time
    assert [
        (
            item.positions[0].quantity,
            item.positions[0].realized_pnl,
            item.positions[0].unrealized_pnl,
        )
        for item in report.account_snapshots[5:]
    ] == [
        (Decimal("1"), Decimal("0"), Decimal("-1")),
        (Decimal("1"), Decimal("0"), Decimal("-2")),
        (Decimal("1"), Decimal("0"), Decimal("-3")),
        (Decimal("-1"), Decimal("-3"), Decimal("1")),
        (Decimal("-1"), Decimal("-3"), Decimal("2")),
    ]


def test_backtrader_account_pnl_covers_profit_loss_close_and_reversal() -> None:
    events = minute_bars()

    class ScheduledStrategy:
        manifest = SmaCrossStrategy.manifest

        def on_start(self) -> None:
            self.cursor = 0

        def on_event(
            self, event: MarketEvent, account: AccountSnapshot
        ) -> tuple[TargetPosition, ...]:
            del account
            targets = ("1", "1", "0", "-1", "1", "0", "0", "0", "0", "0")
            target = Decimal(targets[self.cursor])
            self.cursor += 1
            return (
                TargetPosition(
                    instrument_id=event.instrument_id,
                    quantity=target,
                    reason_code="test.scheduled_pnl",
                ),
            )

        def on_finish(self) -> None:
            pass

    strategy = ScheduledStrategy()
    plan = compile_run(
        run_id="test.backtrader-pnl",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    report = BacktraderAdapter(commission=Decimal("0")).run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert report.status == "succeeded"
    assert [(fill.side, fill.quantity, fill.price) for fill in report.fills] == [
        ("buy", Decimal("1"), Decimal("100")),
        ("sell", Decimal("1"), Decimal("102")),
        ("sell", Decimal("1"), Decimal("103")),
        ("buy", Decimal("2"), Decimal("104")),
        ("sell", Decimal("1"), Decimal("103")),
    ]
    observed = [
        (
            snapshot.positions[0].quantity,
            snapshot.positions[0].average_price,
            snapshot.positions[0].realized_pnl,
            snapshot.positions[0].unrealized_pnl,
        )
        for snapshot in report.account_snapshots
    ]
    assert observed[2:7] == [
        (Decimal("1"), Decimal("100"), Decimal("0"), Decimal("2")),
        (Decimal("0"), Decimal("0"), Decimal("2"), Decimal("0")),
        (Decimal("-1"), Decimal("103"), Decimal("2"), Decimal("-1")),
        (Decimal("1"), Decimal("104"), Decimal("1"), Decimal("-1")),
        (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
    ]
