from __future__ import annotations

import json
from pathlib import Path

from psrc.authoring.audit import audit_external_implementation, audit_manifest
from psrc.authoring.models import (
    PaperAmbiguity,
    PaperEvidenceReference,
    PaperReference,
    PaperStrategySpec,
)
from psrc.cli import main
from psrc.contract.models import StrategyKind
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.papers.ingest import ingest


def _spec(*, blocking: bool) -> PaperStrategySpec:
    manifest = SmaCrossStrategy.manifest
    return PaperStrategySpec(
        spec_id="paper.sma-demo",
        reference=PaperReference(
            title="Synthetic moving-average demonstration",
            locator="docs://synthetic/sma",
            citation="PSRC synthetic example",
        ),
        strategy_kind=StrategyKind.RULE,
        hypothesis="Short-window strength relative to long-window strength predicts direction.",
        data_requirements=manifest.data_requirements,
        output_actions=manifest.action_requirements.allowed,
        execution_assumptions=("Signals use closed bars only.",),
        ambiguities=(
            PaperAmbiguity(
                ambiguity_id="fill-timing",
                severity="blocking" if blocking else "info",
                statement="The paper does not define fill timing.",
                resolution=None if blocking else "Use next-event execution.",
                resolved_by=None if blocking else "operator",
            ),
        ),
    )


def test_authoring_agent_cannot_hide_blocking_ambiguity() -> None:
    report = audit_manifest(_spec(blocking=True), SmaCrossStrategy.manifest)
    assert report.approved_for_compilation is False
    assert report.runtime_authority_granted is False
    assert {issue.code for issue in report.issues} == {"AUTHORING_AMBIGUITY_UNRESOLVED"}


def test_resolved_spec_can_be_promoted_to_normal_compiler_review() -> None:
    report = audit_manifest(_spec(blocking=False), SmaCrossStrategy.manifest)
    assert report.approved_for_compilation is True
    assert report.issues == ()


def test_authoring_audit_is_available_as_machine_readable_cli(tmp_path: Path) -> None:
    spec_path = tmp_path / "paper-spec.json"
    manifest_path = tmp_path / "strategy-manifest.json"
    output = tmp_path / "agent-audit-report.json"
    spec_path.write_text(_spec(blocking=False).model_dump_json(indent=2), encoding="utf-8")
    manifest_path.write_text(SmaCrossStrategy.manifest.model_dump_json(indent=2), encoding="utf-8")
    assert (
        main(
            [
                "author",
                "audit",
                "--spec",
                str(spec_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["approved_for_compilation"] is True
    assert payload["runtime_authority_granted"] is False


def test_authoring_cli_fails_closed_on_blocking_ambiguity(tmp_path: Path) -> None:
    spec_path = tmp_path / "blocked-spec.json"
    manifest_path = tmp_path / "strategy-manifest.json"
    output = tmp_path / "agent-audit-report.json"
    spec_path.write_text(_spec(blocking=True).model_dump_json(indent=2), encoding="utf-8")
    manifest_path.write_text(SmaCrossStrategy.manifest.model_dump_json(indent=2), encoding="utf-8")
    assert (
        main(
            [
                "author",
                "audit",
                "--spec",
                str(spec_path),
                "--manifest",
                str(manifest_path),
                "--output",
                str(output),
            ]
        )
        == 4
    )
    assert json.loads(output.read_text())["approved_for_compilation"] is False


def test_external_authoring_run_rejects_unpinned_source_before_import(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    assert main(["package", "export", "--output", str(packages)]) == 0
    output = tmp_path / "author-run"
    spec_path = tmp_path / "draft.json"
    source_path = tmp_path / "paper.txt"
    source_path.write_text("A reviewable paper source fixture.", encoding="utf-8")
    (packages / "rule.sma_cross" / "strategy.py").write_text(
        "raise AssertionError('rejected package was imported')\n", encoding="utf-8"
    )
    spec_path.write_text(_spec(blocking=False).model_dump_json(indent=2), encoding="utf-8")

    assert (
        main(
            [
                "author",
                "run",
                "--spec",
                str(spec_path),
                "--source",
                str(source_path),
                "--strategy-dir",
                str(packages / "rule.sma_cross"),
                "--output",
                str(output),
            ]
        )
        == 4
    )
    admission = json.loads((output / "external-strategy-admission.json").read_text())
    assert admission["approved_for_runtime_validation"] is False
    assert {issue["code"] for issue in admission["audit"]["issues"]} >= {
        "AUTHORING_SOURCE_UNPINNED",
        "AUTHORING_EVIDENCE_UNSPECIFIED",
        "AUTHORING_INFERENCE_UNSPECIFIED",
    }
    assert not (output / "bundle.json").exists()


def test_external_audit_rejects_source_mismatch_and_unlocated_claim(tmp_path: Path) -> None:
    source_path = tmp_path / "paper.txt"
    source_path.write_text("A declared and reviewable market signal.", encoding="utf-8")
    document = ingest(source_path)
    base = _spec(blocking=False)
    spec = base.model_copy(
        update={
            "reference": base.reference.model_copy(
                update={"source_sha256": "ab" * 32, "media_type": "text/plain"}
            ),
            "inference_rule": "Map the reviewed signal to a target position.",
            "evidence": (
                PaperEvidenceReference(
                    claim_id="missing.anchor",
                    page=1,
                    anchor="phrase absent from source",
                    locator="page 1",
                    interpretation="The claim defines the signal.",
                ),
            ),
        }
    )

    report = audit_external_implementation(spec, SmaCrossStrategy.manifest, document)

    assert report.approved_for_compilation is False
    assert {issue.code for issue in report.issues} >= {
        "AUTHORING_SOURCE_MISMATCH",
        "AUTHORING_EVIDENCE_ANCHOR_MISSING",
    }
