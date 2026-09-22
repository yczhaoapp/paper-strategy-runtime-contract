from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ValidationError

from psrc.adapters.reference import ReferenceEngine
from psrc.adapters.reference import capabilities as reference_capabilities
from psrc.adapters.registry import ENGINE_IDS, EngineId, resolve_adapter
from psrc.authoring.admission import build_external_admission
from psrc.authoring.audit import audit_manifest
from psrc.authoring.models import (
    AgentAuditReport,
    ExternalStrategyAdmission,
    PaperStrategySpec,
)
from psrc.constants import (
    CONTRACT_VERSION,
    JSON_SCHEMA_DIALECT,
    SCHEMA_BASE_URI,
)
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import canonical_json_bytes, sha256_model
from psrc.contract.models import (
    DatasetManifest,
    EngineCapabilities,
    ExecutionPlan,
    RunPolicy,
    RuntimeCapabilities,
    SandboxMode,
    StrategyCodeEvidence,
    StrategyManifest,
    Timeframe,
    TimeframeMode,
    TrainingMode,
)
from psrc.domain.account import AccountSnapshot, Fill
from psrc.domain.actions import ActionEnvelope
from psrc.domain.market import MarketEvent
from psrc.evidence.verify import verify_acceptance
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.papers.ingest import ingest
from psrc.papers.models import (
    DataSourceEvidence,
    PaperDocument,
    PaperRecipe,
    PaperSource,
    PaperStrategyBinding,
    PublicDatasetSource,
    ReproductionSpec,
)
from psrc.runtime.artifacts import ArtifactManifest, ArtifactStore
from psrc.runtime.guards import validate_dataset_events
from psrc.runtime.orchestrator import run_rule, run_trainable
from psrc.runtime.package import (
    StrategyPackage,
    discover_strategy_packages,
    export_strategy_packages,
    load_strategy,
    load_strategy_manifest,
)
from psrc.runtime.report import (
    DecisionRecord,
    FailureReport,
    RunBundle,
    RunInputEvidence,
    RunReport,
    RuntimeLogRecord,
    UnifiedRunReport,
    clear_run_output,
    event_stream_sha256,
    write_failure_bundle,
    write_input_evidence,
    write_run_bundle,
)
from psrc.runtime.training import (
    TrainableRuntimeStrategy,
    TrainingInputEvidence,
    TrainingRequest,
    build_training_input_evidence,
    validate_training_input_evidence,
)
from psrc.sandbox.container import ContainerMounts, DockerSandbox

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "paper-document": PaperDocument,
    "paper-recipe": PaperRecipe,
    "paper-source": PaperSource,
    "paper-strategy-binding": PaperStrategyBinding,
    "data-source-evidence": DataSourceEvidence,
    "public-dataset-source": PublicDatasetSource,
    "reproduction-spec": ReproductionSpec,
    "strategy-manifest": StrategyManifest,
    "dataset-manifest": DatasetManifest,
    "engine-capabilities": EngineCapabilities,
    "runtime-capabilities": RuntimeCapabilities,
    "run-policy": RunPolicy,
    "execution-plan": ExecutionPlan,
    "contract-error": ContractError,
    "artifact-manifest": ArtifactManifest,
    "training-request": TrainingRequest,
    "training-input-evidence": TrainingInputEvidence,
    "run-report": RunReport,
    "failure-report": FailureReport,
    "unified-run-report": UnifiedRunReport,
    "run-bundle": RunBundle,
    "run-input-evidence": RunInputEvidence,
    "strategy-code-evidence": StrategyCodeEvidence,
    "market-event": MarketEvent,
    "account-snapshot": AccountSnapshot,
    "action": ActionEnvelope,
    "fill": Fill,
    "decision-record": DecisionRecord,
    "runtime-log-record": RuntimeLogRecord,
    "paper-strategy-spec": PaperStrategySpec,
    "agent-audit-report": AgentAuditReport,
    "external-strategy-admission": ExternalStrategyAdmission,
}


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _stable_schema_document(value: Any) -> Any:
    """Normalize unordered scalar defaults emitted from set-backed model fields."""
    if isinstance(value, dict):
        return {key: _stable_schema_document(item) for key, item in value.items()}
    if isinstance(value, list):
        normalized = [_stable_schema_document(item) for item in value]
        if all(isinstance(item, (str, int, float, bool)) for item in normalized):
            return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
        return normalized
    return value


