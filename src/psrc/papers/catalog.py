from __future__ import annotations

import ast
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pydantic import TypeAdapter

from psrc.contract.hashing import sha256_model
from psrc.contract.models import StrategyKind
from psrc.papers.ingest import digest, ingest, normalize
from psrc.papers.models import DataSourceEvidence, PaperSource, PaperStrategyBinding
from psrc.runtime.package import discover_strategy_packages
from psrc.runtime.report import RunBundle


def read_source_registry(path: Path) -> tuple[PaperSource, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != "1.0" or set(payload) != {"version", "sources"}:
        raise ValueError("paper source registry must be a versioned closed object")
    sources = TypeAdapter(tuple[PaperSource, ...]).validate_python(payload["sources"])
    if len({source.source_id for source in sources}) != len(sources):
        raise ValueError("paper source IDs must be unique")
    return sources


def read_bindings(root: Path) -> tuple[PaperStrategyBinding, ...]:
    bindings = tuple(
        PaperStrategyBinding.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*.json"))
    )
    if len({binding.strategy_id for binding in bindings}) != len(bindings):
        raise ValueError("paper strategy bindings must be one-to-one")
    return bindings


def _validate_test_node(repository: Path, node_id: str) -> None:
    path_text, separator, test_name = node_id.partition("::")
    if not separator:
        raise ValueError(f"verification test is not an exact pytest node: {node_id}")
    path = (repository / path_text).resolve()
    if not path.is_relative_to(repository.resolve()) or not path.is_file():
        raise ValueError(f"verification test file is absent or unsafe: {path_text}")
    function_name = test_name.split("[", maxsplit=1)[0]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path_text)
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    if function_name not in functions:
        raise ValueError(f"verification test function is absent: {node_id}")


def _executed_pytest_nodes(path: Path) -> set[str]:
    if not path.is_file():
        raise ValueError("JUnit evidence is absent")
    root = ElementTree.parse(path).getroot()
    nodes = set()
    for case in root.iter("testcase"):
        classname, name = case.attrib.get("classname"), case.attrib.get("name")
        if not classname or not name:
            continue
        nodes.add(f"{classname.replace('.', '/')}.py::{name}")
    return nodes


def _runtime_behavior(
    binding: PaperStrategyBinding,
    package: Any,
    evidence_root: Path,
    executed_tests: set[str],
) -> dict[str, Any]:
    missing_tests = sorted(set(binding.verification_tests) - executed_tests)
    if missing_tests:
        raise ValueError(f"bound verification tests were not executed: {missing_tests}")
    bundle_path = evidence_root / "runs/all" / binding.strategy_id / "bundle.json"
    bundle = RunBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    if bundle.strategy_manifest != package.manifest:
        raise ValueError("runtime manifest differs from reviewed package")
    if bundle.strategy_code_evidence != package.code_evidence:
        raise ValueError("runtime code evidence differs from reviewed package")

    report = bundle.report
    expectation = binding.runtime_expectation
    data_kinds = sorted({str(item.kind) for item in package.manifest.data_requirements})
    timeframes = sorted(
        {
            item.timeframe.interval or str(item.timeframe.mode)
            for item in package.manifest.data_requirements
        }
    )
    action_kinds = sorted(
        {str(action.kind) for decision in report.decisions for action in decision.actions}
    )
    reason_codes = sorted(
        {action.reason_code for decision in report.decisions for action in decision.actions}
    )
    decision_sha256 = sha256_model(
        {"decisions": [item.model_dump(mode="json") for item in report.decisions]}
    )
    algorithms = tuple(str(item.metadata.get("algorithm")) for item in report.artifacts)
    artifact_ids = tuple(item.artifact_id for item in report.artifacts)
    if binding.data_mode == "public_historical":
        raw_evidence = bundle.dataset_manifest.extensions.get("org.singularityx.data-provenance")
        data_evidence = DataSourceEvidence.model_validate(raw_evidence)
        if data_evidence.origin != "public_historical":
            raise ValueError("public binding lacks public data-source evidence")
        if not all(
            event.source.startswith("public.fixture.sha256-")
            for event in bundle.input_evidence.source_events
        ):
            raise ValueError("public binding runtime contains non-public source events")
        if bundle.training_request is not None and (
            bundle.training_request.metadata.get("origin") != "public_historical"
            or not bundle.training_request.dataset_id.startswith("public.")
        ):
            raise ValueError("public trainable binding lacks public training provenance")
    observed = {
        "source_data_sha256": bundle.input_evidence.source_data_sha256,
        "decision_sha256": decision_sha256,
        "data_kinds": data_kinds,
        "timeframes": timeframes,
        "observed_action_kinds": action_kinds,
        "observed_reason_codes": reason_codes,
        "expected_decisions": report.metrics.decisions,
        "expected_orders": report.metrics.orders,
        "expected_fills": report.metrics.fills,
        "artifact_algorithm": algorithms[0] if len(algorithms) == 1 else None,
        "artifact_id": artifact_ids[0] if len(artifact_ids) == 1 else None,
    }
    expected = expectation.model_dump(mode="json")
    if observed != expected:
        differences = {
            key: {"expected": expected[key], "observed": value}
            for key, value in observed.items()
            if expected[key] != value
        }
        raise ValueError(f"fresh runtime behavior differs from reviewed signature: {differences}")
    return {
        "bundle": bundle_path.relative_to(evidence_root).as_posix(),
        "decision_sha256": decision_sha256,
        "source_data_sha256": bundle.input_evidence.source_data_sha256,
        "data_fidelity_observed": binding.data_fidelity,
        "runtime_fidelity_observed": (
            "R2_independent_strict"
            if str(report.sandbox_mode) == "strict_container"
            else "R1_unified_contract"
        ),
        "tests_executed": sorted(binding.verification_tests),
        "metrics": {
            "decisions": report.metrics.decisions,
            "orders": report.metrics.orders,
            "fills": report.metrics.fills,
        },
    }


