from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from psrc.contract.hashing import sha256_model
from psrc.papers.compiler import compile_paper, extract_spec, read_recipe
from psrc.papers.ingest import digest, ingest
from psrc.papers.models import DataSourceEvidence, ReproductionSpec
from psrc.runtime.package import load_strategy_manifest
from psrc.runtime.report import RunBundle


def verify_papers(root: Path, sources: Path, recipes: Path) -> dict[str, Any]:
    """Reconstruct provenance and generated code; never accept status flags alone."""
    results = []
    paths = sorted(recipes.glob("*.json"))
    kinds = set()
    origins = set()
    for path in paths:
        recipe = read_recipe(path)
        kinds.add(recipe.strategy_kind)
        try:
            run_root = root / recipe.paper_id
            result = json.loads((run_root / "paper-run.json").read_text(encoding="utf-8"))
            document = ingest(sources / f"{recipe.paper_id}.pdf", source_uri=recipe.source_url)
            expected_spec = extract_spec(document, recipe)
            observed_spec = ReproductionSpec.model_validate(result["spec"])
            if observed_spec != expected_spec:
                raise ValueError("paper source/spec/claim provenance differs")
            with tempfile.TemporaryDirectory(prefix="paper-audit-") as temp:
                target = Path(temp) / "package"
                compile_paper(document, recipe, target)
                expected_files = {p.name: digest(p.read_bytes()) for p in target.iterdir()}
                actual_files = {
                    p.name: digest(p.read_bytes())
                    for p in (run_root / "package").iterdir()
                    if p.is_file()
                }
                if expected_files != actual_files:
                    raise ValueError("generated package differs from independent recompilation")
            raw_bundle = json.loads((run_root / "runtime/bundle.json").read_text(encoding="utf-8"))
            bundle = RunBundle.model_validate(raw_bundle)
            if result["runtime_bundle"] != raw_bundle or result["bundle_sha256"] != sha256_model(
                raw_bundle
            ):
                raise ValueError("paper/runtime bundle binding differs")
            package = load_strategy_manifest(run_root / "package")
            if bundle.strategy_code_evidence != package.code_evidence:
                raise ValueError("strategy/runtime source hash differs")
            report = bundle.report
            if report.execution_plan.engine_id != recipe.execution_engine:
                raise ValueError("paper execution engine differs from reviewed recipe")
            if report.status != "succeeded" or report.metrics.fills <= 0:
                raise ValueError("paper backtest has no verified fills")
            required = 0 if recipe.strategy_kind == "rule" else 1
            if len(report.artifacts) != required:
                raise ValueError("missing training artifact")
            for artifact in report.artifacts:
                for file in artifact.files:
                    payload = (
                        run_root
                        / "runtime/artifact-store"
                        / artifact.artifact_id
                        / file.logical_name
                    )
                    if digest(payload.read_bytes()) != file.sha256:
                        raise ValueError("paper training artifact was changed")
            if result["split"]["train_end"] >= result["split"]["test_start"]:
                raise ValueError("training and test timestamps overlap")
            split = json.loads((run_root / "package/split-evidence.json").read_text())
            if split != result["split"]:
                raise ValueError("split evidence differs from compiled package")
            data_evidence = DataSourceEvidence.model_validate(
                json.loads((run_root / "package/data-source-evidence.json").read_text())
            )
            if result["data_evidence"] != data_evidence.model_dump(mode="json"):
                raise ValueError("paper run differs from data-source evidence")
            origins.add(data_evidence.origin)
            results.append(
                {
                    "paper_id": recipe.paper_id,
                    "status": "passed",
                    "fills": report.metrics.fills,
                    "data_origin": data_evidence.origin,
                }
            )
        except Exception as exc:
            results.append({"paper_id": recipe.paper_id, "status": "failed", "reason": str(exc)})
    expected_ids = {"avellaneda2008", "gould2015", "ma2015", "nevmyvaka2006"}
    passed = {row["paper_id"] for row in results} == expected_ids and kinds == {
        "rule",
        "supervised",
        "reinforcement_learning",
    }
    passed = passed and all(row["status"] == "passed" for row in results)
    passed = passed and origins == {"public_historical", "synthetic_fixture"}
    return {
        "status": "passed" if passed else "failed",
        "papers": results,
        "verification": (
            "source reparse, spec rebuild, package recompile, runtime and artifact hashes"
        ),
        "data_origins": sorted(origins),
    }
