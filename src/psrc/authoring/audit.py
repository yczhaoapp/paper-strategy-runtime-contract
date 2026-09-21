from __future__ import annotations

from psrc.authoring.models import AgentAuditIssue, AgentAuditReport, PaperStrategySpec
from psrc.contract.models import StrategyKind, StrategyManifest, TrainingMode
from psrc.papers.ingest import normalize
from psrc.papers.models import PaperDocument


def audit_manifest(spec: PaperStrategySpec, manifest: StrategyManifest) -> AgentAuditReport:
    """Deterministically audit agent-authored metadata before runtime compilation."""
    issues: list[AgentAuditIssue] = []
    if spec.strategy_kind != manifest.kind:
        issues.append(
            AgentAuditIssue(
                code="AUTHORING_KIND_MISMATCH",
                severity="error",
                path="kind",
                message="Paper spec and strategy manifest declare different strategy kinds",
            )
        )
    if spec.data_requirements != manifest.data_requirements:
        issues.append(
            AgentAuditIssue(
                code="AUTHORING_DATA_CONTRACT_MISMATCH",
                severity="error",
                path="data_requirements",
                message="Paper-derived data contract changed before compilation",
            )
        )
    if not spec.output_actions.issuperset(manifest.action_requirements.allowed):
        issues.append(
            AgentAuditIssue(
                code="AUTHORING_ACTION_EXPANSION",
                severity="error",
                path="action_requirements.allowed",
                message="Manifest requests actions not justified by the paper spec",
            )
        )
    trainable = manifest.kind != StrategyKind.RULE
    training_required = manifest.lifecycle.training == TrainingMode.REQUIRED
    if trainable != training_required:
        issues.append(
            AgentAuditIssue(
                code="AUTHORING_TRAINING_MISMATCH",
                severity="error",
                path="lifecycle.training",
                message="Strategy kind and training lifecycle are inconsistent",
            )
        )
    if manifest.kind == StrategyKind.SUPERVISED and spec.label_definition is None:
        issues.append(
            AgentAuditIssue(
                code="AUTHORING_LABEL_UNSPECIFIED",
                severity="error",
                path="label_definition",
                message="A supervised paper spec must define its training label",
            )
        )
    if manifest.kind == StrategyKind.REINFORCEMENT_LEARNING and spec.reward_definition is None:
        issues.append(
            AgentAuditIssue(
                code="AUTHORING_REWARD_UNSPECIFIED",
                severity="error",
                path="reward_definition",
                message="An RL paper spec must define its reward",
            )
        )
    for ambiguity in spec.ambiguities:
        if ambiguity.severity == "blocking" and ambiguity.resolution is None:
            issues.append(
                AgentAuditIssue(
                    code="AUTHORING_AMBIGUITY_UNRESOLVED",
                    severity="error",
                    path=f"ambiguities.{ambiguity.ambiguity_id}",
                    message=ambiguity.statement,
                )
            )
    return AgentAuditReport(
        spec_id=spec.spec_id,
        strategy_id=manifest.strategy_id,
        approved_for_compilation=not any(issue.severity == "error" for issue in issues),
        issues=tuple(issues),
    )


def audit_external_implementation(
    spec: PaperStrategySpec, manifest: StrategyManifest, document: PaperDocument
) -> AgentAuditReport:
    """Apply the stricter admission policy used for an unseen external paper package."""
    base = audit_manifest(spec, manifest)
    issues = list(base.issues)

    def add(code: str, path: str, message: str) -> None:
        issues.append(
            AgentAuditIssue(code=code, severity="error", path=path, message=message)
        )

    if spec.reference.source_sha256 is None:
        add(
            "AUTHORING_SOURCE_UNPINNED",
            "reference.source_sha256",
            "External paper admission requires the exact source bytes to be hash-pinned",
        )
    elif spec.reference.source_sha256 != document.source_sha256:
        add(
            "AUTHORING_SOURCE_MISMATCH",
            "reference.source_sha256",
            "The supplied paper bytes do not match the hash pinned by the paper spec",
        )
    if spec.reference.media_type is not None and spec.reference.media_type != document.media_type:
        add(
            "AUTHORING_MEDIA_TYPE_MISMATCH",
            "reference.media_type",
            "The parsed paper media type differs from the paper spec",
        )
    if not spec.evidence:
        add(
            "AUTHORING_EVIDENCE_UNSPECIFIED",
            "evidence",
            "External paper admission requires at least one located source claim",
        )
    pages = {page.number: normalize(page.text) for page in document.pages}
    for index, evidence in enumerate(spec.evidence):
        page_text = pages.get(evidence.page)
        if page_text is None:
            add(
                "AUTHORING_EVIDENCE_PAGE_MISSING",
                f"evidence.{index}.page",
                "The evidence page does not exist in the supplied paper",
            )
        elif normalize(evidence.anchor) not in page_text:
            add(
                "AUTHORING_EVIDENCE_ANCHOR_MISSING",
                f"evidence.{index}.anchor",
                "The evidence anchor was not found on the declared paper page",
            )
    if spec.inference_rule is None:
        add(
            "AUTHORING_INFERENCE_UNSPECIFIED",
            "inference_rule",
            "External paper admission requires an explicit inference-to-action rule",
        )
    if manifest.kind != StrategyKind.RULE and not spec.feature_definitions:
        add(
            "AUTHORING_FEATURES_UNSPECIFIED",
            "feature_definitions",
            "Trainable external strategies must declare their model inputs or state features",
        )
    if manifest.kind != StrategyKind.RULE and spec.training_objective is None:
        add(
            "AUTHORING_OBJECTIVE_UNSPECIFIED",
            "training_objective",
            "Trainable external strategies must declare their optimization objective",
        )
    if manifest.entrypoint != "strategy.py:Strategy":
        add(
            "AUTHORING_ENTRYPOINT_UNSUPPORTED",
            "entrypoint",
            "External admission requires the reviewable package-local "
            "strategy.py:Strategy entrypoint",
        )
    return base.model_copy(
        update={
            "approved_for_compilation": not any(issue.severity == "error" for issue in issues),
            "issues": tuple(issues),
        }
    )
