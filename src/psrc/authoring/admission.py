from __future__ import annotations

from psrc.authoring.audit import audit_external_implementation
from psrc.authoring.models import ExternalStrategyAdmission, PaperStrategySpec
from psrc.contract.hashing import sha256_model
from psrc.papers.models import PaperDocument
from psrc.runtime.package import StrategyPackage


def build_external_admission(
    spec: PaperStrategySpec, document: PaperDocument, package: StrategyPackage
) -> ExternalStrategyAdmission:
    """Bind an unseen paper spec to package metadata and source before any import occurs."""
    audit = audit_external_implementation(spec, package.manifest, document)
    return ExternalStrategyAdmission(
        paper_spec=spec,
        paper_spec_sha256=sha256_model(spec),
        paper_document_sha256=sha256_model(document),
        paper_source_sha256=document.source_sha256,
        strategy_manifest_sha256=package.manifest_sha256,
        strategy_code_evidence_sha256=sha256_model(package.code_evidence),
        audit=audit,
        approved_for_runtime_validation=audit.approved_for_compilation,
    )
