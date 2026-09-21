from __future__ import annotations

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compatibility import apply_compatibility_plan
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import DataKind, RunPolicy, SandboxMode, Timeframe, TimeframeMode
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import manifest_for_events, minute_bars
from psrc.runtime.orchestrator import run_rule


def _five_minute_strategy() -> SmaCrossStrategy:
    strategy = SmaCrossStrategy()
    requirement = strategy.manifest.data_requirements[0].model_copy(
        update={
            "timeframe": Timeframe(mode=TimeframeMode.BAR, interval="PT5M"),
            "lookback": 2,
        }
    )
    strategy.manifest = strategy.manifest.model_copy(update={"data_requirements": (requirement,)})
    return strategy


def test_lossy_resample_requires_both_allow_list_and_lossy_consent() -> None:
    events = minute_bars()
    dataset = manifest_for_events(
        dataset_id="synthetic.minute-bars",
        stream_id="bars",
        kind=DataKind.BAR,
        timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
        fields=frozenset({"open", "high", "low", "close", "volume"}),
        events=events,
    )
    strategy = _five_minute_strategy()
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="test.resample-denied",
            strategy=strategy.manifest,
            dataset=dataset,
            engine=capabilities(),
            policy=RunPolicy(
                required_sandbox=SandboxMode.DEVELOPMENT,
                allowed_transformations=frozenset({"bar.resample.v1"}),
                allow_lossy=False,
            ),
        )
    assert raised.value.error.code == ErrorCode.DATA_TIMEFRAME_MISMATCH


def test_explicit_resample_is_audited_and_uses_last_availability_time() -> None:
    events = minute_bars()
    dataset = manifest_for_events(
        dataset_id="synthetic.minute-bars",
        stream_id="bars",
        kind=DataKind.BAR,
        timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
        fields=frozenset({"open", "high", "low", "close", "volume"}),
        events=events,
    )
    strategy = _five_minute_strategy()
    plan = compile_run(
        run_id="test.resample-allowed",
        strategy=strategy.manifest,
        dataset=dataset,
        engine=capabilities(),
        policy=RunPolicy(
            required_sandbox=SandboxMode.DEVELOPMENT,
            allowed_transformations=frozenset({"bar.resample.v1"}),
            allow_lossy=True,
        ),
    )
    transformed = apply_compatibility_plan(events, plan)
    assert plan.compatibility[0].result == "TRANSFORMED_LOSSY"
    assert plan.compatibility[0].reversible is False
    assert len(transformed) < len(events)
    assert transformed[0].available_time == events[4].available_time

    report = run_rule(
        plan=plan,
        strategy=strategy,
        events=events,
        engine=ReferenceEngine(),
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert report.metrics.decisions == len(transformed) == 2
    compatibility_logs = [item for item in report.logs if item.stage == "compatibility"]
    assert compatibility_logs[0].fields["source_event_count"] == 10
    assert compatibility_logs[0].fields["effective_event_count"] == 2


def test_explicit_symbol_mapping_composes_with_resampling() -> None:
    events = minute_bars()
    dataset = manifest_for_events(
        dataset_id="synthetic.minute-bars",
        stream_id="bars",
        kind=DataKind.BAR,
        timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
        fields=frozenset({"open", "high", "low", "close", "volume"}),
        events=events,
    )
    strategy = _five_minute_strategy()
    requirement = strategy.manifest.data_requirements[0].model_copy(
        update={"symbols": ("SYNTH.MAPPED",)}
    )
    strategy.manifest = strategy.manifest.model_copy(update={"data_requirements": (requirement,)})
    plan = compile_run(
        run_id="test.symbol-map-and-resample",
        strategy=strategy.manifest,
        dataset=dataset,
        engine=capabilities(),
        policy=RunPolicy(
            required_sandbox=SandboxMode.DEVELOPMENT,
            allowed_transformations=frozenset({"symbol.map.v1", "bar.resample.v1"}),
            transformation_parameters={
                "symbol.map.v1": {"mapping": {"SYNTH.TEST": "SYNTH.MAPPED"}}
            },
            allow_lossy=True,
        ),
    )
    transformed = apply_compatibility_plan(events, plan)
    assert tuple(item.transformation_id for item in plan.compatibility) == (
        "symbol.map.v1",
        "bar.resample.v1",
    )
    assert plan.compatibility[0].result == "TRANSFORMED_LOSSLESS"
    assert plan.compatibility[0].reversible is True
    assert len(transformed) == 2
    assert {event.instrument_id for event in transformed} == {"SYNTH.MAPPED"}
