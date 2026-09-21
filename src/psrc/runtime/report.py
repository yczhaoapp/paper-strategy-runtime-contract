from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, RootModel, model_validator

from psrc.authoring.audit import audit_external_implementation
from psrc.authoring.models import ExternalStrategyAdmission
from psrc.constants import CONTRACT_VERSION
from psrc.contract.compatibility import apply_compatibility_plan
from psrc.contract.errors import ContractError
from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    ContractModel,
    DatasetManifest,
    EngineCapabilities,
    ExecutionPlan,
    Identifier,
    RunPolicy,
    SandboxMode,
    StrategyCodeEvidence,
    StrategyManifest,
)
from psrc.domain.account import AccountSnapshot, Fill
from psrc.domain.actions import Action
from psrc.domain.market import MarketEvent
from psrc.papers.models import PaperDocument
from psrc.runtime.artifacts import ArtifactManifest
from psrc.runtime.training import (
    TrainingInputEvidence,
    TrainingRequest,
    build_training_input_evidence,
)


class DecisionRecord(ContractModel):
    sequence: int = Field(ge=0)
    event_id: Identifier
    event_time: datetime
    actions: tuple[Action, ...]


class OrderEventRecord(ContractModel):
    sequence: int = Field(ge=0)
    client_order_id: Identifier
    instrument_id: Identifier
    event_time: datetime
    status: Literal["accepted", "replaced", "canceled", "filled", "satisfied", "expired"]
    details: dict[str, str] = Field(default_factory=dict)


class RuntimeLogRecord(ContractModel):
    sequence: int = Field(ge=0)
    timestamp: datetime
    level: Literal["debug", "info", "warning", "error"]
    stage: str
    message: str
    fields: dict[str, Any] = Field(default_factory=dict)


class RunMetrics(ContractModel):
    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    total_return: Decimal
    decisions: int = Field(ge=0)
    orders: int = Field(ge=0)
    fills: int = Field(ge=0)
    no_ops: int = Field(ge=0)


class RunReport(ContractModel):
    contract_version: str = CONTRACT_VERSION
    run_id: Identifier
    status: Literal["succeeded"]
    execution_plan: ExecutionPlan
    sandbox_mode: SandboxMode
    started_at: datetime
    finished_at: datetime
    metrics: RunMetrics
    decisions: tuple[DecisionRecord, ...]
    orders: tuple[OrderEventRecord, ...]
    fills: tuple[Fill, ...]
    account_snapshots: tuple[AccountSnapshot, ...]
    assumptions: tuple[str, ...]
    logs: tuple[RuntimeLogRecord, ...] = ()
    lifecycle: tuple[str, ...] = ()
    artifacts: tuple[ArtifactManifest, ...] = ()
    errors: tuple[dict[str, Any], ...] = ()


class FailureReport(ContractModel):
    contract_version: str = CONTRACT_VERSION
    run_id: Identifier
    status: Literal["failed"] = "failed"
    started_at: datetime
    finished_at: datetime
    error: ContractError
    strategy_manifest: StrategyManifest | None = None
    dataset_manifest: DatasetManifest | None = None
    engine_capabilities: EngineCapabilities | None = None
    run_policy: RunPolicy | None = None
    actual_input: dict[str, Any] = Field(default_factory=dict)


def event_stream_sha256(events: tuple[MarketEvent, ...]) -> str:
    return sha256_model({"events": [event.model_dump(mode="json") for event in events]})


class RunInputEvidence(ContractModel):
    contract_version: str = CONTRACT_VERSION
    dataset_id: Identifier
    source_events: tuple[MarketEvent, ...]
    effective_events: tuple[MarketEvent, ...]
    source_data_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_data_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transformation_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_hashes(self) -> RunInputEvidence:
        if event_stream_sha256(self.source_events) != self.source_data_sha256:
            raise ValueError("source event hash does not match source_data_sha256")
        if event_stream_sha256(self.effective_events) != self.effective_data_sha256:
            raise ValueError("effective event hash does not match effective_data_sha256")
        return self


