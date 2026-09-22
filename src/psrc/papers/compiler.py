from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import yaml

from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.hashing import canonical_json_bytes, sha256_model
from psrc.contract.models import ActionKind, DataKind, StrategyKind, StrategyManifest, TrainingMode
from psrc.examples.synthetic import manifest_for_events
from psrc.papers.data import paper_inputs
from psrc.papers.ingest import digest, fail, normalize
from psrc.papers.models import GroundedClaim, PaperDocument, PaperRecipe, ReproductionSpec
from psrc.runtime.package import _deterministic_yaml_value
from psrc.runtime.training import build_training_input_evidence
from psrc.strategies.common import bar_requirement, event_requirement, make_manifest

PARAMETERS = {
    "moving_average_band": {"short_window", "long_window", "band"},
    "avellaneda_stoikov": {"gamma", "sigma", "k", "tick", "inventory_limit", "horizon_events"},
    "queue_logistic": {"iterations", "tolerance", "trade_threshold"},
    "execution_dynamic_q": {"horizon_events", "target_quantity", "tick"},
}
CLASSES = {
    "moving_average_band": "MovingAverageBandStrategy",
    "avellaneda_stoikov": "AvellanedaStrategy",
    "queue_logistic": "QueueLogisticStrategy",
    "execution_dynamic_q": "ExecutionQStrategy",
}


def read_recipe(path: Path) -> PaperRecipe:
    try:
        return PaperRecipe.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(ErrorCode.PAPER_RECIPE_INVALID, "Invalid or missing paper recipe", cause=str(exc))


def validate_parameters(recipe: PaperRecipe) -> None:
    p = recipe.parameters
    if set(p) != PARAMETERS[recipe.algorithm] or any(
        not math.isfinite(v) or v <= 0 for v in p.values()
    ):
        fail(
            ErrorCode.PAPER_RECIPE_INVALID,
            "Algorithm parameters must be exact, finite and positive",
        )
    for name in (
        "horizon_events",
        "target_quantity",
        "iterations",
        "inventory_limit",
        "short_window",
        "long_window",
    ):
        if name in p and (int(p[name]) != p[name] or p[name] > 1000):
            fail(
                ErrorCode.PAPER_RECIPE_INVALID,
                "Integer parameter outside compiler resource bounds",
                field=name,
            )
    if recipe.algorithm == "execution_dynamic_q" and (
        p["horizon_events"] > 16 or p["target_quantity"] > 20
    ):
        fail(ErrorCode.PAPER_RECIPE_INVALID, "Execution state-space exceeds compiler budget")
    if recipe.algorithm == "moving_average_band" and (
        p["short_window"] >= p["long_window"] or p["band"] >= 1
    ):
        fail(ErrorCode.PAPER_RECIPE_INVALID, "Moving-average windows/band are invalid")
    if recipe.algorithm == "queue_logistic" and not 0.5 <= p["trade_threshold"] < 1:
        fail(ErrorCode.PAPER_RECIPE_INVALID, "Trading threshold must be in [0.5,1)")


def extract_spec(document: PaperDocument, recipe: PaperRecipe) -> ReproductionSpec:
    validate_parameters(recipe)
    if document.source_sha256 != recipe.source_sha256:
        fail(
            ErrorCode.PAPER_SOURCE_MISMATCH,
            "Paper bytes do not match the reviewed recipe",
            expected=recipe.source_sha256,
            actual=document.source_sha256,
        )
    evidence = []
    for claim in recipe.claims:
        if claim.page > len(document.pages):
            fail(
                ErrorCode.PAPER_EVIDENCE_MISSING, "Referenced page is absent", claim=claim.claim_id
            )
        page = document.pages[claim.page - 1]
        if page.number != claim.page or digest(page.text.encode()) != page.sha256:
            fail(
                ErrorCode.PAPER_EVIDENCE_MISSING,
                "Parsed page evidence was modified",
                claim=claim.claim_id,
            )
        text, anchor = normalize(page.text), normalize(claim.anchor)
        start = text.find(anchor)
        if start < 0:
            fail(
                ErrorCode.PAPER_EVIDENCE_MISSING,
                "Evidence anchor is absent from source page",
                claim=claim.claim_id,
                page=claim.page,
            )
        evidence.append(
            GroundedClaim(
                claim=claim,
                normalized_start=start,
                normalized_end=start + len(anchor),
                page_sha256=page.sha256,
            )
        )
    return ReproductionSpec(
        recipe=recipe,
        recipe_sha256=sha256_model(recipe),
        document_sha256=sha256_model(document),
        evidence=tuple(evidence),
    )


