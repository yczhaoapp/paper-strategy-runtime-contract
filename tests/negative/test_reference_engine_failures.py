from __future__ import annotations

from decimal import Decimal

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import ActionKind, ActionRequirements, RunPolicy, SandboxMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, NoOp, SubmitOrder, TargetPosition
from psrc.domain.market import MarketEvent
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars


class IllegalPositionStrategy(SmaCrossStrategy):
    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal("2"),
                reason_code="test.illegal",
            ),
        )


class RepeatedDirectBuyStrategy(SmaCrossStrategy):
    manifest = SmaCrossStrategy.manifest.model_copy(
        update={
            "action_requirements": ActionRequirements(
                allowed=frozenset({ActionKind.NO_OP, ActionKind.SUBMIT_ORDER}),
                max_abs_position=Decimal("1"),
                max_order_quantity=Decimal("1"),
            )
        }
    )

    def __init__(self) -> None:
        self.counter = 0

    def on_start(self) -> None:
        self.counter = 0

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        self.counter += 1
        return (
            SubmitOrder(
                client_order_id=f"buy:{self.counter}",
                instrument_id=event.instrument_id,
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                reason_code="test.repeated_buy",
            ),
        )


class ExpiringDayOrderStrategy(RepeatedDirectBuyStrategy):
    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if event.sequence:
            return (NoOp(reason_code="test.wait", explanation="wait for expiry"),)
        return (
            SubmitOrder(
                client_order_id="day:1",
                instrument_id=event.instrument_id,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                limit_price=Decimal("100"),
                time_in_force="day",
                reason_code="test.day_expiry",
            ),
        )


def test_illegal_position_is_structured_error() -> None:
    events = minute_bars()
    strategy = IllegalPositionStrategy()
    plan = compile_run(
        run_id="test.illegal-position",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )

    with pytest.raises(ContractViolation) as raised:
        ReferenceEngine().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )

    assert raised.value.error.code == ErrorCode.ACTION_INVALID
    assert raised.value.error.stage == "action_validation"


def test_out_of_order_data_fails_before_strategy_execution() -> None:
    events = tuple(reversed(minute_bars()))
    strategy = SmaCrossStrategy()
    canonical = tuple(reversed(events))
    plan = compile_run(
        run_id="test.out-of-order",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(canonical),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )

    with pytest.raises(ContractViolation) as raised:
        ReferenceEngine().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )

    assert raised.value.error.code == ErrorCode.DATA_ORDERING_INVALID


def test_direct_orders_cannot_accumulate_beyond_position_limit() -> None:
    events = minute_bars()[:3]
    strategy = RepeatedDirectBuyStrategy()
    plan = compile_run(
        run_id="test.direct-position-limit",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )

    with pytest.raises(ContractViolation) as raised:
        ReferenceEngine().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )

    assert raised.value.error.code == ErrorCode.ORDER_REJECTED
    assert raised.value.error.details["projected"] == "2"
    assert raised.value.error.details["maximum"] == "1"


def test_day_order_expires_before_a_later_utc_session_can_fill() -> None:
    source = minute_bars()
    first = source[0].model_copy(
        update={
            "event_time": source[0].event_time.replace(hour=23, minute=59),
            "available_time": source[0].available_time.replace(hour=23, minute=59),
            "receive_time": source[0].receive_time.replace(hour=23, minute=59),
            "sequence": 0,
        }
    )
    second_time = source[1].event_time.replace(day=4, hour=0, minute=1)
    second = source[1].model_copy(
        update={
            "event_time": second_time,
            "available_time": second_time,
            "receive_time": second_time,
            "sequence": 1,
        }
    )
    events = (first, second)
    strategy = ExpiringDayOrderStrategy()
    plan = compile_run(
        run_id="test.day-expiry",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )

    report = ReferenceEngine().run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )

    assert report.metrics.fills == 0
    assert [item.status for item in report.orders] == ["accepted", "expired"]
    assert report.orders[-1].details["reason"] == "day_session_end"
