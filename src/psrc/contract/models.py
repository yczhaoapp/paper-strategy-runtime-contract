from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from psrc.constants import CONTRACT_VERSION

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")]
ContractVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
ExtensionNamespace = Annotated[
    str,
    Field(
        pattern=(
            r"^(?:[a-z](?:[a-z0-9-]*[a-z0-9])?\.)+"
            r"[a-z](?:[a-z0-9-]*[a-z0-9])?$"
        )
    ),
]
ExtensionMap = dict[ExtensionNamespace, dict[str, Any]]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


class StrategyKind(StrEnum):
    RULE = "rule"
    SUPERVISED = "supervised"
    REINFORCEMENT_LEARNING = "reinforcement_learning"


class TrainingMode(StrEnum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


class TimeframeMode(StrEnum):
    EVENT = "event"
    BAR = "bar"


class DataKind(StrEnum):
    BAR = "bar"
    TRADE = "trade"
    QUOTE_L1 = "quote_l1"
    BOOK_SNAPSHOT_L2 = "book_snapshot_l2"


class ActionKind(StrEnum):
    NO_OP = "no_op"
    PREDICTION = "prediction"
    TARGET_POSITION = "target_position"
    TARGET_WEIGHT = "target_weight"
    SUBMIT_ORDER = "submit_order"
    CANCEL_ORDER = "cancel_order"
    REPLACE_ORDER = "replace_order"


class SandboxMode(StrEnum):
    DEVELOPMENT = "development"
    STRICT_CONTAINER = "strict_container"


class CompatibilityResult(StrEnum):
    EXACT = "EXACT"
    TRANSFORMED_LOSSLESS = "TRANSFORMED_LOSSLESS"
    TRANSFORMED_LOSSY = "TRANSFORMED_LOSSY"
    UNSUPPORTED = "UNSUPPORTED"


class SupportLevel(StrEnum):
    PROFILED = "PROFILED"
    ADAPTER_AVAILABLE = "ADAPTER_AVAILABLE"
    CONFORMANCE_VERIFIED = "CONFORMANCE_VERIFIED"
    PRODUCTION_CERTIFIED = "PRODUCTION_CERTIFIED"


class Timeframe(ContractModel):
    mode: TimeframeMode
    interval: str | None = Field(default=None, description="ISO-8601 duration, e.g. PT1M or P1D")
    timezone: str = "UTC"
    calendar: str = "24/7"
    alignment: str = "epoch"

    @model_validator(mode="after")
    def validate_interval(self) -> Timeframe:
        if self.mode == TimeframeMode.EVENT and self.interval is not None:
            raise ValueError("event timeframe must not declare an interval")
        if self.mode == TimeframeMode.BAR and not self.interval:
            raise ValueError("bar timeframe requires an interval")
        return self


class LifecycleRequirements(ContractModel):
    training: TrainingMode
    inference: Literal["required"] = "required"
    backtest: Literal["required"] = "required"
    state_checkpointing: bool = False


class DataRequirement(ContractModel):
    stream_id: Identifier
    kind: DataKind
    timeframe: Timeframe
    symbols: tuple[Identifier, ...] = ()
    required_fields: frozenset[str]
    optional_fields: frozenset[str] = frozenset()
    lookback: int = Field(default=1, ge=1)
    depth: int | None = Field(default=None, ge=1)
    max_staleness_ns: int | None = Field(default=None, ge=0)


class ActionRequirements(ContractModel):
    allowed: frozenset[ActionKind]
    max_abs_position: Decimal | None = Field(default=None, gt=0)
    max_order_quantity: Decimal | None = Field(default=None, gt=0)


class ResourcePolicy(ContractModel):
    sandbox: SandboxMode = SandboxMode.STRICT_CONTAINER
    network: Literal["deny", "allow"] = "deny"
    filesystem: Literal["artifact_store_only", "read_only_data", "unrestricted"] = (
        "artifact_store_only"
    )
    allowed_imports: frozenset[str] = frozenset({"math", "statistics", "numpy"})
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    memory_mb: int = Field(default=512, ge=64, le=16384)
    process_limit: int = Field(default=32, ge=1, le=1024)

    @model_validator(mode="after")
    def validate_imports_against_resource_policy(self) -> ResourcePolicy:
        network_modules = frozenset(
            {"aiohttp", "ftplib", "http", "requests", "smtplib", "socket", "urllib"}
        )
        filesystem_modules = frozenset({"os", "pathlib", "shutil", "tempfile"})
        process_modules = frozenset({"ctypes", "multiprocessing", "pty", "subprocess"})
        forbidden: set[str] = set()
        runtime_modules = {
            module
            for module in self.allowed_imports
            if module == "psrc" or module.startswith("psrc.")
        }
        if self.network == "deny":
            forbidden.update(self.allowed_imports & network_modules)
        if self.filesystem != "unrestricted":
            forbidden.update(self.allowed_imports & filesystem_modules)
        forbidden.update(runtime_modules)
        forbidden.update(self.allowed_imports & process_modules)
        if forbidden:
            raise ValueError(
                "allowed_imports contains reserved runtime or denied resource modules: "
                f"{sorted(forbidden)}"
            )
        return self


class StrategyManifest(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    strategy_id: Identifier
    strategy_version: str
    kind: StrategyKind
    entrypoint: str
    required_profiles: frozenset[Identifier]
    lifecycle: LifecycleRequirements
    data_requirements: tuple[DataRequirement, ...]
    action_requirements: ActionRequirements
    resources: ResourcePolicy = ResourcePolicy()
    deterministic: bool = True
    seed: int = 0
    extensions: ExtensionMap = Field(default_factory=dict)


class SourceFileEvidence(ContractModel):
    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_relative_python_path(self) -> SourceFileEvidence:
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("source evidence path must be a safe relative Python path")
        return self


class StrategyCodeEvidence(ContractModel):
    """Content-addressed strategy package and trusted runtime implementation."""

    entrypoint: str
    package_files: tuple[SourceFileEvidence, ...]
    package_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_distribution: Literal["paper-strategy-runtime-contract"] = (
        "paper-strategy-runtime-contract"
    )
    runtime_version: str = CONTRACT_VERSION
    runtime_file_count: int = Field(ge=1)
    runtime_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_package_tree_hash(self) -> StrategyCodeEvidence:
        paths = tuple(item.path for item in self.package_files)
        if not paths or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("package source evidence must be non-empty, unique, and sorted")
        payload = {"files": [item.model_dump(mode="json") for item in self.package_files]}
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.package_sha256:
            raise ValueError("package source tree hash does not match package_sha256")
        return self


class DatasetStream(ContractModel):
    stream_id: Identifier
    kind: DataKind
    timeframe: Timeframe
    symbols: frozenset[Identifier]
    fields: frozenset[str]
    record_count: int = Field(ge=0)
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DatasetManifest(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    dataset_id: Identifier
    dataset_version: str
    generated_at: datetime
    streams: tuple[DatasetStream, ...]
    public_or_synthetic: bool
    extensions: ExtensionMap = Field(default_factory=dict)


class ExecutionSemantics(ContractModel):
    partial_fills: bool
    cancel: bool
    replace: Literal["native", "cancel_new", "unsupported"]
    same_timestamp_ordering: str
    queue_model: str | None = None
    fill_model: str
    fee_model: str
    slippage_model: str
    latency_model: str


class EngineCapabilities(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    engine_id: Identifier
    engine_version: str
    adapter_version: str
    support_level: SupportLevel
    profiles: frozenset[Identifier]
    data_kinds: frozenset[DataKind]
    action_kinds: frozenset[ActionKind]
    execution: ExecutionSemantics
    sandbox_modes: frozenset[SandboxMode] = frozenset()
    extensions: ExtensionMap = Field(default_factory=dict)


class RuntimeCapabilities(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    runtime_id: Identifier
    runtime_version: str
    profiles: frozenset[Identifier]
    extensions: ExtensionMap = Field(default_factory=dict)


class RunPolicy(ContractModel):
    strict: bool = True
    allow_lossy: bool = False
    allowed_transformations: frozenset[Identifier] = frozenset()
    transformation_parameters: dict[Identifier, dict[str, Any]] = Field(default_factory=dict)
    required_sandbox: SandboxMode = SandboxMode.STRICT_CONTAINER
    minimum_engine_support: SupportLevel = SupportLevel.ADAPTER_AVAILABLE
    allow_engine_substitution: Literal[False] = False


class CompatibilityRecord(ContractModel):
    result: CompatibilityResult
    transformation_id: Identifier | None = None
    transformation_version: str | None = None
    rationale: str
    input_schema_sha256: str | None = None
    output_schema_sha256: str | None = None
    affected_records: int = Field(default=0, ge=0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reversible: bool
    disableable: Literal[True] = True


class ExecutionPlan(ContractModel):
    contract_version: ContractVersion = CONTRACT_VERSION
    run_id: Identifier
    strategy_id: Identifier
    dataset_id: Identifier
    engine_id: Identifier
    runtime_id: Identifier
    runtime_required_profiles: frozenset[Identifier] = frozenset()
    engine_required_profiles: frozenset[Identifier] = frozenset()
    data_requirements: tuple[DataRequirement, ...] = ()
    dataset_streams: tuple[DatasetStream, ...]
    compatibility: tuple[CompatibilityRecord, ...]
    strategy_manifest_sha256: str
    strategy_code_evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    training_input_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    external_strategy_admission_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    dataset_manifest_sha256: str
    engine_capabilities_sha256: str
    runtime_capabilities_sha256: str
    run_policy_sha256: str
    required_sandbox: SandboxMode = SandboxMode.DEVELOPMENT
    compiled_at: datetime
