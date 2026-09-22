from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import ValidationError

from psrc.authoring.models import (
    PaperEvidenceReference,
    PaperReference,
    PaperStrategySpec,
)
from psrc.cli import main
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import SandboxMode, StrategyKind, StrategyManifest
from psrc.runtime.package import (
    StrategyPackage,
    discover_strategy_packages,
    export_strategy_packages,
    load_strategy,
)
from psrc.runtime.report import RunBundle
from psrc.strategies.catalog import all_examples


def _materialize_external_package(
    *,
    tmp_path: Path,
    generated: StrategyPackage,
    strategy_id: str,
    source_builder: Callable[[StrategyManifest], str],
    allowed_imports: frozenset[str] = frozenset(),
) -> tuple[Path, StrategyManifest]:
    package_root = tmp_path / strategy_id
    package_root.mkdir()
    resources = generated.manifest.resources.model_copy(
        update={"allowed_imports": generated.manifest.resources.allowed_imports | allowed_imports}
    )
    manifest = generated.manifest.model_copy(
        update={"strategy_id": strategy_id, "resources": resources}
    )
    (package_root / "strategy.yaml").write_text(
        yaml.safe_dump(
            manifest.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for filename in (
        "dataset-manifest.json",
        "input-events.json",
        "training-request.json",
        "training-input-evidence.json",
    ):
        source = generated.root / filename
        if source.is_file():
            (package_root / filename).write_bytes(source.read_bytes())
    (package_root / "strategy.py").write_text(source_builder(manifest), encoding="utf-8")
    return package_root, manifest


def test_eighteen_independent_packages_discover_and_load(tmp_path: Path) -> None:
    export_strategy_packages(tmp_path, tuple(item.manifest for item in all_examples()))
    packages = discover_strategy_packages(tmp_path)
    assert len(packages) == 18
    counts = {kind: 0 for kind in StrategyKind}
    for package in packages:
        strategy = load_strategy(package, sandbox_mode=SandboxMode.DEVELOPMENT)
        assert strategy.manifest == package.manifest
        assert package.manifest.entrypoint == "strategy.py:Strategy"
        assert (package.root / "strategy.py").is_file()
        assert (package.root / "STRATEGY_CARD.md").is_file()
        counts[StrategyKind(package.manifest.kind)] += 1
    assert set(counts.values()) == {6}


def test_strict_package_load_never_falls_back_to_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_strategy_packages(tmp_path, (all_examples()[0].manifest,))
    package = discover_strategy_packages(tmp_path)[0]
    monkeypatch.delenv("PSRC_SANDBOX_ATTESTATION", raising=False)
    with pytest.raises(ContractViolation) as raised:
        load_strategy(package, sandbox_mode=SandboxMode.STRICT_CONTAINER)
    assert raised.value.error.code == ErrorCode.SANDBOX_UNAVAILABLE
    assert raised.value.error.details["fallback_used"] is False


def test_package_source_policy_is_enforced_before_import(tmp_path: Path) -> None:
    export_strategy_packages(tmp_path, (all_examples()[0].manifest,))
    (tmp_path / all_examples()[0].manifest.strategy_id / "strategy.py").write_text(
        "import socket\n", encoding="utf-8"
    )
    package = discover_strategy_packages(tmp_path)[0]
    with pytest.raises(ContractViolation) as raised:
        load_strategy(package, sandbox_mode=SandboxMode.DEVELOPMENT)
    assert raised.value.error.code == ErrorCode.SANDBOX_POLICY_DENIED
    assert raised.value.error.details["fallback_used"] is False


def test_source_change_after_discovery_fails_before_import(tmp_path: Path) -> None:
    export_strategy_packages(tmp_path, (all_examples()[0].manifest,))
    package = discover_strategy_packages(tmp_path)[0]
    (package.root / "strategy.py").write_text("raise RuntimeError('changed')\n", encoding="utf-8")
    with pytest.raises(ContractViolation) as raised:
        load_strategy(package, sandbox_mode=SandboxMode.DEVELOPMENT)
    assert raised.value.error.code == ErrorCode.SOURCE_HASH_MISMATCH
    assert raised.value.error.details["fallback_used"] is False


def test_standalone_external_strategy_directory_executes_without_catalog(
    tmp_path: Path,
) -> None:
    generated_root = tmp_path / "generated"
    assert main(["package", "export", "--output", str(generated_root)]) == 0
    generated = next(
        package
        for package in discover_strategy_packages(generated_root)
        if package.manifest.strategy_id == "rule.sma_cross"
    )

    package_root = tmp_path / "paper-authored-strategy"
    package_root.mkdir()
    external_manifest = generated.manifest.model_copy(
        update={"strategy_id": "rule.external_standalone"}
    )
    (package_root / "strategy.yaml").write_text(
        yaml.safe_dump(
            external_manifest.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    for filename in ("dataset-manifest.json", "input-events.json"):
        (package_root / filename).write_bytes((generated.root / filename).read_bytes())

    source = f"""from psrc.contract.models import StrategyManifest
from psrc.domain.actions import TargetPosition


class Strategy:
    manifest = StrategyManifest.model_validate_json({external_manifest.model_dump_json()!r})

    def on_start(self):
        self.event_count = 0

    def on_event(self, event, account):
        del account
        self.event_count += 1
        target = "1" if self.event_count % 2 else "-1"
        return (TargetPosition(
            instrument_id=event.instrument_id,
            quantity=target,
            reason_code="signal.external_standalone",
        ),)

    def on_finish(self):
        pass
"""
    assert "psrc.strategies" not in source
    assert "psrc.examples" not in source
    (package_root / "strategy.py").write_text(source, encoding="utf-8")

    output = tmp_path / "external-run"
    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(package_root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    bundle = RunBundle.model_validate_json((output / "bundle.json").read_text(encoding="utf-8"))
    assert bundle.strategy_manifest == external_manifest
    assert bundle.strategy_code_evidence is not None
    assert bundle.strategy_code_evidence.package_files[0].path == "strategy.py"
    assert (output / "strategy-code-evidence.json").is_file()
    assert bundle.report.execution_plan.strategy_code_evidence_sha256 is not None
    assert bundle.report.run_id == "package.rule.external_standalone"
    assert bundle.report.metrics.fills > 0


def _external_supervised_source(manifest: StrategyManifest) -> str:
    return f"""from hashlib import sha256

from psrc.contract.models import StrategyManifest
from psrc.domain.actions import Prediction, TargetPosition


class Strategy:
    manifest = StrategyManifest.model_validate_json({manifest.model_dump_json()!r})

    def __init__(self):
        self.threshold = None
        self.artifact_id = None

    def train(self, request, store):
        if not request.labels:
            raise ValueError("labels are required")
        threshold = sum(request.labels) / len(request.labels)
        payload = str(threshold).encode("utf-8")
        artifact_id = f"sha256-{{sha256(payload).hexdigest()}}"
        return store.save_bytes(
            run_id=request.run_id,
            artifact_id=artifact_id,
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.strategy_version,
            artifact_kind="model",
            framework="external-python",
            logical_name="threshold.txt",
            media_type="text/plain",
            payload=payload,
            training_dataset_id=request.dataset_id,
            seed=request.seed,
            training_request_sha256=request.request_sha256,
        )

    def load(self, manifest, store, *, run_id):
        payload = store.load_bytes(
            run_id=run_id,
            strategy_id=self.manifest.strategy_id,
            manifest=manifest,
        )["threshold.txt"]
        self.threshold = float(payload.decode("utf-8"))
        self.artifact_id = manifest.artifact_id

    def on_start(self):
        pass

    def on_event(self, event, account):
        del account
        if self.threshold is None or self.artifact_id is None:
            raise RuntimeError("model is not loaded")
        score = float((event.payload.close - event.payload.open) / event.payload.open)
        target = "1" if score >= self.threshold else "-1"
        return (
            Prediction(
                instrument_id=event.instrument_id,
                value=str(score),
                horizon="PT1M",
                model_artifact_id=self.artifact_id,
                reason_code="external.supervised.score",
            ),
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=target,
                reason_code="external.supervised.target",
            ),
        )

    def on_finish(self):
        pass
"""


def _external_rule_source(manifest: StrategyManifest) -> str:
    return f"""from psrc.contract.models import StrategyManifest
from psrc.domain.actions import TargetPosition


class Strategy:
    manifest = StrategyManifest.model_validate_json({manifest.model_dump_json()!r})

    def on_start(self):
        self.sign = -1

    def on_event(self, event, account):
        del account
        self.sign *= -1
        return (TargetPosition(
            instrument_id=event.instrument_id,
            quantity=str(self.sign),
            reason_code="external.holdout.rule",
        ),)

    def on_finish(self):
        pass
"""


def _external_rl_source(manifest: StrategyManifest) -> str:
    return f"""from hashlib import sha256

from psrc.contract.models import StrategyManifest
from psrc.domain.actions import TargetPosition


class Strategy:
    manifest = StrategyManifest.model_validate_json({manifest.model_dump_json()!r})

    def __init__(self):
        self.target = None

    def train(self, request, store):
        if not request.transitions:
            raise ValueError("transitions are required")
        rewards = [0.0, 0.0, 0.0]
        for transition in request.transitions:
            rewards[transition.action] += transition.reward
        selected = max(range(3), key=lambda action: (rewards[action], -action))
        target = -1 if selected == 0 else 1
        payload = str(target).encode("utf-8")
        artifact_id = f"sha256-{{sha256(payload).hexdigest()}}"
        return store.save_bytes(
            run_id=request.run_id,
            artifact_id=artifact_id,
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.strategy_version,
            artifact_kind="policy",
            framework="external-python",
            logical_name="policy.txt",
            media_type="text/plain",
            payload=payload,
            training_dataset_id=request.dataset_id,
            seed=request.seed,
            training_request_sha256=request.request_sha256,
        )

    def load(self, manifest, store, *, run_id):
        payload = store.load_bytes(
            run_id=run_id,
            strategy_id=self.manifest.strategy_id,
            manifest=manifest,
        )["policy.txt"]
        self.target = payload.decode("utf-8")

    def on_start(self):
        pass

    def on_event(self, event, account):
        del account
        if self.target is None:
            raise RuntimeError("policy is not loaded")
        return (TargetPosition(
            instrument_id=event.instrument_id,
            quantity=self.target,
            reason_code="external.rl.policy",
        ),)

    def on_finish(self):
        pass
"""


@pytest.mark.parametrize(
    ("base_strategy_id", "external_strategy_id", "source_builder"),
    (
        (
            "supervised.ridge_return",
            "supervised.external_standalone",
            _external_supervised_source,
        ),
        (
            "reinforcement_learning.sarsa_trend",
            "reinforcement_learning.external_standalone",
            _external_rl_source,
        ),
    ),
)
def test_external_trainable_strategy_directories_execute_without_internal_catalog(
    tmp_path: Path,
    base_strategy_id: str,
    external_strategy_id: str,
    source_builder: Callable[[StrategyManifest], str],
) -> None:
    generated_root = tmp_path / "generated"
    assert main(["package", "export", "--output", str(generated_root)]) == 0
    generated = next(
        package
        for package in discover_strategy_packages(generated_root)
        if package.manifest.strategy_id == base_strategy_id
    )
    package_root, expected_manifest = _materialize_external_package(
        tmp_path=tmp_path,
        generated=generated,
        strategy_id=external_strategy_id,
        source_builder=source_builder,
        allowed_imports=frozenset({"hashlib"}),
    )
    source = (package_root / "strategy.py").read_text(encoding="utf-8")
    assert "psrc.strategies" not in source
    assert "psrc.examples" not in source

    output = tmp_path / f"run-{external_strategy_id}"
    assert (
        main(
            [
                "run",
                "--strategy-dir",
                str(package_root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    bundle = RunBundle.model_validate_json((output / "bundle.json").read_text(encoding="utf-8"))
    assert bundle.strategy_manifest == expected_manifest
    assert bundle.report.execution_plan.strategy_id == external_strategy_id
    assert bundle.strategy_code_evidence is not None
    assert bundle.strategy_code_evidence.package_files[0].path == "strategy.py"
    assert bundle.training_request is not None
    assert bundle.training_input_evidence is not None
    assert bundle.report.execution_plan.training_input_evidence_sha256 is not None
    assert len(bundle.report.artifacts) == 1
    assert bundle.report.metrics.fills > 0


@pytest.mark.parametrize(
    ("base_strategy_id", "external_strategy_id", "source_builder"),
    (
        ("rule.sma_cross", "rule.unseen_paper", _external_rule_source),
        (
            "supervised.ridge_return",
            "supervised.unseen_paper",
            _external_supervised_source,
        ),
        (
            "reinforcement_learning.sarsa_trend",
            "reinforcement_learning.unseen_paper",
            _external_rl_source,
        ),
    ),
)
def test_unregistered_paper_spec_and_external_code_use_formal_authoring_path(
    tmp_path: Path,
    base_strategy_id: str,
    external_strategy_id: str,
    source_builder: Callable[[StrategyManifest], str],
) -> None:
    generated_root = tmp_path / "generated"
    assert main(["package", "export", "--output", str(generated_root)]) == 0
    generated = next(
        package
        for package in discover_strategy_packages(generated_root)
        if package.manifest.strategy_id == base_strategy_id
    )
    package_root, manifest = _materialize_external_package(
        tmp_path=tmp_path,
        generated=generated,
        strategy_id=external_strategy_id,
        source_builder=source_builder,
        allowed_imports=(
            frozenset({"hashlib"})
            if StrategyKind(generated.manifest.kind) != StrategyKind.RULE
            else frozenset()
        ),
    )
    kind = StrategyKind(manifest.kind)
    paper_text = (
        "Holdout paper fixture. The reviewed signal maps deterministically to a "
        "canonical trading action using only information available at event time."
    )
    media_type: Literal["text/html", "text/plain"]
    if kind == StrategyKind.RULE:
        paper_payload = f"<html><body><p>{paper_text}</p></body></html>"
        paper_source = tmp_path / f"{external_strategy_id}.paper.html"
        media_type = "text/html"
    else:
        paper_payload = paper_text
        paper_source = tmp_path / f"{external_strategy_id}.paper.txt"
        media_type = "text/plain"
    paper_source.write_text(paper_payload, encoding="utf-8")
    spec = PaperStrategySpec(
        spec_id=f"holdout.{external_strategy_id}",
        reference=PaperReference(
            title="Unregistered holdout paper used only by this black-box test",
            locator="https://example.invalid/unregistered-paper",
            citation="Independent holdout fixture",
            source_sha256=sha256(paper_payload.encode()).hexdigest(),
            media_type=media_type,
        ),
        strategy_kind=kind,
        hypothesis="The external implementation emits a deterministic canonical action.",
        data_requirements=manifest.data_requirements,
        output_actions=manifest.action_requirements.allowed,
        feature_definitions=("canonical event features",) if kind != StrategyKind.RULE else (),
        label_definition=("signed next-event outcome" if kind == StrategyKind.SUPERVISED else None),
        reward_definition=(
            "transition reward supplied by the reviewed environment"
            if kind == StrategyKind.REINFORCEMENT_LEARNING
            else None
        ),
        training_objective=(
            "deterministic empirical objective" if kind != StrategyKind.RULE else None
        ),
        inference_rule="Map the reviewed signal to declared canonical actions.",
        execution_assumptions=("Use only information available at event time.",),
        evidence=(
            PaperEvidenceReference(
                claim_id=f"{external_strategy_id}.claim",
                page=1,
                anchor="The reviewed signal maps deterministically",
                locator="Section 2, holdout claim",
                interpretation="The claim defines the signal used by the external code.",
            ),
        ),
    )
    spec_path = tmp_path / f"{external_strategy_id}.paper-spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / f"author-run-{external_strategy_id}"

    assert (
        main(
            [
                "author",
                "run",
                "--spec",
                str(spec_path),
                "--source",
                str(paper_source),
                "--strategy-dir",
                str(package_root),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    bundle = RunBundle.model_validate_json((output / "bundle.json").read_text(encoding="utf-8"))
    admission = bundle.external_strategy_admission
    assert admission is not None
    assert admission.paper_spec.spec_id == spec.spec_id
    assert admission.approved_for_runtime_validation is True
    assert admission.runtime_authority_granted is False
    assert bundle.report.execution_plan.external_strategy_admission_sha256 is not None
    assert (output / "external-strategy-admission.json").is_file()
    assert (output / "paper-document.json").is_file()
    assert "psrc.strategies" not in (package_root / "strategy.py").read_text(encoding="utf-8")
    tampered = bundle.model_dump(mode="json")
    tampered["external_paper_document"]["pages"][0]["text"] = "tampered paper text"
    with pytest.raises(ValidationError):
        RunBundle.model_validate(tampered)
    forged_audit = bundle.model_dump(mode="json")
    forged_audit["external_strategy_admission"]["audit"]["checked_fields"] = []
    with pytest.raises(ValidationError):
        RunBundle.model_validate(forged_audit)