def manifest_for_recipe(recipe: PaperRecipe) -> StrategyManifest:
    if recipe.algorithm == "moving_average_band":
        return make_manifest(
            strategy_id=f"paper.{recipe.paper_id}",
            kind=recipe.strategy_kind,
            entrypoint="strategy.py:Strategy",
            profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
            data=(
                bar_requirement(
                    interval="P1D",
                    symbols=("PUBLIC.AAPL",),
                    lookback=int(recipe.parameters["long_window"]),
                ),
            ),
            actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
            training=TrainingMode.NOT_REQUIRED,
            max_position=Decimal(1),
            max_order=Decimal(2),
        )
    actions = {ActionKind.NO_OP}
    profiles = {"event.l1.v1", "execution.basic.v1"}
    if recipe.strategy_kind == StrategyKind.SUPERVISED:
        actions |= {ActionKind.PREDICTION, ActionKind.TARGET_POSITION}
        profiles.add("training.supervised.v1")
    else:
        actions |= {ActionKind.SUBMIT_ORDER, ActionKind.CANCEL_ORDER}
        profiles.add("execution.advanced.v1")
    if recipe.strategy_kind == StrategyKind.REINFORCEMENT_LEARNING:
        profiles.add("training.rl.v1")
    limit = recipe.parameters.get("inventory_limit", recipe.parameters.get("target_quantity", 1))
    return make_manifest(
        strategy_id=f"paper.{recipe.paper_id}",
        kind=recipe.strategy_kind,
        entrypoint="strategy.py:Strategy",
        profiles=frozenset(profiles),
        data=(
            event_requirement(
                stream_id="quotes",
                kind=DataKind.QUOTE_L1,
                symbols=("PAPER.SIM",),
                fields=frozenset({"bid_price", "bid_size", "ask_price", "ask_size"}),
            ),
        ),
        actions=frozenset(actions),
        max_position=Decimal(str(limit)),
        max_order=(
            Decimal(str(limit * 2))
            if recipe.strategy_kind == StrategyKind.SUPERVISED
            else Decimal(str(limit))
        ),
        training=TrainingMode.NOT_REQUIRED
        if recipe.strategy_kind == StrategyKind.RULE
        else TrainingMode.REQUIRED,
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def compile_paper(document: PaperDocument, recipe: PaperRecipe, output: Path) -> ReproductionSpec:
    spec = extract_spec(document, recipe)
    manifest = manifest_for_recipe(recipe)
    events, training, split, data_evidence = paper_inputs(recipe)
    dataset = manifest_for_events(
        dataset_id=f"paper.{recipe.paper_id}.holdout",
        stream_id=manifest.data_requirements[0].stream_id,
        kind=manifest.data_requirements[0].kind,
        timeframe=manifest.data_requirements[0].timeframe,
        fields=manifest.data_requirements[0].required_fields,
        events=events,
    ).model_copy(
        update={
            "public_or_synthetic": True,
            "extensions": {
                "org.singularityx.data-provenance": data_evidence.model_dump(mode="json")
            },
        }
    )
    # Trusted templates only; no paper text, recipe text or arbitrary Python is evaluated.
    code = (
        "# Generated by psrc-paper-compiler.v1; inspect reproduction-spec.json for provenance.\n"
        f"from psrc.strategy_api import {CLASSES[recipe.algorithm]}, StrategyManifest\n\n\n"
        f"class Strategy({CLASSES[recipe.algorithm]}):\n"
        "    manifest = StrategyManifest.model_validate_json(\n"
        f"        {canonical_json_bytes(manifest).decode()!r}\n"
        "    )\n"
        f"    parameters = {dict(sorted(recipe.parameters.items()))!r}\n"
    )
    try:
        if output.exists() and any(output.iterdir()):
            fail(
                ErrorCode.PAPER_COMPILATION_FAILED,
                "Compile output must be empty; refusing stale package reuse",
            )
        output.mkdir(parents=True, exist_ok=True)
        (output / "strategy.py").write_text(code, encoding="utf-8")
        (output / "strategy.yaml").write_text(
            yaml.safe_dump(
                _deterministic_yaml_value(manifest.model_dump(mode="python")), sort_keys=False
            ),
            encoding="utf-8",
        )
        write_json(output / "dataset-manifest.json", json.loads(canonical_json_bytes(dataset)))
        write_json(output / "input-events.json", [e.model_dump(mode="json") for e in events])
        if training is not None:
            write_json(output / "training-request.json", training.model_dump(mode="json"))
            write_json(
                output / "training-input-evidence.json",
                build_training_input_evidence(training).model_dump(mode="json"),
            )
        write_json(output / "reproduction-spec.json", spec.model_dump(mode="json"))
        write_json(output / "split-evidence.json", split)
        write_json(output / "data-source-evidence.json", data_evidence.model_dump(mode="json"))
        write_json(
            output / "build-manifest.json",
            {
                "compiler": spec.compiler,
                "recipe_sha256": spec.recipe_sha256,
                "source_sha256": document.source_sha256,
                "files": {
                    p.name: digest(p.read_bytes()) for p in sorted(output.iterdir()) if p.is_file()
                },
            },
        )
    except ContractViolation:
        raise
    except Exception as exc:
        fail(
            ErrorCode.PAPER_COMPILATION_FAILED,
            "Failed to materialize strategy package",
            cause=str(exc),
        )
    return spec
