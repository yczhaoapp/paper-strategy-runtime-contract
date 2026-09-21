from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from psrc.contract.models import ActionKind, ContractModel, DataKind, Identifier, StrategyKind

Digest = str


class SourcePage(ContractModel):
    number: int = Field(ge=1)
    text: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PaperDocument(ContractModel):
    version: Literal["1.0"] = "1.0"
    source_uri: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["application/pdf", "text/html", "text/plain"]
    parser: str
    pages: tuple[SourcePage, ...] = Field(min_length=1)


class EvidenceClaim(ContractModel):
    claim_id: Identifier
    page: int = Field(ge=1)
    anchor: str = Field(min_length=3, max_length=100)
    locator: str
    interpretation: str


class PaperRecipe(ContractModel):
    version: Literal["1.0"] = "1.0"
    paper_id: Identifier
    title: str
    source_url: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    algorithm: Literal[
        "avellaneda_stoikov", "queue_logistic", "execution_dynamic_q", "moving_average_band"
    ]
    execution_engine: Literal["reference", "backtrader"] = "reference"
    strategy_kind: StrategyKind
    claims: tuple[EvidenceClaim, ...] = Field(min_length=2)
    parameters: dict[str, float]
    assumptions: tuple[str, ...] = Field(min_length=1)
    deviations: tuple[str, ...] = Field(min_length=1)
    fidelity: Literal["algorithm_reproduction"] = "algorithm_reproduction"
    mapping_review: str = Field(min_length=10)
    data_mode: Literal["synthetic_fixture", "public_historical"] = "synthetic_fixture"

    @model_validator(mode="after")
    def validate_algorithm(self) -> PaperRecipe:
        expected = {
            "avellaneda_stoikov": StrategyKind.RULE,
            "moving_average_band": StrategyKind.RULE,
            "queue_logistic": StrategyKind.SUPERVISED,
            "execution_dynamic_q": StrategyKind.REINFORCEMENT_LEARNING,
        }
        if self.strategy_kind != expected[self.algorithm]:
            raise ValueError("algorithm and strategy kind disagree")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ValueError("claim IDs must be unique")
        return self


class GroundedClaim(ContractModel):
    claim: EvidenceClaim
    normalized_start: int = Field(ge=0)
    normalized_end: int = Field(gt=0)
    page_sha256: str


class ReproductionSpec(ContractModel):
    version: Literal["1.0"] = "1.0"
    recipe: PaperRecipe
    recipe_sha256: str
    document_sha256: str
    evidence: tuple[GroundedClaim, ...]
    compiler: Literal["psrc-paper-compiler.v1"] = "psrc-paper-compiler.v1"
    arbitrary_code_execution: Literal[False] = False
    empirical_results_reproduced: Literal[False] = False


