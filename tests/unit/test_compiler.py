from __future__ import annotations

import pytest

from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import (
    DatasetManifest,
    DatasetStream,
    EngineCapabilities,
    RunPolicy,
    RuntimeCapabilities,
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


def test_malformed_contract_version_is_structured(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    malformed = strategy.model_copy(update={"contract_version": "invalid"})
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.invalid-version",
            strategy=malformed,
            dataset=dataset,
            engine=engine,
            policy=policy,
        )
    assert raised.value.error.code == ErrorCode.CONTRACT_VERSION_UNSUPPORTED
    assert raised.value.error.details["declared"] == {"strategy": "invalid"}


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


def test_insufficient_declared_lookback_fails_before_execution(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    requirement = strategy.data_requirements[0].model_copy(update={"lookback": 101})
    oversized = strategy.model_copy(update={"data_requirements": (requirement,)})
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.insufficient-lookback",
            strategy=oversized,
            dataset=dataset,
            engine=engine,
            policy=policy,
        )
    assert raised.value.error.code == ErrorCode.DATA_RECORD_COUNT_MISMATCH
    assert raised.value.error.details["required_lookback"] == 101
    assert raised.value.error.details["declared_records"] == 100


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


def test_runtime_training_profile_is_not_required_from_engine(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    trainable = strategy.model_copy(
        update={
            "required_profiles": strategy.required_profiles
            | frozenset({"training.supervised.v1"})
        }
    )
    plan = compile_run(
        run_id="run.runtime-profile",
        strategy=trainable,
        dataset=dataset,
        engine=engine,
        policy=policy,
    )
    assert plan.runtime_required_profiles == frozenset({"training.supervised.v1"})
    assert plan.engine_required_profiles == strategy.required_profiles


def test_missing_runtime_training_profile_is_structured(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    trainable = strategy.model_copy(
        update={"required_profiles": frozenset({"training.supervised.v2"})}
    )
    runtime = RuntimeCapabilities(
        runtime_id="test.runtime",
        runtime_version="1.0.0",
        profiles=frozenset({"training.supervised.v1"}),
    )
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.missing-runtime-profile",
            strategy=trainable,
            dataset=dataset,
            engine=engine,
            runtime=runtime,
            policy=policy,
        )
    assert raised.value.error.code == ErrorCode.RUNTIME_CAPABILITY_UNSUPPORTED
    assert raised.value.error.details["missing_profiles"] == ["training.supervised.v2"]


def test_runtime_cannot_claim_an_engine_owned_profile(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    engine_without_execution = engine.model_copy(
        update={"profiles": engine.profiles - {"execution.basic.v1"}}
    )
    overclaiming_runtime = RuntimeCapabilities(
        runtime_id="test.runtime",
        runtime_version="1.0.0",
        profiles=frozenset({"training.supervised.v1", "execution.basic.v1"}),
    )
    with pytest.raises(ContractViolation) as raised:
        compile_run(
            run_id="run.runtime-overclaim",
            strategy=strategy,
            dataset=dataset,
            engine=engine_without_execution,
            runtime=overclaiming_runtime,
            policy=policy,
        )
    assert raised.value.error.code == ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED
    assert raised.value.error.details["missing_profiles"] == ["execution.basic.v1"]


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
