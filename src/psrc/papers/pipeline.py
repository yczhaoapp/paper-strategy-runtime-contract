from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.domain.market import QuoteL1Payload
from psrc.papers.algorithms import probability
from psrc.papers.compiler import compile_paper, read_recipe, write_json
from psrc.papers.data import labeled_observations
from psrc.papers.ingest import fail, ingest
from psrc.runtime.package import load_strategy_manifest


def reproduce(
    source: Path, recipe_path: Path, output: Path, *, require_strict: bool = False
) -> dict[str, Any]:
    from psrc.cli import _load_package_inputs, _run_package, _selected_sandbox

    try:
        _selected_sandbox(require_strict=require_strict)
        recipe = read_recipe(recipe_path)
        document = ingest(source, source_uri=recipe.source_url)
        spec = compile_paper(document, recipe, output / "package")
        write_json(output / "document.json", document.model_dump(mode="json"))
        package = load_strategy_manifest(output / "package")
        report = _run_package(
            package,
            output / "runtime",
            require_strict=require_strict,
            engine_id=recipe.execution_engine,
        )
        _, events, _, _ = _load_package_inputs(package)
        data_evidence = json.loads(
            (output / "package/data-source-evidence.json").read_text(encoding="utf-8")
        )
        diagnostics: dict[str, Any] = {
            "data_origin": data_evidence["origin"],
            "empirical_paper_claims": "not_verified",
        }
        if recipe.algorithm == "queue_logistic":
            features, labels, _ = labeled_observations(events)
            artifact = report.artifacts[0]
            model = json.loads(
                (
                    output / "runtime/artifact-store" / artifact.artifact_id / "model.json"
                ).read_text()
            )
            probabilities = [
                probability(model["intercept"], model["slope"], x[0]) for x in features
            ]
            diagnostics.update(
                {
                    "holdout_brier": sum(
                        (p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)
                    )
                    / len(labels),
                    "null_brier": 0.25,
                    "holdout_count": len(labels),
                    "accuracy": sum(
                        (p > 0.5) == y for p, y in zip(probabilities, labels, strict=True)
                    )
                    / len(labels),
                    "model": model,
                }
            )
        elif recipe.algorithm == "execution_dynamic_q":
            first = events[0].payload
            assert isinstance(first, QuoteL1Payload)
            benchmark = (first.bid_price + first.ask_price) / 2
            volume = sum(f.quantity for f in report.fills)
            total = sum(f.quantity * f.price + f.fee for f in report.fills)
            diagnostics.update(
                {
                    "completed_quantity": float(volume),
                    "target_quantity": recipe.parameters["target_quantity"],
                    "implementation_shortfall": float(total - volume * benchmark),
                }
            )
            if float(volume) != recipe.parameters["target_quantity"]:
                raise ValueError("execution target not completed")
        bundle = json.loads((output / "runtime/bundle.json").read_text(encoding="utf-8"))
        result = {
            "status": report.status,
            "paper_id": recipe.paper_id,
            "kind": recipe.strategy_kind,
            "spec": spec.model_dump(mode="json"),
            "source_sha256": document.source_sha256,
            "bundle_sha256": sha256_model(bundle),
            "runtime_bundle": bundle,
            "diagnostics": diagnostics,
            "data_evidence": data_evidence,
            "split": json.loads((output / "package/split-evidence.json").read_text()),
        }
        write_json(output / "paper-run.json", result)
        content = html.escape(
            json.dumps(
                {
                    "paper": recipe.title,
                    "status": result["status"],
                    "fidelity": recipe.fidelity,
                    "diagnostics": diagnostics,
                    "assumptions": recipe.assumptions,
                    "deviations": recipe.deviations,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        (output / "index.html").write_text(
            '<!doctype html><html lang="zh"><meta charset="utf-8"><title>论文复现证据</title>'
            "<style>body{max-width:1000px;margin:3rem auto;font:16px system-ui;padding:1rem}"
            "pre{white-space:pre-wrap;background:#f3f5f8;padding:2rem}a{color:#176868}</style>"
            f"<h1>论文到运行时的复现证据</h1><pre>{content}</pre>"
            '<a href="runtime/report.html">查看训练、推理、订单与回测报告</a></html>',
            encoding="utf-8",
        )
        return result
    except ContractViolation as exc:
        write_json(output / "failure.json", exc.error.model_dump(mode="json"))
        raise
    except Exception as exc:
        error = ContractError(
            run_id="paper.pipeline",
            stage=ErrorStage.REPORTING,
            code=ErrorCode.PAPER_COMPILATION_FAILED,
            message="Paper reproduction could not complete its evidence",
            cause_chain=(f"{type(exc).__name__}: {exc}",),
        )
        write_json(output / "failure.json", error.model_dump(mode="json"))
        raise ContractViolation(error) from exc


def run_suite(
    sources: Path, recipes: Path, output: Path, *, require_strict: bool = False
) -> dict[str, Any]:
    runs = []
    for path in sorted(recipes.glob("*.json")):
        recipe = read_recipe(path)
        result = reproduce(
            sources / f"{recipe.paper_id}.pdf",
            path,
            output / recipe.paper_id,
            require_strict=require_strict,
        )
        runs.append(
            {
                "paper_id": recipe.paper_id,
                "kind": recipe.strategy_kind,
                "status": result["status"],
                "source_sha256": result["source_sha256"],
                "report_sha256": sha256_model(result),
            }
        )
    if not runs:
        fail(ErrorCode.PAPER_RECIPE_INVALID, "no paper recipes found")
    summary = {
        "status": "passed",
        "paper_count": len(runs),
        "runs": runs,
        "fidelity": "algorithm_reproduction",
        "empirical_results_reproduced": False,
    }
    write_json(output / "summary.json", summary)
    return summary