class RunBundle(ContractModel):
    contract_version: str = CONTRACT_VERSION
    report: RunReport
    strategy_manifest: StrategyManifest
    dataset_manifest: DatasetManifest
    engine_capabilities: EngineCapabilities
    run_policy: RunPolicy
    input_evidence: RunInputEvidence
    strategy_code_evidence: StrategyCodeEvidence | None = None
    training_request: TrainingRequest | None = None
    training_input_evidence: TrainingInputEvidence | None = None
    external_strategy_admission: ExternalStrategyAdmission | None = None
    external_paper_document: PaperDocument | None = None

    @model_validator(mode="after")
    def validate_context_integrity(self) -> RunBundle:
        plan = self.report.execution_plan
        mismatches: dict[str, object] = {
            name: observed
            for name, observed, expected in (
                ("run_id", plan.run_id, self.report.run_id),
                ("strategy_id", plan.strategy_id, self.strategy_manifest.strategy_id),
                ("dataset_id", plan.dataset_id, self.dataset_manifest.dataset_id),
                ("engine_id", plan.engine_id, self.engine_capabilities.engine_id),
                (
                    "strategy_manifest_sha256",
                    plan.strategy_manifest_sha256,
                    sha256_model(self.strategy_manifest),
                ),
                (
                    "dataset_manifest_sha256",
                    plan.dataset_manifest_sha256,
                    sha256_model(self.dataset_manifest),
                ),
                (
                    "engine_capabilities_sha256",
                    plan.engine_capabilities_sha256,
                    sha256_model(self.engine_capabilities),
                ),
                ("run_policy_sha256", plan.run_policy_sha256, sha256_model(self.run_policy)),
            )
            if observed != expected
        }
        if self.report.artifacts and self.training_request is None:
            mismatches["training_request"] = "missing for trainable artifacts"
        if self.training_request is None and self.training_input_evidence is not None:
            mismatches["training_input_evidence"] = "declared without a training request"
        elif self.training_request is not None and self.training_input_evidence is not None:
            expected_training_evidence = build_training_input_evidence(self.training_request)
            if self.training_input_evidence != expected_training_evidence:
                mismatches["training_input_evidence"] = "does not match training request"
            if plan.training_input_evidence_sha256 != sha256_model(
                self.training_input_evidence
            ):
                mismatches["training_input_evidence_sha256"] = (
                    plan.training_input_evidence_sha256
                )
        elif plan.training_input_evidence_sha256 is not None:
            mismatches["training_input_evidence_sha256"] = (
                "declared without training evidence"
            )
        if self.external_strategy_admission is None:
            if plan.external_strategy_admission_sha256 is not None:
                mismatches["external_strategy_admission"] = "missing"
            if self.external_paper_document is not None:
                mismatches["external_paper_document"] = "declared without admission"
        else:
            admission = self.external_strategy_admission
            if not admission.approved_for_runtime_validation:
                mismatches["external_strategy_admission"] = "not approved"
            if admission.audit.strategy_id != self.strategy_manifest.strategy_id:
                mismatches["external_strategy_id"] = admission.audit.strategy_id
            if admission.strategy_manifest_sha256 != sha256_model(self.strategy_manifest):
                mismatches["external_strategy_manifest_sha256"] = (
                    admission.strategy_manifest_sha256
                )
            if self.strategy_code_evidence is None or (
                admission.strategy_code_evidence_sha256
                != sha256_model(self.strategy_code_evidence)
            ):
                mismatches["external_strategy_code_evidence_sha256"] = (
                    admission.strategy_code_evidence_sha256
                )
            if plan.external_strategy_admission_sha256 != sha256_model(admission):
                mismatches["external_strategy_admission_sha256"] = (
                    plan.external_strategy_admission_sha256
                )
            if self.external_paper_document is None:
                mismatches["external_paper_document"] = "missing"
            elif admission.paper_document_sha256 != sha256_model(
                self.external_paper_document
            ):
                mismatches["external_paper_document_sha256"] = (
                    admission.paper_document_sha256
                )
            elif admission.paper_source_sha256 != self.external_paper_document.source_sha256:
                mismatches["external_paper_source_sha256"] = admission.paper_source_sha256
            else:
                expected_audit = audit_external_implementation(
                    admission.paper_spec,
                    self.strategy_manifest,
                    self.external_paper_document,
                )
                if admission.audit != expected_audit:
                    mismatches["external_strategy_audit"] = "not deterministically reproducible"
        if self.strategy_code_evidence is None:
            if plan.strategy_code_evidence_sha256 is not None:
                mismatches["strategy_code_evidence"] = "missing"
        elif plan.strategy_code_evidence_sha256 != sha256_model(self.strategy_code_evidence):
            mismatches["strategy_code_evidence_sha256"] = plan.strategy_code_evidence_sha256
        elif self.strategy_code_evidence.runtime_version != plan.contract_version:
            mismatches["strategy_runtime_version"] = self.strategy_code_evidence.runtime_version
        if self.input_evidence.dataset_id != self.dataset_manifest.dataset_id:
            mismatches["input_evidence_dataset_id"] = self.input_evidence.dataset_id
        if len(self.dataset_manifest.streams) == 1 and (
            self.dataset_manifest.streams[0].data_sha256 != self.input_evidence.source_data_sha256
        ):
            mismatches["source_data_sha256"] = self.input_evidence.source_data_sha256
        planned_transformations = tuple(
            item.transformation_id
            for item in plan.compatibility
            if item.transformation_id is not None
        )
        if self.input_evidence.transformation_ids != planned_transformations:
            mismatches["transformation_ids"] = self.input_evidence.transformation_ids
        expected_effective_events = apply_compatibility_plan(
            self.input_evidence.source_events, plan
        )
        if self.input_evidence.effective_events != expected_effective_events:
            mismatches["effective_events"] = "do not match the compiled compatibility plan"
        if len(self.report.decisions) != len(self.input_evidence.effective_events):
            mismatches["effective_event_count"] = len(self.input_evidence.effective_events)
        if self.training_request is not None:
            for artifact in self.report.artifacts:
                if artifact.training_dataset_id != self.training_request.dataset_id:
                    mismatches["artifact_training_dataset_id"] = artifact.training_dataset_id
                if artifact.seed != self.training_request.seed:
                    mismatches["artifact_seed"] = artifact.seed
        if mismatches:
            raise ValueError(f"RunBundle context does not match its execution plan: {mismatches}")
        return self


