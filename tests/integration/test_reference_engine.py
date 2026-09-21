from __future__ import annotations

from decimal import Decimal

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import RunPolicy, SandboxMode
from psrc.domain.market import BarPayload
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.runtime.report import RunReport


def test_sma_cross_runs_end_to_end_without_same_bar_fills() -> None:
    events = minute_bars()
    dataset = minute_bar_manifest(events)
    strategy = SmaCrossStrategy()
    plan = compile_run(
        run_id="test.sma-e2e",
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

    assert report.status == "succeeded"
    assert report.metrics.decisions == len(events)
    assert report.metrics.no_ops == 4
    assert report.metrics.orders == 6
    # Repeated target-position intents are idempotent once the target is reached.
    assert report.metrics.fills == 2
    assert report.fills[0].timestamp == events[5].available_time
    payload = events[4].payload
    assert isinstance(payload, BarPayload)
    assert report.fills[0].price != payload.close
    assert report.metrics.final_equity != Decimal("100000")


def test_same_seed_and_inputs_are_economically_deterministic() -> None:
    def run_once() -> RunReport:
        events = minute_bars()
        strategy = SmaCrossStrategy()
        plan = compile_run(
            run_id="test.deterministic",
            strategy=strategy.manifest,
            dataset=minute_bar_manifest(events),
            engine=capabilities(),
            policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
        )
        return ReferenceEngine().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )

    first = run_once()
    second = run_once()
    assert first.metrics == second.metrics
    assert first.decisions == second.decisions
    assert first.fills == second.fills
    assert first.account_snapshots == second.account_snapshots
