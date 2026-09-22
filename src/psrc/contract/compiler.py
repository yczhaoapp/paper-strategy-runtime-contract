from __future__ import annotations

from datetime import UTC, datetime
from typing import NoReturn

from psrc.constants import CONTRACT_MAJOR
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    CompatibilityRecord,
    CompatibilityResult,
    DatasetManifest,
    DatasetStream,
    EngineCapabilities,
    ExecutionPlan,
    RunPolicy,
    RuntimeCapabilities,
    SandboxMode,
    StrategyManifest,
    SupportLevel,
    Timeframe,
    TimeframeMode,
)
from psrc.runtime.capabilities import capabilities as runtime_capabilities


def _major(version: str) -> int | None:
    try:
        return int(version.split(".", maxsplit=1)[0])
    except (AttributeError, TypeError, ValueError):
        return None


def _fail(
    *,
    run_id: str,
    strategy: StrategyManifest,
    engine: EngineCapabilities,
    code: ErrorCode,
    message: str,
    details: dict[str, object],
) -> NoReturn:
    raise ContractViolation(
        ContractError(
            run_id=run_id,
            stage=ErrorStage.CAPABILITY_NEGOTIATION,
            code=code,
            message=message,
            strategy_id=strategy.strategy_id,
            engine_id=engine.engine_id,
            details=details,
        )
    )


def _find_stream(dataset: DatasetManifest, stream_id: str) -> DatasetStream | None:
    return next((stream for stream in dataset.streams if stream.stream_id == stream_id), None)


def _duration_seconds(value: str | None) -> int | None:
    units = {"S": 1, "M": 60, "H": 3600}
    if value == "P1D":
        return 86400
    if value and value.startswith("PT") and value[-1] in units:
        try:
            return int(value[2:-1]) * units[value[-1]]
        except ValueError:
            return None
    return None


def _can_resample(stream: DatasetStream, requirement_timeframe: Timeframe) -> bool:
    source = stream.timeframe
    target = requirement_timeframe
    source_seconds = _duration_seconds(source.interval)
    target_seconds = _duration_seconds(target.interval)
    return bool(
        source.mode == TimeframeMode.BAR
        and target.mode == TimeframeMode.BAR
        and source.timezone == target.timezone
        and source.calendar == target.calendar
        and source.alignment == target.alignment
        and source_seconds
        and target_seconds
        and source_seconds < target_seconds
        and target_seconds % source_seconds == 0
    )


def _symbol_mapping(
    stream: DatasetStream,
    required_symbols: tuple[str, ...],
    policy: RunPolicy,
) -> dict[str, str] | None:
    transformation_id = "symbol.map.v1"
    if transformation_id not in policy.allowed_transformations:
        return None
    raw_mapping = policy.transformation_parameters.get(transformation_id, {}).get("mapping")
    if not isinstance(raw_mapping, dict) or not raw_mapping:
        return None
    mapping = {
        source: target
        for source, target in raw_mapping.items()
        if isinstance(source, str)
        and isinstance(target, str)
        and source in stream.symbols
        and target in required_symbols
    }
    # A reversible symbol transform must be one-to-one and cannot collide with
    # an already present target instrument.
    if len(set(mapping.values())) != len(mapping) or set(mapping.values()) & stream.symbols:
        return None
    missing = set(required_symbols) - stream.symbols
    return mapping if missing <= set(mapping.values()) else None