class PaperSource(ContractModel):
    """Immutable public paper bytes used by one or more strategy bindings."""

    version: Literal["1.0"] = "1.0"
    source_id: Identifier
    title: str = Field(min_length=5)
    authors: tuple[str, ...] = Field(min_length=1)
    source_url: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ImplementationFile(ContractModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbols: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_path(self) -> ImplementationFile:
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("implementation path must be a safe repository-relative Python path")
        return self


class ClaimImplementationLink(ContractModel):
    """Auditable edge from one paper claim to code and an executed test."""

    claim_id: Identifier
    implementation_path: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    verification_test: str = Field(min_length=5)

    @model_validator(mode="after")
    def validate_paths(self) -> ClaimImplementationLink:
        path = PurePosixPath(self.implementation_path)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("claim link implementation path must be safe and Python")
        test_path, separator, test_name = self.verification_test.partition("::")
        parsed_test_path = PurePosixPath(test_path)
        if (
            not separator
            or not test_name.startswith("test_")
            or parsed_test_path.is_absolute()
            or ".." in parsed_test_path.parts
            or parsed_test_path.suffix != ".py"
            or not parsed_test_path.parts
            or parsed_test_path.parts[0] != "tests"
        ):
            raise ValueError("claim link must reference an exact repository pytest node")
        return self


class RuntimeBehaviorExpectation(ContractModel):
    """Deterministic behavioral signature required from a fresh unified run."""

    source_data_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data_kinds: tuple[DataKind, ...] = Field(min_length=1)
    timeframes: tuple[str, ...] = Field(min_length=1)
    observed_action_kinds: tuple[ActionKind, ...] = Field(min_length=1)
    observed_reason_codes: tuple[Identifier, ...] = Field(min_length=1)
    expected_decisions: int = Field(gt=0)
    expected_orders: int = Field(ge=0)
    expected_fills: int = Field(gt=0)
    artifact_algorithm: str | None = None
    artifact_id: str | None = None

    @model_validator(mode="after")
    def validate_signature(self) -> RuntimeBehaviorExpectation:
        ordered = (
            self.data_kinds,
            self.timeframes,
            self.observed_action_kinds,
            self.observed_reason_codes,
        )
        if any(len(values) != len(set(values)) for values in ordered):
            raise ValueError("runtime behavior sets must not contain duplicates")
        if (self.artifact_algorithm is None) != (self.artifact_id is None):
            raise ValueError("artifact algorithm and artifact id must be declared together")
        if self.artifact_id is not None and not self.artifact_id.startswith("sha256-"):
            raise ValueError("runtime artifact id must be content addressed")
        return self


class PaperStrategyBinding(ContractModel):
    """Review record binding a contract example to grounded paper claims and code."""

    version: Literal["1.1"] = "1.1"
    strategy_id: Identifier
    strategy_kind: StrategyKind
    source_id: Identifier
    fidelity: Literal["formula_reproduction", "algorithm_reproduction", "method_reproduction"]
    algorithm_fidelity: Literal["A1_method_equivalent", "A2_algorithm_exact"]
    data_fidelity: Literal["D0_synthetic", "D1_public_proxy", "D2_equivalent_market", "D3_original"]
    experimental_fidelity: Literal["E0_runtime_only", "E1_directional_result", "E2_main_results"]
    runtime_fidelity: Literal["R1_unified_contract", "R2_independent_strict"]
    claims: tuple[EvidenceClaim, ...] = Field(min_length=2)
    implementation: tuple[ImplementationFile, ...] = Field(min_length=2)
    verification_tests: tuple[str, ...] = Field(min_length=1)
    claim_links: tuple[ClaimImplementationLink, ...] = Field(min_length=2)
    runtime_expectation: RuntimeBehaviorExpectation
    data_mode: Literal["synthetic_fixture", "public_historical"]
    original_paper_dataset: Literal[False] = False
    empirical_results_reproduced: Literal[False] = False
    assumptions: tuple[str, ...] = Field(min_length=1)
    deviations: tuple[str, ...] = Field(min_length=1)
    mapping_review: str = Field(min_length=20)

    @model_validator(mode="after")
    def validate_binding(self) -> PaperStrategyBinding:
        claim_ids = {claim.claim_id for claim in self.claims}
        if len(claim_ids) != len(self.claims):
            raise ValueError("binding claim IDs must be unique")
        if len({item.path for item in self.implementation}) != len(self.implementation):
            raise ValueError("implementation paths must be unique")
        if len(set(self.verification_tests)) != len(self.verification_tests):
            raise ValueError("verification test nodes must be unique")
        link_ids = {link.claim_id for link in self.claim_links}
        if len(link_ids) != len(self.claim_links) or link_ids != claim_ids:
            raise ValueError("every paper claim must have exactly one implementation link")
        implementations = {
            (item.path, symbol) for item in self.implementation for symbol in item.symbols
        }
        for link in self.claim_links:
            if (link.implementation_path, link.symbol) not in implementations:
                raise ValueError("claim link does not name a declared implementation symbol")
            if link.verification_test not in self.verification_tests:
                raise ValueError("claim link does not name a declared verification test")
        if self.strategy_kind == StrategyKind.RULE:
            if self.runtime_expectation.artifact_id is not None:
                raise ValueError("rule strategy behavior must not declare a training artifact")
        elif self.runtime_expectation.artifact_id is None:
            raise ValueError("trainable strategy behavior requires a content-addressed artifact")
        if self.fidelity in {"formula_reproduction", "algorithm_reproduction"}:
            if self.algorithm_fidelity != "A2_algorithm_exact":
                raise ValueError("formula/algorithm reproduction requires A2 algorithm fidelity")
        elif self.algorithm_fidelity != "A1_method_equivalent":
            raise ValueError("method reproduction requires A1 algorithm fidelity")
        expected_data_fidelity = {
            "synthetic_fixture": "D0_synthetic",
            "public_historical": "D1_public_proxy",
        }[self.data_mode]
        if self.data_fidelity != expected_data_fidelity:
            raise ValueError("data mode and data fidelity disagree")
        if self.empirical_results_reproduced:
            raise ValueError("binding must not claim unverified empirical paper results")
        if self.experimental_fidelity != "E0_runtime_only":
            raise ValueError("current suite verifies runtime behavior, not paper results")
        return self


class DataSourceEvidence(ContractModel):
    """Data origin is independent from paper and algorithm fidelity."""

    version: Literal["1.0"] = "1.0"
    origin: Literal["synthetic_fixture", "public_historical"]
    dataset_id: Identifier
    source_url: str | None = None
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance_record_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    license_name: str
    license_url: str | None = None
    license_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    instrument_id: Identifier
    period_start: str
    period_end: str
    record_count: int = Field(gt=0)
    transforms: tuple[str, ...] = Field(min_length=1)
    original_paper_dataset: Literal[False] = False
    empirical_results_reproduced: Literal[False] = False

    @model_validator(mode="after")
    def validate_public_source(self) -> DataSourceEvidence:
        if self.origin == "public_historical" and not all(
            (self.source_url, self.provenance_record_sha256, self.license_url, self.license_sha256)
        ):
            raise ValueError("public historical data requires source and license evidence")
        if self.origin == "synthetic_fixture" and any(
            (self.provenance_record_sha256, self.license_sha256)
        ):
            raise ValueError("synthetic fixture must not claim external source/license bytes")
        if self.period_start > self.period_end:
            raise ValueError("data evidence period is reversed")
        return self


class PublicDatasetSource(ContractModel):
    """Closed, hash-pinned redistribution record for a bundled public fixture."""

    version: Literal["1.0"] = "1.0"
    dataset_id: Identifier
    file: str
    source_url: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retrieved_at: str
    records: int = Field(gt=0)
    instrument: Identifier
    period: tuple[str, str]
    columns_used: tuple[str, ...] = Field(min_length=1)
    repository_license: str = Field(min_length=1)
    repository_license_file: str
    repository_license_url: str
    repository_license_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rights_note: str = Field(min_length=20)
    availability_rule: str = Field(min_length=20)
    paper_dataset: Literal[False] = False
    empirical_paper_results_reproduced: Literal[False] = False

    @model_validator(mode="after")
    def validate_files_and_period(self) -> PublicDatasetSource:
        for name in (self.file, self.repository_license_file):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
                raise ValueError("public dataset files must be safe sibling names")
        if self.period[0] > self.period[1]:
            raise ValueError("public dataset period is reversed")
        if len(self.columns_used) != len(set(self.columns_used)):
            raise ValueError("public dataset columns must be unique")
        return self