class UnifiedRunReport(
    RootModel[Annotated[RunReport | FailureReport, Field(discriminator="status")]]
):
    pass


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def write_run_bundle(report: RunReport, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "report.json", report.model_dump(mode="json"))
    _write_json(output / "execution-plan.json", report.execution_plan.model_dump(mode="json"))
    _write_json(
        output / "decisions.json",
        [record.model_dump(mode="json") for record in report.decisions],
    )
    _write_json(
        output / "orders.json",
        [record.model_dump(mode="json") for record in report.orders],
    )
    _write_json(output / "fills.json", [fill.model_dump(mode="json") for fill in report.fills])
    _write_json(
        output / "account-snapshots.json",
        [snapshot.model_dump(mode="json") for snapshot in report.account_snapshots],
    )
    _write_json(
        output / "artifacts.json",
        [artifact.model_dump(mode="json") for artifact in report.artifacts],
    )
    _write_json(output / "logs.json", [record.model_dump(mode="json") for record in report.logs])
    (output / "report.html").write_text(_success_html(report), encoding="utf-8")


def write_failure_bundle(report: FailureReport, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "report.json", report.model_dump(mode="json"))
    _write_json(output / "error.json", report.error.model_dump(mode="json"))
    (output / "report.html").write_text(_failure_html(report), encoding="utf-8")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(title)}</title><style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;