def _schema_export(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    catalog: dict[str, str] = {}
    for name, model in SCHEMA_MODELS.items():
        filename = f"{name}.schema.json"
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        schema["$schema"] = JSON_SCHEMA_DIALECT
        schema["$id"] = f"{SCHEMA_BASE_URI}/{filename}?contract={CONTRACT_VERSION}"
        _write_json(output / filename, _stable_schema_document(schema))
        catalog[name] = filename
    _write_json(output / "catalog.json", {"contract_version": CONTRACT_VERSION, "schemas": catalog})
    print(f"exported {len(catalog)} schemas to {output}")
    return 0


def _validate(model_name: str, input_path: Path) -> int:
    model = SCHEMA_MODELS[model_name]
    try:
        value = model.model_validate(_load_yaml(input_path))
    except ValidationError as exc:
        print(exc.json(indent=2))
        return 2
    print(value.model_dump_json(indent=2))
    return 0


def _author_audit(spec_path: Path, manifest_path: Path, output: Path) -> int:
    try:
        spec = PaperStrategySpec.model_validate(_load_yaml(spec_path))
        manifest = StrategyManifest.model_validate(_load_yaml(manifest_path))
    except ValidationError as exc:
        print(exc.json(indent=2))
        return 2
    report = audit_manifest(spec, manifest)
    _write_json(output, report.model_dump(mode="json"))
    print(report.model_dump_json(indent=2))
    return 0 if report.approved_for_compilation else 4


def _author_run(
    spec_path: Path,
    source_path: Path,
    strategy_dir: Path,
    output: Path,
    *,
    require_strict: bool,
    engine_id: EngineId,
) -> int:
    clear_run_output(output)
    try:
        spec = PaperStrategySpec.model_validate(_load_yaml(spec_path))
    except ValidationError as exc:
        print(exc.json(indent=2))
        return 2
    document = ingest(source_path, source_uri=spec.reference.locator)
    package = load_strategy_manifest(strategy_dir)
    admission = build_external_admission(spec, document, package)
    _write_json(output / "paper-document.json", document.model_dump(mode="json"))
    _write_json(
        output / "external-strategy-admission.json",
        admission.model_dump(mode="json"),
    )
    if not admission.approved_for_runtime_validation:
        print(admission.model_dump_json(indent=2))
        return 4
    report = _run_package(
        package,
        output,
        require_strict=require_strict,
        engine_id=engine_id,
        external_strategy_admission=admission,
        external_paper_document=document,
    )
    print(report.model_dump_json(indent=2))
    return 0


def _acceptance_verify(
    matrix_path: Path, output: Path, evidence_root: Path, *, require_strict: bool
) -> int:
    report = verify_acceptance(matrix_path, evidence_root, require_strict=require_strict)
    _write_json(output, report)
    if report["status"] != "passed":
        failed = [name for name, item in report["checks"].items() if not item["passed"]]
        print(f"Verification failed: {', '.join(failed)}; report: {output}")
        return 1
    if report["verification_scope"] == "strict_container_verification":
        print(f"Strict-container verification passed; report: {output}")
    else:
        print(f"Development preflight passed; report: {output}")
    return 0


def _demo_sma(output: Path) -> int:
    from psrc.examples.sma_cross import SmaCrossStrategy

    clear_run_output(output)
    events = minute_bars()
    strategy = SmaCrossStrategy()
    dataset = minute_bar_manifest(events)
    sandbox = (
        SandboxMode.STRICT_CONTAINER
        if DockerSandbox.current_process_attested()
        else SandboxMode.DEVELOPMENT
    )
    engine = reference_capabilities(strict_container=sandbox == SandboxMode.STRICT_CONTAINER)
    policy = RunPolicy(required_sandbox=sandbox)
    plan = compile_run(
        run_id="demo.sma-cross",
        strategy=strategy.manifest,
        dataset=dataset,
        engine=engine,
        policy=policy,
    )
    report = run_rule(
        plan=plan,
        strategy=strategy,
        events=events,
        engine=ReferenceEngine(declared_capabilities=engine),
        sandbox_mode=sandbox,
    )
    write_run_bundle(report, output)
    write_input_evidence(
        output,
        report=report,
        strategy=strategy.manifest,
        dataset=dataset,
        engine=engine,
        policy=policy,
        events=events,
    )
    print(report.model_dump_json(indent=2))
    return 0


def _load_package_inputs(
    package: StrategyPackage,
) -> tuple[
    DatasetManifest,
    tuple[MarketEvent, ...],
    TrainingRequest | None,
    TrainingInputEvidence | None,
]:
    try:
        dataset = DatasetManifest.model_validate_json(
            (package.root / "dataset-manifest.json").read_text(encoding="utf-8")
        )
        payload = json.loads((package.root / "input-events.json").read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise TypeError("input-events.json must contain a JSON array")
        events = tuple(MarketEvent.model_validate(item) for item in payload)
        training_path = package.root / "training-request.json"
        training = (
            TrainingRequest.model_validate_json(training_path.read_text(encoding="utf-8"))
            if training_path.is_file()
            else None
        )
        training_evidence_path = package.root / "training-input-evidence.json"
        training_evidence = (
            TrainingInputEvidence.model_validate_json(
                training_evidence_path.read_text(encoding="utf-8")
            )
            if training_evidence_path.is_file()
            else None
        )
    except Exception as exc:
        raise ContractViolation(
            ContractError(
                run_id="package.inputs",
                stage=ErrorStage.DISCOVERY,
                code=ErrorCode.MANIFEST_INVALID,
                message="Strategy package input evidence is missing or invalid",
                strategy_id=package.manifest.strategy_id,
                details={"package_root": str(package.root)},
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
    validate_dataset_events(
        run_id="package.inputs",
        strategy=package.manifest,
        dataset=dataset,
        events=events,
    )
    required = package.manifest.lifecycle.training == TrainingMode.REQUIRED
    if required != (training is not None) or required != (training_evidence is not None):
        raise ContractViolation(
            ContractError(
                run_id="package.inputs",
                stage=ErrorStage.DISCOVERY,
                code=ErrorCode.MANIFEST_INVALID,
                message="Training request presence does not match the strategy lifecycle",
                strategy_id=package.manifest.strategy_id,
                details={
                    "training_required": required,
                    "training_present": training is not None,
                    "training_evidence_present": training_evidence is not None,
                },
            )
        )
    if training is not None and training_evidence is not None:
        try:
            validate_training_input_evidence(training, training_evidence)
        except ValueError as exc:
            raise ContractViolation(
                ContractError(
                    run_id="package.inputs",
                    stage=ErrorStage.VALIDATION,
                    code=ErrorCode.TRAINING_DATA_MISMATCH,
                    message="Training input differs from its immutable evidence declaration",
                    strategy_id=package.manifest.strategy_id,
                    details={"fallback_used": False},
                    cause_chain=(f"{type(exc).__name__}: {exc}",),
                )
            ) from exc
    return dataset, events, training, training_evidence


def _selected_sandbox(*, require_strict: bool) -> SandboxMode:
    if DockerSandbox.current_process_attested():
        return SandboxMode.STRICT_CONTAINER
    if require_strict:
        raise ContractViolation(
            ContractError(
                run_id="sandbox.attestation",
                stage=ErrorStage.SANDBOX,
                code=ErrorCode.SANDBOX_UNAVAILABLE,
                message="Strict execution was required but runtime controls are not attested",
                details={"fallback_used": False},
            )
        )
    return SandboxMode.DEVELOPMENT


def _run_package(
    package: StrategyPackage,
    output: Path,
    *,
    require_strict: bool = False,
    engine_id: EngineId = "reference",
    external_strategy_admission: ExternalStrategyAdmission | None = None,
    external_paper_document: PaperDocument | None = None,
) -> RunReport:
    clear_run_output(output)
    sandbox = _selected_sandbox(require_strict=require_strict)
    resolved = resolve_adapter(
        engine_id,
        strict_container=sandbox == SandboxMode.STRICT_CONTAINER,
    )
    engine_capabilities = resolved.capabilities
    policy = RunPolicy(required_sandbox=sandbox)
    dataset, events, training, training_evidence = _load_package_inputs(package)
    plan = compile_run(
        run_id=f"package.{package.manifest.strategy_id}",
        strategy=package.manifest,
        dataset=dataset,
        engine=engine_capabilities,
        policy=policy,
        strategy_code_evidence_sha256=sha256_model(package.code_evidence),
        training_input_evidence_sha256=(
            sha256_model(training_evidence) if training_evidence is not None else None
        ),
        external_strategy_admission_sha256=(
            sha256_model(external_strategy_admission)
            if external_strategy_admission is not None
            else None
        ),
    )
    strategy = load_strategy(package, sandbox_mode=sandbox)
    if training is not None:
        report = run_trainable(
            plan=plan,
            strategy=cast(TrainableRuntimeStrategy, strategy),
            training=training,
            events=events,
            engine=resolved.engine,
            store=ArtifactStore(output / "artifact-store"),
            sandbox_mode=sandbox,
        )
    else:
        report = run_rule(
            plan=plan,
            strategy=strategy,
            events=events,
            engine=resolved.engine,
            sandbox_mode=sandbox,
        )
    write_run_bundle(report, output)
    write_input_evidence(
        output,
        report=report,
        strategy=package.manifest,
        dataset=dataset,
        engine=engine_capabilities,
        policy=policy,
        events=events,
        strategy_code_evidence=package.code_evidence,
        training=training,
        training_input_evidence=training_evidence,
        external_strategy_admission=external_strategy_admission,
        external_paper_document=external_paper_document,
    )
    return report


def _demo_all(output: Path, strategies_root: Path, *, require_strict: bool) -> int:
    sandbox = _selected_sandbox(require_strict=require_strict)
    summaries: list[dict[str, object]] = []
    packages = discover_strategy_packages(strategies_root)
    for package in packages:
        strategy_output = output / package.manifest.strategy_id
        report = _run_package(package, strategy_output, require_strict=require_strict)
        summaries.append(
            {
                "strategy_id": package.manifest.strategy_id,
                "kind": package.manifest.kind,
                "status": report.status,
                "decisions": report.metrics.decisions,
                "orders": report.metrics.orders,
                "fills": report.metrics.fills,
                "artifact_count": len(report.artifacts),
            }
        )
    by_kind: dict[str, int] = {}
    for summary in summaries:
        kind = str(summary["kind"])
        by_kind[kind] = by_kind.get(kind, 0) + 1
    _write_json(
        output / "summary.json",
        {
            "contract_version": CONTRACT_VERSION,
            "sandbox_mode": sandbox,
            "status": "succeeded",
            "strategy_count": len(summaries),
            "counts_by_kind": by_kind,
            "runs": summaries,
        },
    )
    print(f"completed {len(summaries)} strategy runs; summary: {output / 'summary.json'}")
    return 0


def _package_export(output: Path) -> int:
    from psrc.strategies.catalog import all_examples

    examples = all_examples()
    manifests = tuple(example.manifest for example in examples)
    export_strategy_packages(output, manifests)
    for example in examples:
        package = output / example.manifest.strategy_id
        stable_dataset = json.loads(canonical_json_bytes(example.dataset))
        _write_json(package / "dataset-manifest.json", stable_dataset)
        _write_json(
            package / "input-events.json",
            [event.model_dump(mode="json") for event in example.events],
        )
        training = getattr(example, "training", None)
        if isinstance(training, TrainingRequest):
            _write_json(package / "training-request.json", training.model_dump(mode="json"))
            _write_json(
                package / "training-input-evidence.json",
                build_training_input_evidence(training).model_dump(mode="json"),
            )
        binding_path = (
            Path(__file__).parents[2] / "papers/bindings" / f"{example.manifest.strategy_id}.json"
        )
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        _write_json(package / "paper-binding.json", binding)
        card = package / "STRATEGY_CARD.md"
        card.write_text(
            card.read_text(encoding="utf-8")
            + "\n## 论文依据\n\n"
            + f"- 来源 ID：`{binding['source_id']}`\n"
            + f"- 忠实度：`{binding['fidelity']}`\n"
            + "- 机器证据：`paper-binding.json`；包含页级声明、实现哈希、假设与偏差。\n",
            encoding="utf-8",
        )
    print(f"exported {len(manifests)} strategy packages to {output}")
    return 0


def _run_strategy_directory(
    strategy_dir: Path,
    output: Path,
    *,
    require_strict: bool,
    engine_id: EngineId,
) -> int:
    package = load_strategy_manifest(strategy_dir)
    report = _run_package(
        package,
        output,
        require_strict=require_strict,
        engine_id=engine_id,
    )
    print(report.model_dump_json(indent=2))
    return 0


def _persist_cli_run_failure(
    args: argparse.Namespace, error: ContractError, *, started_at: datetime
) -> None:
    """Persist the same failure envelope for every ordinary `psrc run` failure path."""
    if args.command != "run" and not (
        args.command == "author" and args.author_command == "run"
    ):
        return
    strategy: StrategyManifest | None = None
    dataset: DatasetManifest | None = None
    engine: EngineCapabilities | None = None
    actual_input: dict[str, Any] = {
        "strategy_dir": str(args.strategy_dir),
        "requested_engine": str(args.engine),
        "strict_required": bool(args.require_strict),
    }
    if args.command == "author":
        actual_input.update(
            {
                "paper_source": str(args.source),
                "paper_spec": str(args.spec),
            }
        )
    try:
        strategy = load_strategy_manifest(args.strategy_dir).manifest
    except Exception as exc:
        actual_input["strategy_context_error"] = f"{type(exc).__name__}: {exc}"
    try:
        dataset = DatasetManifest.model_validate_json(
            (args.strategy_dir / "dataset-manifest.json").read_text(encoding="utf-8")
        )
    except Exception as exc:
        actual_input["dataset_context_error"] = f"{type(exc).__name__}: {exc}"
    try:
        payload = json.loads((args.strategy_dir / "input-events.json").read_text(encoding="utf-8"))
        events = tuple(MarketEvent.model_validate(item) for item in payload)
        actual_input.update(
            {
                "event_count": len(events),
                "event_kinds": sorted({str(item.payload.kind) for item in events}),
                "event_symbols": sorted({item.instrument_id for item in events}),
                "event_stream_sha256": event_stream_sha256(events),
            }
        )
    except Exception as exc:
        actual_input["event_context_error"] = f"{type(exc).__name__}: {exc}"
    try:
        engine = resolve_adapter(args.engine, strict_container=False).capabilities
    except Exception as exc:
        actual_input["engine_context_error"] = f"{type(exc).__name__}: {exc}"
    requested_policy = RunPolicy(
        required_sandbox=(
            SandboxMode.STRICT_CONTAINER if args.require_strict else SandboxMode.DEVELOPMENT
        )
    )
    write_failure_bundle(
        FailureReport(
            run_id=error.run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            error=error,
            strategy_manifest=strategy,
            dataset_manifest=dataset,
            engine_capabilities=engine,
            run_policy=requested_policy,
            actual_input=actual_input,
        ),
        args.output,
    )


def _run_strategy_in_docker(strategy_dir: Path, output: Path, *, engine_id: EngineId) -> int:
    """Launch exactly one package in the attested strict-container boundary."""
    package = load_strategy_manifest(strategy_dir)
    DockerSandbox.require_available(
        run_id=f"sandbox.{package.manifest.strategy_id}",
        strategy_id=package.manifest.strategy_id,
    )
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="psrc-sandbox-artifacts-") as artifacts:
        result = DockerSandbox.execute(
            run_id=f"sandbox.{package.manifest.strategy_id}",
            strategy_id=package.manifest.strategy_id,
            policy=package.manifest.resources,
            mounts=ContainerMounts(
                data=package.root,
                artifacts=Path(artifacts),
                reports=output,
            ),
            command=(
                "run",
                "--strategy-dir",
                "/psrc/data",
                "--output",
                "/psrc/reports",
                "--require-strict",
                "--engine",
                engine_id,
            ),
        )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    print(f"strict-container strategy run completed; evidence: {output}")
    return 0


def _demo_compatibility(output: Path) -> int:
    from psrc.examples.sma_cross import SmaCrossStrategy

    events = minute_bars()
    strategy = SmaCrossStrategy()
    requirement = strategy.manifest.data_requirements[0].model_copy(
        update={
            "timeframe": Timeframe(mode=TimeframeMode.BAR, interval="PT5M"),
            "symbols": ("SYNTH.MAPPED",),
            "lookback": 2,
        }
    )
    strategy.manifest = strategy.manifest.model_copy(update={"data_requirements": (requirement,)})
    dataset = minute_bar_manifest(events)
    sandbox = (
        SandboxMode.STRICT_CONTAINER
        if DockerSandbox.current_process_attested()
        else SandboxMode.DEVELOPMENT
    )
    engine = reference_capabilities(strict_container=sandbox == SandboxMode.STRICT_CONTAINER)
    policy = RunPolicy(
        required_sandbox=sandbox,
        allowed_transformations=frozenset({"bar.resample.v1", "symbol.map.v1"}),
        transformation_parameters={"symbol.map.v1": {"mapping": {"SYNTH.TEST": "SYNTH.MAPPED"}}},
        allow_lossy=True,
    )
    plan = compile_run(
        run_id="evidence.compatibility-resample",
        strategy=strategy.manifest,
        dataset=dataset,
        engine=engine,
        policy=policy,
    )
    report = run_rule(
        plan=plan,
        strategy=strategy,
        events=events,
        engine=ReferenceEngine(declared_capabilities=engine),
        sandbox_mode=sandbox,
    )
    write_run_bundle(report, output)
    write_input_evidence(
        output,
        report=report,
        strategy=strategy.manifest,
        dataset=dataset,
        engine=engine,
        policy=policy,
        events=events,
    )
    print(
        f"compatibility transform verified: {len(events)} source events -> "
        f"{report.metrics.decisions} effective events"
    )
    return 0


def _demo_failures(output: Path) -> int:
    from psrc.evidence.failures import generate_failure_evidence

    observed = generate_failure_evidence(output)
    print(
        f"captured {len(observed)} required structured failures; summary: {output / 'summary.json'}"
    )
    return 0


def _demo_adapters(output: Path) -> int:
    try:
        from psrc.evidence.adapters import generate_adapter_evidence
    except ImportError as exc:
        raise ContractViolation(
            ContractError(
                run_id="adapter-evidence.dependencies",
                stage=ErrorStage.INITIALIZATION,
                code=ErrorCode.ENGINE_DEPENDENCY_MISSING,
                message="Native adapter extras are not installed",
                details={"install": "uv sync --extra adapters"},
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
    comparison = generate_adapter_evidence(output)
    print(
        f"verified {comparison['native_engine_count']} native engines; "
        f"comparison: {output / 'comparison.json'}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="psrc")
    parser.add_argument("--version", action="version", version=CONTRACT_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    paper = commands.add_parser("paper", help="ingest real papers and compile grounded packages")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)
    ingest_cmd = paper_commands.add_parser("ingest")
    ingest_cmd.add_argument("--source", type=Path, required=True)
    ingest_cmd.add_argument("--output", type=Path, required=True)
    reproduce_cmd = paper_commands.add_parser("reproduce")
    reproduce_cmd.add_argument("--source", type=Path, required=True)
    reproduce_cmd.add_argument("--recipe", type=Path, required=True)
    reproduce_cmd.add_argument("--output", type=Path, required=True)
    reproduce_cmd.add_argument("--require-strict", action="store_true")
    suite_cmd = paper_commands.add_parser("suite")
    suite_cmd.add_argument("--sources", type=Path, default=Path("papers/sources"))
    suite_cmd.add_argument("--recipes", type=Path, default=Path("papers/recipes"))
    suite_cmd.add_argument("--output", type=Path, required=True)
    suite_cmd.add_argument("--require-strict", action="store_true")

    schema = commands.add_parser("schema", help="work with contract schemas")
    schema_commands = schema.add_subparsers(dest="schema_command", required=True)
    export = schema_commands.add_parser("export", help="export versioned JSON Schemas")
    export.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate", help="validate a contract document")
    validate.add_argument("model", choices=sorted(SCHEMA_MODELS))
    validate.add_argument("input", type=Path)

    package = commands.add_parser("package", help="work with strategy packages")
    package_commands = package.add_subparsers(dest="package_command", required=True)
    package_export = package_commands.add_parser(
        "export", help="export all catalog strategies as independent packages"
    )
    package_export.add_argument("--output", type=Path, default=Path("strategies"))

    run = commands.add_parser("run", help="run a validated strategy package directory")
    run.add_argument("--strategy-dir", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--require-strict", action="store_true")
    run.add_argument("--engine", choices=ENGINE_IDS, default="reference")

    sandbox = commands.add_parser("sandbox", help="run strategy packages in Docker isolation")
    sandbox_commands = sandbox.add_subparsers(dest="sandbox_command", required=True)
    sandbox_run = sandbox_commands.add_parser(
        "run", help="run one package in an attested strict container"
    )
    sandbox_run.add_argument("--strategy-dir", type=Path, required=True)
    sandbox_run.add_argument("--output", type=Path, required=True)
    sandbox_run.add_argument("--engine", choices=ENGINE_IDS, default="reference")

    author = commands.add_parser("author", help="audit paper-derived strategy metadata")
    author_commands = author.add_subparsers(dest="author_command", required=True)
    author_audit = author_commands.add_parser(
        "audit", help="deterministically audit a paper spec against a manifest"
    )
    author_audit.add_argument("--spec", type=Path, required=True)
    author_audit.add_argument("--manifest", type=Path, required=True)
    author_audit.add_argument("--output", type=Path, required=True)
    author_run = author_commands.add_parser(
        "run", help="audit and run an unseen externally implemented paper strategy"
    )
    author_run.add_argument("--spec", type=Path, required=True)
    author_run.add_argument(
        "--source", type=Path, required=True, help="PDF, HTML or UTF-8 paper source"
    )
    author_run.add_argument("--strategy-dir", type=Path, required=True)
    author_run.add_argument("--output", type=Path, required=True)
    author_run.add_argument("--require-strict", action="store_true")
    author_run.add_argument("--engine", choices=ENGINE_IDS, default="reference")

    verify = commands.add_parser("verify", help="verify generated acceptance evidence")
    verify.add_argument("--matrix", type=Path, required=True)
    verify.add_argument(
        "--output", type=Path, default=Path("reports/generated/acceptance-report.json")
    )
    verify.add_argument("--evidence-root", type=Path, default=Path("reports/generated"))
    verify.add_argument(
        "--require-strict",
        action="store_true",
        help="require dynamic strict-container attestation for generated evidence",
    )

    demo = commands.add_parser("demo", help="run bundled vertical-slice examples")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    sma = demo_commands.add_parser("sma", help="run SMA cross on synthetic minute bars")
    sma.add_argument("--output", type=Path, default=Path("runs/demo.sma-cross"))
    all_demo = demo_commands.add_parser("all", help="run all 18 bundled strategies")
    all_demo.add_argument("--output", type=Path, default=Path("runs/all"))
    all_demo.add_argument("--strategies-root", type=Path, default=Path("strategies"))
    all_demo.add_argument("--require-strict", action="store_true")
    failures = demo_commands.add_parser(
        "failures", help="run the eight mandatory structured-failure scenarios"
    )
    failures.add_argument("--output", type=Path, default=Path("runs/failures"))
    adapters = demo_commands.add_parser(
        "adapters", help="run the cross-engine native conformance comparison"
    )
    adapters.add_argument("--output", type=Path, default=Path("runs/adapters"))
    compatibility = demo_commands.add_parser(
        "compatibility", help="run the audited bar-resampling compatibility path"
    )
    compatibility.add_argument("--output", type=Path, default=Path("runs/compatibility"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = datetime.now(UTC)
    try:
        if args.command == "paper":
            from psrc.papers.compiler import write_json
            from psrc.papers.ingest import ingest
            from psrc.papers.pipeline import reproduce, run_suite

            if args.paper_command == "ingest":
                write_json(args.output, ingest(args.source).model_dump(mode="json"))
            elif args.paper_command == "reproduce":
                reproduce(args.source, args.recipe, args.output, require_strict=args.require_strict)
            else:
                run_suite(
                    args.sources, args.recipes, args.output, require_strict=args.require_strict
                )
            print(f"paper pipeline completed; evidence: {args.output}")
            return 0
        if args.command == "schema" and args.schema_command == "export":
            return _schema_export(args.output)
        if args.command == "validate":
            return _validate(args.model, args.input)
        if args.command == "package" and args.package_command == "export":
            return _package_export(args.output)
        if args.command == "run":
            return _run_strategy_directory(
                args.strategy_dir,
                args.output,
                require_strict=args.require_strict,
                engine_id=args.engine,
            )
        if args.command == "sandbox" and args.sandbox_command == "run":
            return _run_strategy_in_docker(
                args.strategy_dir,
                args.output,
                engine_id=args.engine,
            )
        if args.command == "author":
            if args.author_command == "audit":
                return _author_audit(args.spec, args.manifest, args.output)
            return _author_run(
                args.spec,
                args.source,
                args.strategy_dir,
                args.output,
                require_strict=args.require_strict,
                engine_id=args.engine,
            )
        if args.command == "verify":
            return _acceptance_verify(
                args.matrix,
                args.output,
                args.evidence_root,
                require_strict=args.require_strict,
            )
        if args.command == "demo" and args.demo_command == "sma":
            return _demo_sma(args.output)
        if args.command == "demo" and args.demo_command == "all":
            return _demo_all(args.output, args.strategies_root, require_strict=args.require_strict)
        if args.command == "demo" and args.demo_command == "failures":
            return _demo_failures(args.output)
        if args.command == "demo" and args.demo_command == "adapters":
            return _demo_adapters(args.output)
        if args.command == "demo" and args.demo_command == "compatibility":
            return _demo_compatibility(args.output)
    except ContractViolation as exc:
        _persist_cli_run_failure(args, exc.error, started_at=started_at)
        print(exc.error.model_dump_json(indent=2))
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
