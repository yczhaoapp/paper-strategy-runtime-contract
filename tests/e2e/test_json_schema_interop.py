from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from psrc.authoring.audit import audit_external_implementation
from psrc.authoring.models import (
    ExternalStrategyAdmission,
    PaperEvidenceReference,
    PaperReference,
    PaperStrategySpec,
)
from psrc.cli import main
from psrc.contract.errors import ContractError, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.contract.models import StrategyManifest
from psrc.papers.models import PaperDocument, SourcePage
from psrc.runtime.report import FailureReport


def _references(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        own = (value["$ref"],) if isinstance(value.get("$ref"), str) else ()
        return own + tuple(reference for item in value.values() for reference in _references(item))
    if isinstance(value, list):
        return tuple(reference for item in value for reference in _references(item))
    return ()


def test_all_public_schemas_validate_real_documents_without_a_registry(
    tmp_path: Path,
) -> None:
    schemas = tmp_path / "schemas"
    packages = tmp_path / "packages"
    run_output = tmp_path / "run"
    assert main(["schema", "export", "--output", str(schemas)]) == 0
    assert main(["package", "export", "--output", str(packages)]) == 0
    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(packages / "supervised.ridge_return"),
                "--output",
                str(run_output),
            ]
        )
        == 0
    )

    bundle = json.loads((run_output / "bundle.json").read_text(encoding="utf-8"))
    report = bundle["report"]
    strategy = StrategyManifest.model_validate(bundle["strategy_manifest"])
    paper_text = "The reviewed feature predicts the signed next-event return."
    paper_source_sha256 = sha256(paper_text.encode()).hexdigest()
    paper_document = PaperDocument(
        source_uri="fixture://external-schema-validation",
        source_sha256=paper_source_sha256,
        media_type="text/plain",
        parser="utf8-v1",
        pages=(
            SourcePage(
                number=1,
                text=paper_text,
                sha256=paper_source_sha256,
            ),
        ),
    )
    paper_spec = PaperStrategySpec(
        spec_id="paper.external-schema-validation",
        reference=PaperReference(
            title="Independent schema validation fixture",
            locator="fixture://external-schema-validation",
            citation="PSRC interoperability fixture",
            source_sha256=paper_source_sha256,
            media_type="text/plain",
        ),
        strategy_kind=strategy.kind,
        hypothesis="A structured external strategy document is independently validatable.",
        data_requirements=strategy.data_requirements,
        output_actions=strategy.action_requirements.allowed,
        feature_definitions=("reviewed feature",),
        label_definition="One-step signed return.",
        training_objective="Minimize deterministic holdout error.",
        inference_rule="Map the predicted return to the declared target action.",
        execution_assumptions=("Closed bars only.",),
        evidence=(
            PaperEvidenceReference(
                claim_id="external-schema-validation.claim",
                page=1,
                anchor="reviewed feature predicts",
                locator="fixture page 1",
                interpretation="The feature predicts the supervised label.",
            ),
        ),
    )
    agent_audit = audit_external_implementation(paper_spec, strategy, paper_document)
    external_admission = ExternalStrategyAdmission(
        paper_spec=paper_spec,
        paper_spec_sha256=sha256_model(paper_spec),
        paper_document_sha256=sha256_model(paper_document),
        paper_source_sha256=paper_source_sha256,
        strategy_manifest_sha256=sha256_model(strategy),
        strategy_code_evidence_sha256=sha256_model(bundle["strategy_code_evidence"]),
        audit=agent_audit,
        approved_for_runtime_validation=agent_audit.approved_for_compilation,
    )
    now = datetime.now(UTC)
    error = ContractError(
        run_id="schema.external-failure",
        stage=ErrorStage.BACKTEST,
        code=ErrorCode.BACKTEST_FAILED,
        message="Independent validation fixture",
    )
    failure = FailureReport(
        run_id=error.run_id,
        started_at=now,
        finished_at=now,
        error=error,
    )

    from psrc.papers.catalog import read_bindings, read_source_registry
    from psrc.papers.compiler import extract_spec, read_recipe
    from psrc.papers.data import public_daily_bars
    from psrc.papers.ingest import ingest

    repository = Path(__file__).parents[2]
    recipe = read_recipe(repository / "papers/recipes/avellaneda2008.json")
    document = ingest(
        repository / "papers/sources/avellaneda2008.pdf", source_uri=recipe.source_url
    )
    reproduction_spec = extract_spec(document, recipe)
    paper_source = read_source_registry(repository / "papers/source-registry.json")[0]
    paper_binding = read_bindings(repository / "papers/bindings")[0]
    from psrc.papers.models import PublicDatasetSource

    _, data_source_evidence = public_daily_bars()
    public_dataset_source = PublicDatasetSource.model_validate_json(
        (repository / "data/public/source.json").read_text(encoding="utf-8")
    )
    samples: dict[str, object] = {
        "paper-document": document.model_dump(mode="json"),
        "paper-recipe": recipe.model_dump(mode="json"),
        "reproduction-spec": reproduction_spec.model_dump(mode="json"),
        "paper-source": paper_source.model_dump(mode="json"),
        "paper-strategy-binding": paper_binding.model_dump(mode="json"),
        "data-source-evidence": data_source_evidence.model_dump(mode="json"),
        "public-dataset-source": public_dataset_source.model_dump(mode="json"),
        "strategy-manifest": bundle["strategy_manifest"],
        "dataset-manifest": bundle["dataset_manifest"],
            "engine-capabilities": bundle["engine_capabilities"],
            "runtime-capabilities": bundle["runtime_capabilities"],
        "run-policy": bundle["run_policy"],
        "execution-plan": report["execution_plan"],
        "contract-error": error.model_dump(mode="json"),
        "artifact-manifest": report["artifacts"][0],
        "training-request": bundle["training_request"],
        "training-input-evidence": bundle["training_input_evidence"],
        "run-report": report,
        "failure-report": failure.model_dump(mode="json"),
        "unified-run-report": report,
        "run-bundle": bundle,
        "run-input-evidence": bundle["input_evidence"],
        "strategy-code-evidence": bundle["strategy_code_evidence"],
        "market-event": bundle["input_evidence"]["source_events"][0],
        "account-snapshot": report["account_snapshots"][0],
        "action": report["decisions"][0]["actions"][0],
        "fill": report["fills"][0],
        "decision-record": report["decisions"][0],
        "runtime-log-record": report["logs"][0],
        "paper-strategy-spec": paper_spec.model_dump(mode="json"),
        "agent-audit-report": agent_audit.model_dump(mode="json"),
        "external-strategy-admission": external_admission.model_dump(mode="json"),
    }
    catalog = json.loads((schemas / "catalog.json").read_text(encoding="utf-8"))
    assert set(samples) == set(catalog["schemas"])

    for name, filename in catalog["schemas"].items():
        schema = json.loads((schemas / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert all(reference.startswith("#/$defs/") for reference in _references(schema))
        Draft202012Validator(schema).validate(samples[name])