padding:0 1rem;color:#17202a}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccd1d1;padding:.45rem;text-align:left}}
code,pre{{background:#f4f6f7;padding:.15rem .3rem}}pre{{overflow:auto;padding:1rem}}
.ok{{color:#147a3d}}.failed{{color:#b42318}}
</style></head><body>{body}</body></html>
"""


def _success_html(
    report: RunReport,
    input_evidence: RunInputEvidence | None = None,
    code_evidence: StrategyCodeEvidence | None = None,
) -> str:
    compatibility = "".join(
        "<tr>"
        f"<td>{escape(str(item.result))}</td>"
        f"<td>{escape(item.transformation_id or 'none')}</td>"
        f"<td>{escape(item.rationale)}</td>"
        f"<td>{item.affected_records}</td>"
        "</tr>"
        for item in report.execution_plan.compatibility
    )
    metrics = "".join(
        f"<tr><th>{escape(name)}</th><td>{escape(str(value))}</td></tr>"
        for name, value in report.metrics.model_dump(mode="json").items()
    )
    artifacts = (
        "".join(
            f"<li><code>{escape(item.artifact_id)}</code> ({escape(item.artifact_kind)})</li>"
            for item in report.artifacts
        )
        or "<li>不适用</li>"
    )
    assumptions = "".join(f"<li>{escape(item)}</li>" for item in report.assumptions)
    input_section = ""
    if input_evidence is not None:
        input_section = (
            "<h2>实际输入</h2><table>"
            f"<tr><th>源事件数</th><td>{len(input_evidence.source_events)}</td></tr>"
            f"<tr><th>有效事件数</th><td>{len(input_evidence.effective_events)}</td></tr>"
            "<tr><th>源数据 SHA-256</th><td><code>"
            f"{input_evidence.source_data_sha256}</code></td></tr>"
            "<tr><th>有效数据 SHA-256</th><td><code>"
            f"{input_evidence.effective_data_sha256}</code></td></tr>"
            "</table>"
        )
    code_section = ""
    if code_evidence is not None:
        code_section = (
            "<h2>源码证据</h2><table>"
            f"<tr><th>策略源码文件数</th><td>{len(code_evidence.package_files)}</td></tr>"
            "<tr><th>策略包 SHA-256</th><td><code>"
            f"{code_evidence.package_sha256}</code></td></tr>"
            "<tr><th>运行时源码 SHA-256</th><td><code>"
            f"{code_evidence.runtime_source_sha256}</code></td></tr>"
            "<tr><th>执行计划证据 SHA-256</th><td><code>"
            f"{report.execution_plan.strategy_code_evidence_sha256}</code></td></tr>"
            "</table>"
        )
    logs = "".join(
        f"<tr><td>{item.sequence}</td><td>{escape(item.stage)}</td>"
        f"<td>{escape(item.level)}</td><td>{escape(item.message)}</td></tr>"
        for item in report.logs
    )
    return _page(
        f"PSRC 运行 {report.run_id}",
        f"<h1>PSRC 统一运行报告</h1><p class=ok><strong>成功（SUCCEEDED）</strong></p>"
        f"<p>运行 <code>{escape(report.run_id)}</code>；策略 "
        f"<code>{escape(report.execution_plan.strategy_id)}</code>；引擎 "
        f"<code>{escape(report.execution_plan.engine_id)}</code>；沙箱 "
        f"<code>{escape(str(report.sandbox_mode))}</code>.</p>"
        f"<h2>指标</h2><table>{metrics}</table>"
        f"{input_section}"
        f"{code_section}"
        "<h2>兼容性审计</h2><table><tr><th>结果</th><th>转换</th>"
        f"<th>理由</th><th>影响记录数</th></tr>{compatibility}</table>"
        f"<h2>产物</h2><ul>{artifacts}</ul>"
        f"<h2>生命周期</h2><p>{escape(' -> '.join(report.lifecycle) or '引擎直接执行')}</p>"
        "<h2>训练、推理与回测日志</h2><table><tr><th>序号</th><th>阶段</th>"
        f"<th>级别</th><th>消息</th></tr>{logs}</table>"
        f"<h2>假设</h2><ul>{assumptions}</ul>",
    )


def _failure_html(report: FailureReport) -> str:
    payload = escape(json.dumps(report.error.model_dump(mode="json"), indent=2))
    return _page(
        f"PSRC 失败运行 {report.run_id}",
        f"<h1>PSRC 统一运行报告</h1><p class=failed><strong>失败（FAILED）</strong></p>"
        f"<p>运行 <code>{escape(report.run_id)}</code> 在阶段 "
        f"<code>{escape(str(report.error.stage))}</code> 失败，错误码 "
        f"<code>{escape(str(report.error.code))}</code>.</p>"
        f"<h2>机器可读错误</h2><pre>{payload}</pre>",
    )


def write_input_evidence(
    output: Path,
    *,
    report: RunReport,
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
    events: tuple[MarketEvent, ...],
    strategy_code_evidence: StrategyCodeEvidence | None = None,
    training: TrainingRequest | None = None,
    training_input_evidence: TrainingInputEvidence | None = None,
    external_strategy_admission: ExternalStrategyAdmission | None = None,
    external_paper_document: PaperDocument | None = None,
) -> None:
    effective_events = apply_compatibility_plan(events, report.execution_plan)
    input_evidence = RunInputEvidence(
        dataset_id=dataset.dataset_id,
        source_events=events,
        effective_events=effective_events,
        source_data_sha256=event_stream_sha256(events),
        effective_data_sha256=event_stream_sha256(effective_events),
        transformation_ids=tuple(
            item.transformation_id
            for item in report.execution_plan.compatibility
            if item.transformation_id is not None
        ),
    )
    bundle = RunBundle(
        report=report,
        strategy_manifest=strategy,
        dataset_manifest=dataset,
        engine_capabilities=engine,
        run_policy=policy,
        input_evidence=input_evidence,
        strategy_code_evidence=strategy_code_evidence,
        training_request=training,
        training_input_evidence=training_input_evidence,
        external_strategy_admission=external_strategy_admission,
        external_paper_document=external_paper_document,
    )
    _write_json(output / "strategy-manifest.json", strategy.model_dump(mode="json"))
    _write_json(output / "dataset-manifest.json", dataset.model_dump(mode="json"))
    _write_json(output / "engine-capabilities.json", engine.model_dump(mode="json"))
    _write_json(output / "run-policy.json", policy.model_dump(mode="json"))
    _write_json(output / "input-evidence.json", input_evidence.model_dump(mode="json"))
    if strategy_code_evidence is not None:
        _write_json(
            output / "strategy-code-evidence.json",
            strategy_code_evidence.model_dump(mode="json"),
        )
    _write_json(
        output / "source-events.json",
        [event.model_dump(mode="json") for event in input_evidence.source_events],
    )
    _write_json(
        output / "effective-events.json",
        [event.model_dump(mode="json") for event in input_evidence.effective_events],
    )
    if training is not None:
        _write_json(output / "training-request.json", training.model_dump(mode="json"))
    if training_input_evidence is not None:
        _write_json(
            output / "training-input-evidence.json",
            training_input_evidence.model_dump(mode="json"),
        )
    if external_strategy_admission is not None:
        _write_json(
            output / "external-strategy-admission.json",
            external_strategy_admission.model_dump(mode="json"),
        )
    if external_paper_document is not None:
        _write_json(
            output / "paper-document.json",
            external_paper_document.model_dump(mode="json"),
        )
    _write_json(output / "bundle.json", bundle.model_dump(mode="json"))
    (output / "report.html").write_text(
        _success_html(report, input_evidence, strategy_code_evidence), encoding="utf-8"
    )
