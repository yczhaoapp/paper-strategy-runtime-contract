from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from psrc.adapters.backtrader import BacktraderAdapter
from psrc.adapters.backtrader import capabilities as backtrader_capabilities
from psrc.adapters.base import BacktestAdapter
from psrc.adapters.reference import ReferenceEngine
from psrc.adapters.reference import capabilities as reference_capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import EngineCapabilities, RunPolicy, SandboxMode
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.runtime.orchestrator import run_rule
from psrc.runtime.report import RunReport, write_input_evidence, write_run_bundle
from psrc.sandbox.container import DockerSandbox


def _hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def generate_adapter_evidence(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    events = minute_bars()
    dataset = minute_bar_manifest(events)
    sandbox = (
        SandboxMode.STRICT_CONTAINER
        if DockerSandbox.current_process_attested()
        else SandboxMode.DEVELOPMENT
    )
    strict = sandbox == SandboxMode.STRICT_CONTAINER
    policy = RunPolicy(required_sandbox=sandbox)
    adapters: tuple[tuple[str, BacktestAdapter, Callable[[], EngineCapabilities]], ...] = (
        ("reference", ReferenceEngine(), lambda: reference_capabilities(strict_container=strict)),
        (
            "backtrader",
            BacktraderAdapter(),
            lambda: backtrader_capabilities(strict_container=strict),
        ),
    )
    reports: dict[str, RunReport] = {}
    for name, engine, capability_factory in adapters:
        strategy = SmaCrossStrategy()
        engine_capabilities = capability_factory()
        plan = compile_run(
            run_id=f"adapter-evidence.{name}",
            strategy=strategy.manifest,
            dataset=dataset,
            engine=engine_capabilities,
            policy=policy,
        )
        report = run_rule(
            plan=plan,
            strategy=strategy,
            events=events,
            engine=engine,
            sandbox_mode=sandbox,
        )
        reports[name] = report
        write_run_bundle(report, output / name)
        write_input_evidence(
            output / name,
            report=report,
            strategy=strategy.manifest,
            dataset=dataset,
            engine=engine_capabilities,
            policy=policy,
            events=events,
        )

    decision_hashes = {
        name: _hash([record.model_dump(mode="json") for record in report.decisions])
        for name, report in reports.items()
    }
    fill_shapes = {
        name: [
            {
                "timestamp": fill.timestamp.isoformat(),
                "side": fill.side,
                "quantity": format(fill.quantity.normalize(), "f"),
            }
            for fill in report.fills
        ]
        for name, report in reports.items()
    }
    comparison: dict[str, object] = {
        "status": "passed",
        "engines": list(reports),
        "native_engine_count": len(reports),
        "decisions_equal": len(set(decision_hashes.values())) == 1,
        "fill_shapes_equal": len({_hash(value) for value in fill_shapes.values()}) == 1,
        "execution_prices_intentionally_engine_specific": True,
        "decision_sha256": decision_hashes,
        "fill_shapes": fill_shapes,
    }
    if not comparison["decisions_equal"] or not comparison["fill_shapes_equal"]:
        raise AssertionError("Cross-engine conformance comparison failed")
    (output / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return comparison
