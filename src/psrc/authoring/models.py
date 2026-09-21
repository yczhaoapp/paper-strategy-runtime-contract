from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from psrc.constants import CONTRACT_VERSION
from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    ActionKind,
    ContractModel,
    DataRequirement,
    Identifier,
    StrategyKind,
)


class PaperReference(ContractModel):
    title: str = Field(min_length=3)
    locator: str = Field(min_length=3)
    citation: str = Field(min_length=3)
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["application/pdf", "text/html", "text/plain"] | None = None


class PaperEvidenceReference(ContractModel):
    claim_id: Identifier
    page: int = Field(ge=1)
    anchor: str = Field(min_length=3, max_length=100)
    locator: str = Field(min_length=3)
    interpretation: str = Field(min_length=3)


class PaperAmbiguity(ContractModel):
    ambiguity_id: Identifier
    severity: Literal["info", "warning", "blocking"]
    statement: str
    source_excerpt_locator: str | None = None
    resolution: str | None = None
    resolved_by: Literal["paper", "operator", "experiment"] | None = None


class PaperStrategySpec(ContractModel):
    contract_version: str = CONTRACT_VERSION
    spec_id: Identifier
    reference: PaperReference
    strategy_kind: StrategyKind
    hypothesis: str = Field(min_length=3)
    data_requirements: tuple[DataRequirement, ...] = Field(min_length=1)
    output_actions: frozenset[ActionKind] = Field(min_length=1)
    feature_definitions: tuple[str, ...] = ()
    label_definition: str | None = None
    reward_definition: str | None = None
    training_objective: str | None = Field(default=None, min_length=3)
    inference_rule: str | None = Field(default=None, min_length=3)
    execution_assumptions: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[PaperEvidenceReference, ...] = ()
    ambiguities: tuple[PaperAmbiguity, ...] = ()


class AgentAuditIssue(ContractModel):
    code: Identifier
    severity: Literal["warning", "error"]
    path: str
    message: str


class AgentAuditReport(ContractModel):
    contract_version: str = CONTRACT_VERSION
    spec_id: Identifier
    strategy_id: Identifier
    approved_for_compilation: bool
    issues: tuple[AgentAuditIssue, ...]
    human_review_required: bool = True
    runtime_authority_granted: Literal[False] = False
    checked_fields: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {
                "strategy_kind",
                "data_requirements",
                "output_actions",
                "training_semantics",
                "source_identity",
                "paper_evidence",
                "inference_rule",
                "paper_ambiguities",
            }
        )
    )


class ExternalStrategyAdmission(ContractModel):
    """Hash-bound review record for an externally authored strategy package."""

    contract_version: str = CONTRACT_VERSION
    implementation_mode: Literal["external_implementation"] = "external_implementation"
    paper_spec: PaperStrategySpec
    paper_spec_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    paper_document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    paper_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    strategy_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    strategy_code_evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audit: AgentAuditReport
    approved_for_runtime_validation: bool
    runtime_authority_granted: Literal[False] = False

    @model_validator(mode="after")
    def validate_binding(self) -> ExternalStrategyAdmission:
        if sha256_model(self.paper_spec) != self.paper_spec_sha256:
            raise ValueError("paper strategy spec hash does not match the embedded spec")
        if self.audit.spec_id != self.paper_spec.spec_id:
            raise ValueError("authoring audit refers to a different paper strategy spec")
        if (
            self.approved_for_runtime_validation
            and self.paper_spec.reference.source_sha256 != self.paper_source_sha256
        ):
            raise ValueError("paper source hash differs from the paper strategy spec")
        if self.approved_for_runtime_validation != self.audit.approved_for_compilation:
            raise ValueError("admission decision differs from the deterministic audit")
        if self.approved_for_runtime_validation and not self.audit.human_review_required:
            raise ValueError("external admission cannot waive required human review")
        return self
