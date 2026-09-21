from __future__ import annotations

from collections.abc import Callable

from psrc.adapters.backtrader import BacktraderAdapter
from psrc.adapters.backtrader import capabilities as backtrader_capabilities
from psrc.adapters.reference import ReferenceEngine
from psrc.adapters.reference import capabilities as reference_capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import EngineCapabilities, RunPolicy, SandboxMode
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.runtime.report import RunReport


def _run(
    engine: ReferenceEngine | BacktraderAdapter,
    capability_factory: Callable[[], EngineCapabilities],
) -> RunReport:
    events = minute_bars()
    strategy = SmaCrossStrategy()
    plan = compile_run(
        run_id=f"diff.{capability_factory().engine_id}",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capability_factory(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    return engine.run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )


def test_decisions_and_fill_direction_are_stable_across_two_native_engines() -> None:
    reports = (
        _run(ReferenceEngine(), reference_capabilities),
        _run(BacktraderAdapter(), backtrader_capabilities),
    )
    canonical_decisions = tuple(record.actions for record in reports[0].decisions)
    assert all(
        tuple(record.actions for record in report.decisions) == canonical_decisions
        for report in reports
    )

    canonical_fill_shape = tuple(
        (fill.timestamp, fill.side, fill.quantity) for fill in reports[0].fills
    )
    assert all(
        tuple((fill.timestamp, fill.side, fill.quantity) for fill in report.fills)
        == canonical_fill_shape
        for report in reports
    )
    # Execution prices intentionally differ with each declared native fill model.
    assert len({tuple(fill.price for fill in report.fills) for report in reports}) == 2
    final_equities = [report.metrics.final_equity for report in reports]
    assert max(final_equities) - min(final_equities) < 2
    assert all(report.assumptions for report in reports)
