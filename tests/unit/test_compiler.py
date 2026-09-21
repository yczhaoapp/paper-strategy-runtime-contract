from __future__ import annotations

import pytest

from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import (
    DatasetManifest,
    DatasetStream,
    EngineCapabilities,
    RunPolicy,
    SandboxMode,
    StrategyManifest,
    SupportLevel,
    Timeframe,
    TimeframeMode,
)


def test_exact_contract_compiles(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    plan = compile_run(
        run_id="run.exact",
        strategy=strategy,
        dataset=dataset,
        engine=engine,
        policy=policy,
    )

    assert plan.strategy_id == strategy.strategy_id
    assert len(plan.compatibility) == 1
    assert plan.compatibility[0].result == "EXACT"
    assert len(plan.strategy_manifest_sha256) == 64


def test_missing_field_is_structured(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    stream = dataset.streams[0].model_copy(update={"fields": frozenset({"open", "close"})})
    incomplete = dataset.model_copy(update={"streams": (stream,)})

    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.missing-field",
            strategy=strategy,
            dataset=incomplete,
            engine=engine,
            policy=policy,
        )

    assert raised.value.error.code == ErrorCode.DATA_FIELD_MISSING
    assert raised.value.error.details["missing_fields"] == ["high", "low", "volume"]


def test_timeframe_mismatch_is_not_silently_resampled(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    daily = Timeframe(mode=TimeframeMode.BAR, interval="P1D")
    stream: DatasetStream = dataset.streams[0].model_copy(update={"timeframe": daily})
    wrong = DatasetManifest.model_validate(
        dataset.model_copy(update={"streams": (stream,)}).model_dump(mode="python")
    )

    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.wrong-timeframe",
            strategy=strategy,
            dataset=wrong,
            engine=engine,
            policy=policy,
        )

    assert raised.value.error.code == ErrorCode.DATA_TIMEFRAME_MISMATCH


def test_missing_engine_profile_is_structured(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    reduced = engine.model_copy(update={"profiles": frozenset({"core.bar.v1"})})

    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.missing-profile",
            strategy=strategy,
            dataset=dataset,
            engine=reduced,
            policy=policy,
        )

    assert raised.value.error.code == ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED


def test_profile_only_engine_cannot_silently_compile_as_runnable(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    profiled = engine.model_copy(update={"support_level": SupportLevel.PROFILED})
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.profile-only",
            strategy=strategy,
            dataset=dataset,
            engine=profiled,
            policy=policy,
        )
    assert raised.value.error.code == ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED
    assert raised.value.error.details["adapter_execution_available"] is False


def test_run_policy_cannot_weaken_strategy_sandbox_minimum(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
) -> None:
    development_policy = RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT)
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.sandbox-downgrade",
            strategy=strategy,
            dataset=dataset,
            engine=engine,
            policy=development_policy,
        )
    assert raised.value.error.code == ErrorCode.SANDBOX_POLICY_DOWNGRADE
    assert raised.value.error.details["fallback_used"] is False