def verify_strategy_bindings(
    repository: Path, *, evidence_root: Path | None = None
) -> dict[str, Any]:
    """Rebuild every source, claim and code binding instead of trusting catalog prose."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        sources = read_source_registry(repository / "papers/source-registry.json")
        bindings = read_bindings(repository / "papers/bindings")
        packages = discover_strategy_packages(repository / "strategies")
    except Exception as exc:
        return {"status": "failed", "errors": [str(exc)], "bindings": []}

    source_by_id = {source.source_id: source for source in sources}
    package_by_id = {package.manifest.strategy_id: package for package in packages}
    try:
        executed_tests = (
            _executed_pytest_nodes(evidence_root / "junit.xml")
            if evidence_root is not None
            else set()
        )
    except Exception as exc:
        errors.append(f"runtime test evidence: {exc}")
        executed_tests = set()
    documents = {}
    for source in sources:
        try:
            path = repository / "papers/sources" / f"{source.source_id}.pdf"
            if digest(path.read_bytes()) != source.source_sha256:
                raise ValueError("source bytes differ from registry hash")
            document = ingest(path, source_uri=source.source_url)
            if document.source_sha256 != source.source_sha256:
                raise ValueError("parsed document hash differs from registry")
            documents[source.source_id] = document
        except Exception as exc:
            errors.append(f"source {source.source_id}: {exc}")

    for binding in bindings:
        try:
            source = source_by_id[binding.source_id]
            document = documents[binding.source_id]
            package = package_by_id[binding.strategy_id]
            if package.manifest.kind != binding.strategy_kind:
                raise ValueError("strategy kind differs from package manifest")
            for claim in binding.claims:
                if claim.page > len(document.pages):
                    raise ValueError(f"claim {claim.claim_id} page is absent")
                page = document.pages[claim.page - 1]
                if normalize(claim.anchor) not in normalize(page.text):
                    raise ValueError(f"claim {claim.claim_id} anchor is absent")
            for item in binding.implementation:
                path = (repository / item.path).resolve()
                if not path.is_relative_to(repository.resolve()) or not path.is_file():
                    raise ValueError(f"implementation path is absent or unsafe: {item.path}")
                payload = path.read_bytes()
                if sha256(payload).hexdigest() != item.sha256:
                    raise ValueError(f"implementation hash differs: {item.path}")
                source_text = payload.decode("utf-8")
                missing = [symbol for symbol in item.symbols if symbol not in source_text]
                if missing:
                    raise ValueError(f"implementation symbols absent: {item.path}: {missing}")
            for node_id in binding.verification_tests:
                _validate_test_node(repository, node_id)
            behavior = (
                _runtime_behavior(binding, package, evidence_root, executed_tests)
                if evidence_root is not None
                else None
            )
            results.append(
                {
                    "strategy_id": binding.strategy_id,
                    "kind": binding.strategy_kind,
                    "source_id": source.source_id,
                    "source_sha256": source.source_sha256,
                    "fidelity": binding.fidelity,
                    "claim_count": len(binding.claims),
                    "claim_link_count": len(binding.claim_links),
                    "runtime_behavior": behavior,
                    "runtime_verified": behavior is not None,
                    "status": "passed",
                }
            )
        except Exception as exc:
            results.append(
                {"strategy_id": binding.strategy_id, "status": "failed", "reason": str(exc)}
            )

    expected_ids = set(package_by_id)
    actual_ids = {binding.strategy_id for binding in bindings}
    kinds = Counter(str(binding.strategy_kind) for binding in bindings)
    fidelities = Counter(binding.fidelity for binding in bindings)
    reproduction_by_kind = Counter(str(binding.strategy_kind) for binding in bindings)
    exact_by_kind = Counter(
        str(binding.strategy_kind)
        for binding in bindings
        if binding.algorithm_fidelity == "A2_algorithm_exact"
    )
    exact_count = sum(binding.algorithm_fidelity == "A2_algorithm_exact" for binding in bindings)
    data_fidelities = Counter(binding.data_fidelity for binding in bindings)
    experimental_fidelities = Counter(binding.experimental_fidelity for binding in bindings)
    declared_runtime_fidelities = Counter(binding.runtime_fidelity for binding in bindings)
    public_by_kind = Counter(
        str(binding.strategy_kind)
        for binding in bindings
        if binding.data_fidelity != "D0_synthetic"
    )
    observed_runtime_fidelities = Counter(
        str(result["runtime_behavior"]["runtime_fidelity_observed"])
        for result in results
        if isinstance(result.get("runtime_behavior"), dict)
    )
    reproduction_count = len(bindings)
    coverage_ok = (
        len(bindings) == 18
        and actual_ids == expected_ids
        and kinds == Counter({kind.value: 6 for kind in StrategyKind})
        and all(result["status"] == "passed" for result in results)
        and len({binding.source_id for binding in bindings}) >= 15
        and fidelities["formula_reproduction"] >= 6
        and reproduction_count == 18
        and exact_count >= 9
        and data_fidelities["D1_public_proxy"] >= 6
        and all(public_by_kind[kind.value] >= 2 for kind in StrategyKind)
        and experimental_fidelities == Counter({"E0_runtime_only": 18})
        and (
            evidence_root is None
            or all(result.get("runtime_verified") is True for result in results)
        )
    )
    return {
        "status": "passed" if coverage_ok and not errors else "failed",
        "strategy_count": len(bindings),
        "counts_by_kind": dict(kinds),
        "source_count": len({binding.source_id for binding in bindings}),
        "fidelity_counts": dict(fidelities),
        "reproduction_count": reproduction_count,
        "reproduction_by_kind": dict(reproduction_by_kind),
        "algorithm_exact_count": exact_count,
        "algorithm_exact_by_kind": dict(exact_by_kind),
        "data_fidelity_counts": dict(data_fidelities),
        "public_data_by_kind": dict(public_by_kind),
        "experimental_fidelity_counts": dict(experimental_fidelities),
        "declared_runtime_fidelity_counts": dict(declared_runtime_fidelities),
        "observed_runtime_fidelity_counts": dict(observed_runtime_fidelities),
        "policy": {
            "minimum_distinct_sources": 15,
            "minimum_formula_reproductions": 6,
            "required_reproductions": 18,
            "maximum_method_adaptations": 0,
            "minimum_algorithm_exact": 9,
            "minimum_public_data_bindings": 6,
            "minimum_public_data_bindings_per_kind": 2,
            "empirical_claims_allowed": False,
        },
        "coverage_is_one_to_one": actual_ids == expected_ids,
        "runtime_evidence_required": evidence_root is not None,
        "bindings": results,
        "errors": errors,
    }
