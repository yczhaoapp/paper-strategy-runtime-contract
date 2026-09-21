from __future__ import annotations

from decimal import Decimal

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import RunPolicy, SandboxMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, TargetPosition
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
