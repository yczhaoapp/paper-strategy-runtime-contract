from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError

from psrc.cli import main
from psrc.runtime.report import FailureReport, RunBundle
from psrc.sandbox.container import DockerSandbox


def test_cli_builds_complete_reproduction_bundle(tmp_path: Path, capsys: object) -> None:
    del capsys
    repository = Path(__file__).parents[2]
    schemas = tmp_path / "schemas"
    packages = tmp_path / "packages"
    evidence = tmp_path / "evidence"

    assert main(["schema", "export", "--output", str(schemas)]) == 0
    assert len(json.loads((schemas / "catalog.json").read_text())["schemas"]) >= 20
    assert main(["package", "export", "--output", str(packages)]) == 0
    assert len(list(packages.glob("*/strategy.yaml"))) == 18
    assert len(list(packages.glob("*/strategy.py"))) == 18
    assert len(list(packages.glob("*/input-events.json"))) == 18

    sample_manifest = packages / "rule.sma_cross/strategy.yaml"
    assert main(["validate", "strategy-manifest", str(sample_manifest)]) == 0
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("strategy_id: broken\n", encoding="utf-8")
    assert main(["validate", "strategy-manifest", str(invalid)]) == 2

    assert main(["demo", "sma", "--output", str(evidence / "runs/sma")]) == 0
    bundle_payload = json.loads((evidence / "runs/sma/bundle.json").read_text(encoding="utf-8"))
    RunBundle.model_validate(bundle_payload)
    tampered_input = json.loads((evidence / "runs/sma/bundle.json").read_text(encoding="utf-8"))
    tampered_input["input_evidence"]["source_events"][0]["payload"]["close"] = "999"
    with pytest.raises(ValidationError):
        RunBundle.model_validate(tampered_input)
    tampered_runtime = json.loads((evidence / "runs/sma/bundle.json").read_text())
    tampered_runtime["runtime_capabilities"]["runtime_version"] = "tampered"
    with pytest.raises(ValidationError):
        RunBundle.model_validate(tampered_runtime)
    bundle_payload["report"]["execution_plan"]["strategy_id"] = "rule.mismatched"
    with pytest.raises(ValidationError):
        RunBundle.model_validate(bundle_payload)
    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "rule.sma_cross"),
                "--output",
                str(evidence / "runs/single-package"),
            ]
        )
        == 0
    )
    package_bundle_path = evidence / "runs/single-package/bundle.json"
    package_bundle = json.loads(package_bundle_path.read_text(encoding="utf-8"))
    assert package_bundle["strategy_code_evidence"]["package_files"]
    assert package_bundle["report"]["execution_plan"]["strategy_code_evidence_sha256"]
    package_bundle["strategy_code_evidence"]["package_files"][0]["sha256"] = "f" * 64
    with pytest.raises(ValidationError):
        RunBundle.model_validate(package_bundle)
    assert (
        main(
            [
                "demo",
                "all",
                "--output",
                str(evidence / "runs/all"),
                "--strategies-root",
                str(packages),
            ]
        )
        == 0
    )
    assert main(["demo", "failures", "--output", str(evidence / "runs/failures")]) == 0
    assert main(["demo", "adapters", "--output", str(evidence / "runs/adapters")]) == 0
    assert main(["demo", "compatibility", "--output", str(evidence / "runs/compatibility")]) == 0

    assert main(["paper", "suite", "--output", str(evidence / "runs/papers")]) == 0

    from psrc.papers.catalog import read_bindings

    suites = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        suites,
        "testsuite",
        tests="100",
        failures="0",
        errors="0",
        skipped="0",
    )
    for binding in read_bindings(repository / "papers/bindings"):
        for node_id in binding.verification_tests:
            path, name = node_id.split("::", maxsplit=1)
            ElementTree.SubElement(
                suite,
                "testcase",
                classname=path.removesuffix(".py").replace("/", "."),
                name=name,
            )
    for classname, name in (
        (
            "tests.integration.test_public_interface_coverage",
            "test_trade_tick_account_and_order_status_are_executable",
        ),
        (
            "tests.integration.test_public_interface_coverage",
            "test_catalog_declares_all_strategy_shapes_and_required_time_granularities",
        ),
        (
            "tests.negative.test_reference_engine_failures",
            "test_direct_orders_cannot_accumulate_beyond_position_limit",
        ),
        (
            "tests.negative.test_reference_engine_failures",
            "test_day_order_expires_before_a_later_utc_session_can_fill",
        ),
        (
            "tests.negative.test_runtime_contract_enforcement",
            "test_actual_bar_timestamps_must_match_declared_epoch_grid",
        ),
        (
            "tests.e2e.test_cli",
            "test_invalid_contract_version_uses_structured_persisted_failure",
        ),
        (
            "tests.e2e.test_cli",
            "test_reused_output_contains_only_the_latest_failed_run",
        ),
        (
            "tests.unit.test_compiler",
            "test_insufficient_declared_lookback_fails_before_execution",
        ),
        (
            "tests.unit.test_compiler",
            "test_runtime_training_profile_is_not_required_from_engine",
        ),
        (
            "tests.unit.test_compiler",
            "test_missing_runtime_training_profile_is_structured",
        ),
        (
            "tests.unit.test_compiler",
            "test_runtime_cannot_claim_an_engine_owned_profile",
        ),
        (
            "tests.adapters.test_trainable_lifecycle",
            "test_backtrader_executes_train_save_reload_infer_backtest_lifecycle["
            "supervised-logistic]",
        ),
        (
            "tests.adapters.test_trainable_lifecycle",
            "test_backtrader_executes_train_save_reload_infer_backtest_lifecycle["
            "rl-tabular-q]",
        ),
        (
            "tests.sandbox.test_policy",
            "test_static_scanner_resolves_aliases_and_rejects_runtime_namespace_escape",
        ),
        (
            "tests.sandbox.test_policy",
            "test_static_scanner_rejects_private_dependency_escape_and_strategy_api_children",
        ),
        (
            "tests.sandbox.test_policy",
            "test_runtime_audit_blocks_file_process_and_report_mount_access",
        ),
        (
            "tests.sandbox.test_policy",
            "test_artifact_store_is_the_only_writable_strategy_channel",
        ),
        (
            "tests.sandbox.test_policy",
            "test_manifest_descriptor_executes_inside_resource_guard",
        ),
        (
            "tests.sandbox.test_policy",
            "test_guarded_action_return_is_canonicalized_before_leaving_policy_scope",
        ),
        (
            "tests.negative.test_training_failures",
            "test_runtime_independently_verifies_returned_training_artifact[missing]",
        ),
        (
            "tests.negative.test_training_failures",
            "test_runtime_independently_verifies_returned_training_artifact[size]",
        ),
        (
            "tests.unit.test_artifacts",
            "test_artifact_size_and_authorized_root_cannot_be_forged",
        ),
        (
            "tests.unit.test_artifacts",
            "test_strategy_artifact_channel_rejects_executable_scalar_subclasses",
        ),
        (
            "tests.unit.test_artifacts",
            "test_strategy_channel_class_root_does_not_change_host_authority",
        ),
        (
            "tests.negative.test_training_failures",
            "test_strategy_channel_class_mutation_cannot_bypass_host_manifest_verification",
        ),
        (
            "tests.negative.test_training_failures",
            "test_noop_load_cannot_be_reported_as_verified_artifact_reload",
        ),
        (
            "tests.negative.test_training_failures",
            "test_guarded_training_return_is_canonicalized_before_trusted_artifact_io",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_public_adapter_wraps_unexpected_strategy_exceptions["
            "on_start-ReferenceEngine-capabilities]",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_public_adapter_wraps_unexpected_strategy_exceptions["
            "on_start-BacktraderAdapter-capabilities]",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_target_batches_and_derived_native_orders_enforce_limits["
            "duplicate-target-ReferenceEngine-capabilities]",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_target_batches_and_derived_native_orders_enforce_limits["
            "derived-order-limit-BacktraderAdapter-capabilities]",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_public_adapter_checks_concrete_payload_fields["
            "ReferenceEngine-capabilities]",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_public_adapter_checks_concrete_payload_fields["
            "BacktraderAdapter-capabilities]",
        ),
        (
            "tests.adapters.test_public_boundary_failures",
            "test_public_adapter_checks_concrete_l2_depth",
        ),
        (
            "tests.unit.test_compatibility",
            "test_resample_uses_market_time_for_ohlc_when_a_bar_arrives_late",
        ),
        (
            "tests.unit.test_compatibility",
            "test_resample_rejects_ambiguous_duplicate_market_timestamps",
        ),
        (
            "tests.negative.test_runtime_contract_enforcement",
            "test_orchestrator_rejects_strategy_identity_mismatch",
        ),
        (
            "tests.negative.test_runtime_contract_enforcement",
            "test_orchestrator_rejects_engine_capability_mismatch",
        ),
        (
            "tests.negative.test_runtime_contract_enforcement",
            "test_orchestrator_rejects_runtime_capability_mismatch",
        ),
        (
            "tests.negative.test_runtime_contract_enforcement",
            "test_orchestrator_rejects_sandbox_downgrade",
        ),
            (
                "tests.negative.test_runtime_contract_enforcement",
                "test_orchestrator_enforces_actual_event_staleness",
            ),
            (
                "tests.negative.test_runtime_contract_enforcement",
                "test_adapter_public_run_rejects_strategy_identity_mismatch",
            ),
            (
                "tests.negative.test_training_failures",
                "test_orchestrator_rejects_training_request_not_bound_to_plan",
            ),
            (
                "tests.negative.test_training_failures",
                "test_run_bundle_rejects_tampered_artifact_training_request_hash",
            ),
            *(
            (
                "tests.integration.test_strategy_packages",
                "test_unregistered_authoring_fixture_and_external_code_use_formal_path["
                f"{kind}]",
            )
            for kind in ("rule", "supervised", "reinforcement_learning")
        ),
    ):
        ElementTree.SubElement(
            suite,
            "testcase",
            classname=classname,
            name=name,
        )
    ElementTree.ElementTree(suites).write(
        evidence / "junit.xml", encoding="utf-8", xml_declaration=True
    )
    (evidence / "coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": 90}}), encoding="utf-8"
    )
    acceptance_report = evidence / "acceptance-report.json"
    assert (
        main(
            [
                "verify",
                "--matrix",
                str(repository / "ACCEPTANCE_MATRIX.yaml"),
                "--evidence-root",
                str(evidence),
                "--output",
                str(acceptance_report),
            ]
        )
        == 0
    )
    assert json.loads(acceptance_report.read_text(encoding="utf-8"))["status"] == "passed"


def test_verification_cli_fails_closed_when_evidence_is_absent(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    output = tmp_path / "failed-verification.json"
    assert (
        main(
            [
                "verify",
                "--matrix",
                str(repository / "ACCEPTANCE_MATRIX.yaml"),
                "--evidence-root",
                str(tmp_path / "absent"),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["verifier_policy"].startswith("generated evidence only")


def test_package_cli_require_strict_never_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "output"
    assert main(["package", "export", "--output", str(packages)]) == 0
    monkeypatch.setattr(DockerSandbox, "current_process_attested", classmethod(lambda cls: False))
    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "rule.sma_cross"),
                "--output",
                str(output),
                "--require-strict",
            ]
        )
        == 3
    )
    failure = FailureReport.model_validate_json((output / "report.json").read_text())
    assert failure.error.code == "SANDBOX_UNAVAILABLE"
    assert failure.actual_input["strict_required"] is True


def test_package_cli_selects_each_executable_engine(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    assert main(["package", "export", "--output", str(packages)]) == 0

    for engine_id in ("reference", "backtrader"):
        output = tmp_path / f"run-{engine_id}"
        assert (
            main(
                [
                    "run",
                    "--strategy-dir",
                    str(packages / "rule.sma_cross"),
                    "--output",
                    str(output),
                    "--engine",
                    engine_id,
                ]
            )
            == 0
        )
        bundle = RunBundle.model_validate_json((output / "bundle.json").read_text(encoding="utf-8"))
        assert bundle.report.execution_plan.engine_id == engine_id
        assert bundle.engine_capabilities.engine_id == engine_id
        assert bundle.report.metrics.fills > 0


def test_selected_engine_capability_failure_never_substitutes_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "unsupported-run"
    assert main(["package", "export", "--output", str(packages)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "rule.l1_microprice"),
                "--output",
                str(output),
                "--engine",
                "backtrader",
            ]
        )
        == 3
    )
    error = json.loads(capsys.readouterr().out)
    assert error["code"] == "ENGINE_CAPABILITY_UNSUPPORTED"
    assert error["engine_id"] == "backtrader"
    failure = FailureReport.model_validate_json((output / "report.json").read_text())
    assert failure.error.code == "ENGINE_CAPABILITY_UNSUPPORTED"
    assert failure.engine_capabilities is not None
    assert failure.engine_capabilities.engine_id == "backtrader"


def test_invalid_contract_version_uses_structured_persisted_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "invalid-version"
    assert main(["package", "export", "--output", str(packages)]) == 0
    capsys.readouterr()
    manifest = packages / "rule.sma_cross/strategy.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "contract_version: 1.4.0", "contract_version: invalid", 1
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "rule.sma_cross"),
                "--output",
                str(output),
            ]
        )
        == 3
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["code"] == "MANIFEST_INVALID"
    failure = FailureReport.model_validate_json((output / "report.json").read_text())
    assert failure.error.code == "MANIFEST_INVALID"


def test_reused_output_contains_only_the_latest_failed_run(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "reused-output"
    assert main(["package", "export", "--output", str(packages)]) == 0
    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "rule.sma_cross"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert (output / "bundle.json").is_file()

    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "rule.l1_microprice"),
                "--output",
                str(output),
                "--engine",
                "backtrader",
            ]
        )
        == 3
    )
    failure = FailureReport.model_validate_json((output / "report.json").read_text())
    assert failure.error.code == "ENGINE_CAPABILITY_UNSUPPORTED"
    for stale in (
        "bundle.json",
        "execution-plan.json",
        "decisions.json",
        "orders.json",
        "fills.json",
        "account-snapshots.json",
        "artifacts.json",
        "logs.json",
    ):
        assert not (output / stale).exists()
