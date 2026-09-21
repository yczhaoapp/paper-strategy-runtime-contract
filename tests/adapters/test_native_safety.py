from __future__ import annotations

from decimal import Decimal

import pytest

from psrc.adapters.backtrader import BacktraderAdapter, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import RunPolicy, SandboxMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, TargetPosition
from psrc.domain.market import MarketEvent
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("wrong-symbol", ErrorCode.ACTION_INVALID),
        ("oversized", ErrorCode.ACTION_INVALID),
        ("broker-margin", ErrorCode.ORDER_REJECTED),
        ("reverse-data", ErrorCode.DATA_ORDERING_INVALID),
    ],
)
def test_native_adapter_cannot_reinterpret_or_silently_reject_orders(
    scenario: str, code: ErrorCode
) -> None:
    class Invalid(SmaCrossStrategy):
        def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
            return (
                TargetPosition(
                    instrument_id="WRONG" if scenario == "wrong-symbol" else event.instrument_id,
                    quantity=Decimal(999) if scenario == "oversized" else Decimal(1),
                    reason_code="test.native-safety",
                ),
            )

    events = minute_bars()
    strategy = Invalid()
    plan = compile_run(
        run_id="test.native-safety",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    if scenario == "reverse-data":
        events = tuple(reversed(events))
    engine = BacktraderAdapter(
        initial_cash=Decimal(1) if scenario == "broker-margin" else Decimal(100000)
    )
    with pytest.raises(ContractViolation) as caught:
        engine.run(
            plan=plan, strategy=strategy, events=events, sandbox_mode=SandboxMode.DEVELOPMENT
        )
    assert caught.value.error.code == code