def compile_run(
    *,
    run_id: str,
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
    runtime: RuntimeCapabilities | None = None,
    strategy_code_evidence_sha256: str | None = None,
    training_input_evidence_sha256: str | None = None,
    external_strategy_admission_sha256: str | None = None,
) -> ExecutionPlan:
    runtime = runtime or runtime_capabilities()
    versions = {
        "strategy": strategy.contract_version,
        "dataset": dataset.contract_version,
        "engine": engine.contract_version,
        "runtime": runtime.contract_version,
    }
    incompatible = {
        name: version for name, version in versions.items() if _major(version) != CONTRACT_MAJOR
    }
    if incompatible:
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.CONTRACT_VERSION_UNSUPPORTED,
            message="A declared contract major version is unsupported",
            details={"supported_major": CONTRACT_MAJOR, "declared": incompatible},
        )

    sandbox_rank = {
        SandboxMode.DEVELOPMENT: 0,
        SandboxMode.STRICT_CONTAINER: 1,
    }
    if (
        sandbox_rank[SandboxMode(policy.required_sandbox)]
        < sandbox_rank[SandboxMode(strategy.resources.sandbox)]
    ):
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.SANDBOX_POLICY_DOWNGRADE,
            message="Run policy cannot weaken the strategy's minimum sandbox requirement",
            details={
                "strategy_minimum": strategy.resources.sandbox,
                "requested": policy.required_sandbox,
                "fallback_used": False,
            },
        )

    support_rank = {
        SupportLevel.PROFILED: 0,
        SupportLevel.ADAPTER_AVAILABLE: 1,
        SupportLevel.CONFORMANCE_VERIFIED: 2,
        SupportLevel.PRODUCTION_CERTIFIED: 3,
    }
    if (
        support_rank[SupportLevel(engine.support_level)]
        < support_rank[SupportLevel(policy.minimum_engine_support)]
    ):
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED,
            message="Engine support level is below the run-policy minimum",
            details={
                "actual_support_level": engine.support_level,
                "minimum_support_level": policy.minimum_engine_support,
                "adapter_execution_available": False,
            },
        )

    runtime_namespace_profiles = frozenset(
        profile for profile in strategy.required_profiles if profile.startswith("training.")
    )
    runtime_required_profiles = runtime_namespace_profiles
    missing_runtime_profiles = runtime_required_profiles - runtime.profiles
    if missing_runtime_profiles:
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.RUNTIME_CAPABILITY_UNSUPPORTED,
            message="Runtime does not satisfy required orchestration capability profiles",
            details={
                "runtime_id": runtime.runtime_id,
                "missing_profiles": sorted(missing_runtime_profiles),
            },
        )

    engine_required_profiles = strategy.required_profiles - runtime_required_profiles
    missing_engine_profiles = engine_required_profiles - engine.profiles
    if missing_engine_profiles:
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED,
            message="Engine does not satisfy required capability profiles",
            details={"missing_profiles": sorted(missing_engine_profiles)},
        )

    missing_actions = strategy.action_requirements.allowed - engine.action_kinds
    if missing_actions:
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED,
            message="Engine does not support all declared strategy actions",
            details={"missing_actions": sorted(missing_actions)},
        )

    records: list[CompatibilityRecord] = []
    for requirement in strategy.data_requirements:
        stream = _find_stream(dataset, requirement.stream_id)
        if stream is None:
            _fail(
                run_id=run_id,
                strategy=strategy,
                engine=engine,
                code=ErrorCode.DATA_STREAM_MISSING,
                message=f"Required stream {requirement.stream_id!r} is absent",
                details={"stream_id": requirement.stream_id},
            )

        if requirement.kind not in engine.data_kinds:
            _fail(
                run_id=run_id,
                strategy=strategy,
                engine=engine,
                code=ErrorCode.ENGINE_CAPABILITY_UNSUPPORTED,
                message="Engine cannot consume the required market-data kind",
                details={"kind": requirement.kind},
            )

        if stream.kind != requirement.kind:
            _fail(
                run_id=run_id,
                strategy=strategy,
                engine=engine,
                code=ErrorCode.DATA_STREAM_MISSING,
                message="Dataset stream kind does not match the declaration",
                details={"required": requirement.kind, "actual": stream.kind},
            )

        missing_fields = requirement.required_fields - stream.fields
        if missing_fields:
            _fail(
                run_id=run_id,
                strategy=strategy,
                engine=engine,
                code=ErrorCode.DATA_FIELD_MISSING,
                message="Dataset does not contain all required fields",
                details={"stream_id": stream.stream_id, "missing_fields": sorted(missing_fields)},
            )

        required_symbol_count = len(requirement.symbols or tuple(stream.symbols)) or 1
        minimum_source_records = requirement.lookback * required_symbol_count
        if (
            stream.timeframe == requirement.timeframe
            and stream.record_count < minimum_source_records
        ):
            _fail(
                run_id=run_id,
                strategy=strategy,
                engine=engine,
                code=ErrorCode.DATA_RECORD_COUNT_MISMATCH,
                message="Dataset cannot supply the declared per-symbol lookback",
                details={
                    "stream_id": stream.stream_id,
                    "required_lookback": requirement.lookback,
                    "required_symbol_count": required_symbol_count,
                    "minimum_records": minimum_source_records,
                    "declared_records": stream.record_count,
                    "fallback_used": False,
                },
            )

        transformed = False
        missing_symbols = set(requirement.symbols) - stream.symbols
        if missing_symbols:
            symbol_mapping = _symbol_mapping(stream, requirement.symbols, policy)
            if symbol_mapping is None:
                _fail(
                    run_id=run_id,
                    strategy=strategy,
                    engine=engine,
                    code=ErrorCode.SYMBOL_MAPPING_FAILED,
                    message="Required symbols are not present and no valid explicit mapping exists",
                    details={
                        "missing_symbols": sorted(missing_symbols),
                        "available_transformation": "symbol.map.v1",
                        "allowed_transformations": sorted(policy.allowed_transformations),
                    },
                )
            records.append(
                CompatibilityRecord(
                    result=CompatibilityResult.TRANSFORMED_LOSSLESS,
                    transformation_id="symbol.map.v1",
                    transformation_version="1.0.0",
                    rationale="Explicitly map dataset symbols to strategy contract symbols",
                    input_schema_sha256=stream.schema_sha256,
                    output_schema_sha256=stream.schema_sha256,
                    affected_records=stream.record_count,
                    parameters={
                        "stream_id": stream.stream_id,
                        "mapping": symbol_mapping,
                    },
                    reversible=True,
                )
            )
            transformed = True

        if stream.timeframe != requirement.timeframe:
            transformation_id = "bar.resample.v1"
            if (
                transformation_id in policy.allowed_transformations
                and policy.allow_lossy
                and _can_resample(stream, requirement.timeframe)
            ):
                records.append(
                    CompatibilityRecord(
                        result=CompatibilityResult.TRANSFORMED_LOSSY,
                        transformation_id=transformation_id,
                        transformation_version="1.0.0",
                        rationale=(
                            f"Explicitly resample {stream.timeframe.interval} bars to "
                            f"{requirement.timeframe.interval} bars"
                        ),
                        input_schema_sha256=stream.schema_sha256,
                        output_schema_sha256=stream.schema_sha256,
                        affected_records=stream.record_count,
                        parameters={
                            "stream_id": stream.stream_id,
                            "source_interval": stream.timeframe.interval,
                            "target_interval": requirement.timeframe.interval,
                        },
                        reversible=False,
                    )
                )
                transformed = True
            else:
                _fail(
                    run_id=run_id,
                    strategy=strategy,
                    engine=engine,
                    code=ErrorCode.DATA_TIMEFRAME_MISMATCH,
                    message="Dataset timeframe does not exactly match the strategy requirement",
                    details={
                        "required": requirement.timeframe.model_dump(mode="json"),
                        "actual": stream.timeframe.model_dump(mode="json"),
                        "available_transformation": (
                            transformation_id
                            if _can_resample(stream, requirement.timeframe)
                            else None
                        ),
                        "allowed_transformations": sorted(policy.allowed_transformations),
                        "allow_lossy": policy.allow_lossy,
                    },
                )
        if not transformed:
            records.append(
                CompatibilityRecord(
                    result=CompatibilityResult.EXACT,
                    rationale=f"Stream {stream.stream_id} exactly satisfies its data requirement",
                    input_schema_sha256=stream.schema_sha256,
                    output_schema_sha256=stream.schema_sha256,
                    affected_records=stream.record_count,
                    reversible=True,
                )
            )

    if policy.required_sandbox not in engine.sandbox_modes:
        _fail(
            run_id=run_id,
            strategy=strategy,
            engine=engine,
            code=ErrorCode.SANDBOX_UNAVAILABLE,
            message="Selected engine adapter cannot honor the required sandbox mode",
            details={
                "required": policy.required_sandbox,
                "available": sorted(engine.sandbox_modes),
            },
        )

    return ExecutionPlan(
        run_id=run_id,
        strategy_id=strategy.strategy_id,
        dataset_id=dataset.dataset_id,
        engine_id=engine.engine_id,
        runtime_id=runtime.runtime_id,
        runtime_required_profiles=runtime_required_profiles,
        engine_required_profiles=engine_required_profiles,
        data_requirements=strategy.data_requirements,
        dataset_streams=dataset.streams,
        compatibility=tuple(records),
        strategy_manifest_sha256=sha256_model(strategy),
        strategy_code_evidence_sha256=strategy_code_evidence_sha256,
        training_input_evidence_sha256=training_input_evidence_sha256,
        external_strategy_admission_sha256=external_strategy_admission_sha256,
        dataset_manifest_sha256=sha256_model(dataset),
        engine_capabilities_sha256=sha256_model(engine),
        runtime_capabilities_sha256=sha256_model(runtime),
        run_policy_sha256=sha256_model(policy),
        required_sandbox=policy.required_sandbox,
        compiled_at=datetime.now(UTC),
    )
