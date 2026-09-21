from __future__ import annotations

import json
import re
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from psrc.adapters.profiles import discover_engine_profiles
from psrc.constants import JSON_SCHEMA_DIALECT, SCHEMA_BASE_URI
from psrc.contract.models import (
    StrategyCodeEvidence,
    StrategyKind,
    SupportLevel,
    TrainingMode,
)
from psrc.runtime.package import discover_strategy_packages
from psrc.runtime.report import RunBundle
from psrc.sandbox.container import DockerSandbox

REQUIRED_ERRORS = {
    "DATA_FIELD_MISSING",
    "DATA_TIMEFRAME_MISMATCH",
    "SYMBOL_MAPPING_FAILED",
    "ARTIFACT_NOT_FOUND",
    "ACTION_INVALID",
    "ORDER_REJECTED",
    "TRAINING_FAILED",
    "BACKTEST_FAILED",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_references(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        own = (value["$ref"],) if isinstance(value.get("$ref"), str) else ()
        return own + tuple(
            reference for item in value.values() for reference in _schema_references(item)
        )
    if isinstance(value, list):
        return tuple(reference for item in value for reference in _schema_references(item))
    return ()


def verify_acceptance(
    matrix_path: Path, evidence_root: Path, *, require_strict: bool = False
) -> dict[str, Any]:
    """Verify generated evidence without treating matrix prose as a result."""
    repository = matrix_path.resolve().parent
    matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, passed: bool, detail: object) -> None:
        checks[name] = {"passed": passed, "detail": detail}

    required_files = (
        "README.md",
        "CHANGELOG.md",
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "spec/contract-v1.md",
        "spec/lifecycle.md",
        "spec/runtime-interfaces.md",
        "spec/package.md",
        "spec/errors.md",
        "spec/compatibility.md",
        "spec/security.md",
        "spec/authoring-agent.md",
        "spec/versioning.md",
        "docs/reproduction.md",
        "docs/audit-remediation.md",
        "docs/acceptance-audit.md",
        "docs/interpretation-register.md",
        "docs/public-interface-coverage.md",
        "scripts/verify-container.sh",
        "spec/data-provenance.md",
        "papers/source-registry.json",
        "data/public/source.json",
    )
    missing_files = [name for name in required_files if not (repository / name).is_file()]
    check("repository_delivery", not missing_files, {"missing": missing_files})

    packages = discover_strategy_packages(repository / "strategies")
    counts = Counter(str(package.manifest.kind) for package in packages)
    fingerprints = {
        json.dumps(
            {
                "kind": package.manifest.kind,
                "data": [
                    item.model_dump(mode="json") for item in package.manifest.data_requirements
                ],
                "actions": package.manifest.action_requirements.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        for package in packages
    }
    expected_counts = {kind.value: 6 for kind in StrategyKind}
    incomplete_packages = [
        package.manifest.strategy_id
        for package in packages
        if not all(
            (package.root / filename).is_file()
            for filename in (
                "strategy.py",
                "dataset-manifest.json",
                "input-events.json",
                "paper-binding.json",
            )
        )
        or (
            package.manifest.lifecycle.training == TrainingMode.REQUIRED
            and not all(
                (package.root / filename).is_file()
                for filename in (
                    "training-request.json",
                    "training-input-evidence.json",
                )
            )
        )
        or package.manifest.entrypoint != "strategy.py:Strategy"
    ]
    check(
        "strategy_packages_6x3",
        len(packages) == 18
        and dict(counts) == expected_counts
        and len(fingerprints) >= 12
        and not incomplete_packages,
        {
            "total": len(packages),
            "counts": dict(counts),
            "materially_distinct_contract_shapes": len(fingerprints),
            "incomplete_executable_packages": incomplete_packages,
        },
    )

    review_documents = (
        "README.md",
        "docs/adapter-guide.md",
        "docs/architecture.md",
        "docs/reproduction.md",
        "docs/audit-remediation.md",
        "docs/strategy-matrix.md",
        "docs/acceptance-audit.md",
        "docs/interpretation-register.md",
        "docs/public-interface-coverage.md",
        "spec/contract-v1.md",
        "spec/lifecycle.md",
        "spec/runtime-interfaces.md",
        "spec/package.md",
        "spec/errors.md",
        "spec/compatibility.md",
        "spec/security.md",
        "spec/versioning.md",
        "spec/authoring-agent.md",
        "spec/data-provenance.md",
    )

    def has_chinese(path: Path) -> bool:
        return (
            path.is_file()
            and re.search(r"[\u3400-\u9fff]", path.read_text(encoding="utf-8")) is not None
        )

    untranslated_documents = [
        name for name in review_documents if not has_chinese(repository / name)
    ]
    untranslated_cards = [
        package.manifest.strategy_id
        for package in packages
        if not has_chinese(package.root / "STRATEGY_CARD.md")
    ]
    check(
        "chinese_review_materials",
        not untranslated_documents and not untranslated_cards and len(packages) == 18,
        {
            "review_documents": len(review_documents),
            "strategy_cards": len(packages) - len(untranslated_cards),
            "untranslated_documents": untranslated_documents,
            "untranslated_strategy_cards": untranslated_cards,
            "machine_contract_language": "stable English identifiers",
        },
    )

    schema_catalog_path = repository / "schemas/generated/catalog.json"
    schema_catalog = _json(schema_catalog_path) if schema_catalog_path.is_file() else {}
    schema_entries = schema_catalog.get("schemas", {})
    schema_issues: list[str] = []
    schema_ids: set[str] = set()
    external_references: dict[str, list[str]] = {}
    for name, filename in schema_entries.items():
        schema_path = repository / "schemas/generated" / filename
        if not schema_path.is_file():
            schema_issues.append(f"{name}: missing {filename}")
            continue
        schema = _json(schema_path)
        expected_id = f"{SCHEMA_BASE_URI}/{filename}?contract={matrix.get('contract_version')}"
        if schema.get("$id") != expected_id:
            schema_issues.append(f"{name}: unexpected $id")
        if schema.get("$schema") != JSON_SCHEMA_DIALECT:
            schema_issues.append(f"{name}: unexpected or missing $schema")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            schema_issues.append(f"{name}: invalid Draft 2020-12 schema: {exc.message}")
        non_local = sorted(
            reference
            for reference in set(_schema_references(schema))
            if not reference.startswith("#/$defs/")
        )
        if non_local:
            external_references[name] = non_local
        schema_ids.add(str(schema.get("$id")))
    strategy_schema_path = repository / "schemas/generated/strategy-manifest.schema.json"
    strategy_schema_text = (
        strategy_schema_path.read_text(encoding="utf-8") if strategy_schema_path.is_file() else ""
    )
    extension_namespace_enforced = "patternProperties" in strategy_schema_text
    check(
        "versioned_public_schemas",
        len(schema_entries) >= 22
        and not schema_issues
        and not external_references
        and len(schema_ids) == len(schema_entries)
        and schema_catalog.get("contract_version") == matrix.get("contract_version")
        and extension_namespace_enforced,
        {
            "schema_count": len(schema_entries),
            "issues": schema_issues,
            "external_references": external_references,
            "unique_ids": len(schema_ids),
            "dialect": JSON_SCHEMA_DIALECT,
            "standalone_references": not external_references,
            "reverse_dns_extensions_enforced": extension_namespace_enforced,
        },
    )

    all_runs_path = evidence_root / "runs/all/summary.json"
    all_runs = _json(all_runs_path) if all_runs_path.is_file() else {}
    successful_bundles = 0
    artifact_runs = 0
    complete_bundles = 0
    validated_bundles = 0
    verified_artifact_files = 0
    stage_complete_bundles = 0
    source_attested_bundles = 0
    runs_with_fills = 0
    observed_sandbox_modes: set[str] = set()
    for package in packages:
        bundle = evidence_root / "runs/all" / package.manifest.strategy_id
        report_path = bundle / "report.json"
        if not report_path.is_file():
            continue
        report = _json(report_path)
        successful_bundles += int(report.get("status") == "succeeded")
        artifact_runs += int(bool(report.get("artifacts")))
        runs_with_fills += int(report.get("metrics", {}).get("fills", 0) > 0)
        if isinstance(report.get("sandbox_mode"), str):
            observed_sandbox_modes.add(report["sandbox_mode"])
        complete = all(
            (bundle / filename).is_file()
            for filename in (
                "report.json",
                "bundle.json",
                "report.html",
                "strategy-manifest.json",
                "dataset-manifest.json",
                "engine-capabilities.json",
                "run-policy.json",
                "execution-plan.json",
                "decisions.json",
                "orders.json",
                "fills.json",
                "account-snapshots.json",
                "artifacts.json",
                "logs.json",
                "input-evidence.json",
                "strategy-code-evidence.json",
                "source-events.json",
                "effective-events.json",
            )
        ) and (
            package.manifest.lifecycle.training != TrainingMode.REQUIRED
            or (bundle / "training-input-evidence.json").is_file()
        )
        complete_bundles += int(complete)
        if not complete:
            continue
        try:
            validated = RunBundle.model_validate(_json(bundle / "bundle.json"))
            standalone_code_evidence = StrategyCodeEvidence.model_validate(
                _json(bundle / "strategy-code-evidence.json")
            )
        except Exception:
            continue
        if (
            validated.strategy_manifest != package.manifest
            or validated.strategy_code_evidence is None
            or validated.strategy_code_evidence != package.code_evidence
            or validated.strategy_code_evidence != standalone_code_evidence
            or not validated.report.run_id.startswith("package.")
        ):
            continue
        validated_bundles += 1
        source_attested_bundles += int(
            validated.report.execution_plan.strategy_code_evidence_sha256 is not None
        )
        stages = {record.stage for record in validated.report.logs}
        required_stages = {
            "initialization",
            "compatibility",
            "inference",
            "backtest",
            "finalization",
        }
        if validated.training_request is not None:
            required_stages |= {"training", "artifact"}
        stage_complete_bundles += int(required_stages <= stages)
        for artifact in validated.report.artifacts:
            for declared in artifact.files:
                artifact_path = (
                    bundle / "artifact-store" / artifact.artifact_id / declared.logical_name
                )
                if (
                    artifact_path.is_file()
                    and sha256(artifact_path.read_bytes()).hexdigest() == declared.sha256
                    and artifact.artifact_id == f"sha256-{declared.sha256}"
                ):
                    verified_artifact_files += 1
    check(
        "unified_train_infer_backtest_reports",
        all_runs.get("strategy_count") == 18
        and successful_bundles == 18
        and artifact_runs == 12
        and complete_bundles == 18
        and validated_bundles == 18
        and source_attested_bundles == 18
        and verified_artifact_files == 12
        and stage_complete_bundles == 18
        and runs_with_fills == 18,
        {
            "successful_runs": successful_bundles,
            "trainable_artifact_runs": artifact_runs,
            "complete_bundles": complete_bundles,
            "schema_validated_bundles": validated_bundles,
            "source_attested_bundles": source_attested_bundles,
            "content_addressed_artifact_files": verified_artifact_files,
            "stage_complete_bundles": stage_complete_bundles,
            "runs_with_native_or_reference_fills": runs_with_fills,
        },
    )
    strict_attested = DockerSandbox.current_process_attested()
    expected_sandbox_mode = "strict_container" if strict_attested else "development"
    check(
        "evidence_sandbox_mode_consistency",
        successful_bundles == 18
        and observed_sandbox_modes == {expected_sandbox_mode}
        and (strict_attested or not require_strict),
        {
            "process_strict_container_attested": strict_attested,
            "expected_mode": expected_sandbox_mode,
            "observed_modes": sorted(observed_sandbox_modes),
            "strict_dynamic_evidence": strict_attested,
            "strict_evidence_required": require_strict,
        },
    )

    compatibility_path = evidence_root / "runs/compatibility/bundle.json"
    try:
        compatibility_bundle = RunBundle.model_validate(_json(compatibility_path))
        compatibility_ok = (
            compatibility_bundle.input_evidence.transformation_ids
            == ("symbol.map.v1", "bar.resample.v1")
            and len(compatibility_bundle.input_evidence.source_events) == 10
            and len(compatibility_bundle.input_evidence.effective_events) == 2
            and {
                event.instrument_id
                for event in compatibility_bundle.input_evidence.effective_events
            }
            == {"SYNTH.MAPPED"}
            and compatibility_bundle.report.metrics.decisions == 2
            and any(
                record.stage == "compatibility"
                and record.fields.get("source_event_count") == 10
                and record.fields.get("effective_event_count") == 2
                for record in compatibility_bundle.report.logs
            )
        )
    except Exception:
        compatibility_ok = False
    check(
        "compatibility_transform_executed_and_audited",
        compatibility_ok,
        {
            "bundle": "runs/compatibility/bundle.json",
            "expected_event_reduction": "10 -> 2",
        },
    )

    failure_path = evidence_root / "runs/failures/summary.json"
    failure_summary = _json(failure_path) if failure_path.is_file() else {}
    observed_errors = set(failure_summary.get("observed_error_codes", {}).values())
    check(
        "eight_required_structured_failures",
        failure_summary.get("status") == "passed" and observed_errors == REQUIRED_ERRORS,
        {"observed": sorted(observed_errors), "required": sorted(REQUIRED_ERRORS)},
    )

    from psrc.papers.catalog import verify_strategy_bindings
    from psrc.papers.evidence import verify_papers

    binding_evidence = verify_strategy_bindings(repository, evidence_root=evidence_root)
    check(
        "eighteen_paper_grounded_strategy_bindings",
        binding_evidence["status"] == "passed",
        binding_evidence,
    )

    paper_evidence = verify_papers(
        evidence_root / "runs/papers", repository / "papers/sources", repository / "papers/recipes"
    )
    check("real_paper_to_runtime_evidence", paper_evidence["status"] == "passed", paper_evidence)

    adapter_path = evidence_root / "runs/adapters/comparison.json"
    adapter_comparison = _json(adapter_path) if adapter_path.is_file() else {}
    check(
        "two_native_engine_conformance",
        adapter_comparison.get("status") == "passed"
        and adapter_comparison.get("native_engine_count") == 2
        and adapter_comparison.get("decisions_equal") is True
        and adapter_comparison.get("fill_shapes_equal") is True,
        adapter_comparison,
    )

    profiles = discover_engine_profiles(repository / "engine_profiles")
    verified = {
        profile.engine_id
        for profile in profiles
        if profile.support_level == SupportLevel.CONFORMANCE_VERIFIED
    }
    profiled = {
        profile.engine_id for profile in profiles if profile.support_level == SupportLevel.PROFILED
    }
    check(
        "engine_capability_inventory",
        verified == {"reference", "backtrader"} and profiled == {"lean", "qlib", "vnpy"},
        {"conformance_verified": sorted(verified), "profiled_only": sorted(profiled)},
    )

    dockerfile = (repository / "Dockerfile").read_text(encoding="utf-8")
    sandbox_source = (repository / "src/psrc/sandbox/container.py").read_text(encoding="utf-8")
    package_source = (repository / "src/psrc/runtime/package.py").read_text(encoding="utf-8")
    compiler_source = (repository / "src/psrc/contract/compiler.py").read_text(encoding="utf-8")
    cli_source = (repository / "src/psrc/cli.py").read_text(encoding="utf-8")
    container_gate = (repository / "scripts/verify-container.sh").read_text(encoding="utf-8")
    controls = (
        "--network",
        "--read-only",
        "--cap-drop",
        "no-new-privileges:true",
        "--pids-limit",
        "--memory",
        "--user",
        "--tmpfs",
    )
    missing_controls = [control for control in controls if control not in sandbox_source]
    runtime_attestation_tokens = (
        "_container_marker_present",
        "_runtime_controls_present",
        "CapEff",
        "NoNewPrivs",
        "/proc/mounts",
    )
    missing_attestation_checks = [
        token for token in runtime_attestation_tokens if token not in sandbox_source
    ]
    check(
        "fail_closed_sandbox",
        not missing_controls
        and "USER 65532:65532" in dockerfile
        and "FROM python:3.12-slim@sha256:" in dockerfile
        and "uv sync --frozen" in dockerfile
        and "StaticPolicyScanner.scan" in package_source
        and "SANDBOX_POLICY_DENIED" in package_source
        and "SANDBOX_POLICY_DOWNGRADE" in compiler_source
        and "DockerSandbox.execute(" in cli_source
        and '"/psrc/data"' in cli_source
        and not missing_attestation_checks,
        {
            "missing_controls": missing_controls,
            "non_root_image": "USER 65532:65532" in dockerfile,
            "base_image_digest_pinned": "FROM python:3.12-slim@sha256:" in dockerfile,
            "dependency_lock_enforced": "uv sync --frozen" in dockerfile,
            "package_source_policy_enforced": (
                "StaticPolicyScanner.scan" in package_source
                and "SANDBOX_POLICY_DENIED" in package_source
            ),
            "strategy_minimum_sandbox_enforced": ("SANDBOX_POLICY_DOWNGRADE" in compiler_source),
            "single_package_docker_entrypoint": (
                "DockerSandbox.execute(" in cli_source and '"/psrc/data"' in cli_source
            ),
            "missing_runtime_attestation_checks": missing_attestation_checks,
        },
    )

    authoring_models = (repository / "src/psrc/authoring/models.py").read_text(encoding="utf-8")
    check(
        "executable_agent_audit_boundary",
        "audit_manifest(spec, manifest)" in cli_source
        and 'add_parser("author"' in cli_source
        and "runtime_authority_granted: Literal[False]" in authoring_models,
        {
            "machine_readable_cli": "audit_manifest(spec, manifest)" in cli_source,
            "runtime_authority_fixed_false": (
                "runtime_authority_granted: Literal[False]" in authoring_models
            ),
        },
    )

    full_gate_tokens = (
        "ruff check",
        "mypy src tests",
        "schema export",
        "package export",
        "diff -ru",
        "pytest",
        "demo all",
        "demo failures",
        "demo adapters",
        "demo compatibility",
        "psrc verify",
        "--require-strict",
    )
    missing_gate_steps = [token for token in full_gate_tokens if token not in container_gate]
    check(
        "strict_container_complete_gate",
        not missing_gate_steps and '"$(id -u)" == "0"' in container_gate,
        {
            "missing_steps": missing_gate_steps,
            "root_execution_rejected": '"$(id -u)" == "0"' in container_gate,
            "generated_content_drift_checked": "diff -ru" in container_gate,
        },
    )

    junit_path = evidence_root / "junit.xml"
    test_detail: dict[str, int] = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if junit_path.is_file():
        junit_root = ElementTree.parse(junit_path).getroot()
        suites = (
            [junit_root] if junit_root.tag == "testsuite" else list(junit_root.findall("testsuite"))
        )
        for suite in suites:
            for key in test_detail:
                test_detail[key] += int(suite.attrib.get(key, 0))
        executed_tests = {
            f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}"
            for case in junit_root.iter("testcase")
        }
    else:
        executed_tests = set()
    check(
        "automated_tests_no_failures_or_skips",
        test_detail["tests"] >= 50
        and test_detail["failures"] == 0
        and test_detail["errors"] == 0
        and test_detail["skipped"] == 0,
        test_detail,
    )
    required_interface_tests = {
        "tests.integration.test_public_interface_coverage::"
        "test_trade_tick_account_and_order_status_are_executable",
        "tests.integration.test_public_interface_coverage::"
        "test_catalog_declares_all_strategy_shapes_and_required_time_granularities",
    }
    external_authoring_prefix = (
        "tests.integration.test_strategy_packages::"
        "test_unregistered_paper_spec_and_external_code_use_formal_authoring_path["
    )
    external_authoring_runs = sum(
        node.startswith(external_authoring_prefix) for node in executed_tests
    )
    missing_interface_tests = sorted(required_interface_tests - executed_tests)
    check(
        "public_interface_and_unseen_paper_execution",
        not missing_interface_tests and external_authoring_runs == 3,
        {
            "missing_interface_tests": missing_interface_tests,
            "unseen_paper_strategy_shapes_executed": external_authoring_runs,
            "required_unseen_paper_strategy_shapes": 3,
        },
    )

    coverage_path = evidence_root / "coverage.json"
    coverage = _json(coverage_path).get("totals", {}) if coverage_path.is_file() else {}
    percent_covered = float(coverage.get("percent_covered", 0))
    check(
        "code_coverage",
        percent_covered >= 90,
        {"percent_covered": round(percent_covered, 2), "minimum": 90},
    )

    passed = all(item["passed"] for item in checks.values())
    return {
        "challenge": matrix.get("challenge"),
        "verification_scope": (
            "strict_container_verification" if require_strict else "development_preflight"
        ),
        "contract_version": matrix.get("contract_version"),
        "status": "passed" if passed else "failed",
        "verifier_policy": "generated evidence only; matrix narrative is not a result",
        "checks": checks,
        "passed_checks": sum(int(item["passed"]) for item in checks.values()),
        "total_checks": len(checks),
    }
