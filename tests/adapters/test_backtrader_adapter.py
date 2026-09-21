from __future__ import annotations

from psrc.adapters.backtrader import BacktraderAdapter, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import RunPolicy, SandboxMode
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
