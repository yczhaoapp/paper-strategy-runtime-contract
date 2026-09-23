from __future__ import annotations

import importlib.util

import pytest

from psrc.adapters.registry import resolve_adapter
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import RunPolicy, SandboxMode
from psrc.domain.market import BarPayload
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars


def test_nautilus_adapter_runs_canonical_sma_strategy() -> None:
    if importlib.util.find_spec("nautilus_trader") is None:
        with pytest.raises(ContractViolation) as caught:
            resolve_adapter("nautilus-trader", strict_container=False)
        assert caught.value.error.code == ErrorCode.ENGINE_DEPENDENCY_MISSING
        return
    from psrc.adapters.nautilus import NautilusAdapter, capabilities

    events = minute_bars()
    strategy = SmaCrossStrategy()
    plan = compile_run(
        run_id="test.nautilus-sma",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    report = NautilusAdapter().run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert report.status == "succeeded"
    assert report.metrics.decisions == len(events)
    assert report.metrics.fills >= 1
    assert report.fills[0].timestamp > events[4].available_time
    assert report.execution_plan.engine_id == "nautilus-trader"
    for event, snapshot in zip(events, report.account_snapshots, strict=True):
        assert isinstance(event.payload, BarPayload)
        position = snapshot.positions[0]
        if position.quantity:
            assert position.average_price > 0
        assert position.unrealized_pnl == position.quantity * (
            event.payload.close - position.average_price
        )
