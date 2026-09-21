from __future__ import annotations

from psrc.contract.compiler import compile_run
from psrc.contract.models import (
    DatasetManifest,
    EngineCapabilities,
    ExecutionPlan,
    RunPolicy,
    StrategyManifest,
)


def test_v110_parser_accepts_v100_execution_plan_without_source_evidence(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    plan = compile_run(
        run_id="evolution.v100-plan",
        strategy=strategy,
        dataset=dataset,
        engine=engine,
        policy=policy,
    )
    v100_document = plan.model_dump(mode="json", exclude_none=True)
    v100_document["contract_version"] = "1.0.0"
    parsed = ExecutionPlan.model_validate(v100_document)
    assert parsed.contract_version == "1.0.0"
    assert parsed.strategy_code_evidence_sha256 is None


def test_v110_compiler_accepts_all_v100_major_contract_declarations(
    strategy: StrategyManifest,
    dataset: DatasetManifest,
    engine: EngineCapabilities,
    policy: RunPolicy,
) -> None:
    plan = compile_run(
        run_id="evolution.v100-declarations",
        strategy=strategy.model_copy(update={"contract_version": "1.0.0"}),
        dataset=dataset.model_copy(update={"contract_version": "1.0.0"}),
        engine=engine.model_copy(update={"contract_version": "1.0.0"}),
        policy=policy,
    )
    assert plan.strategy_id == strategy.strategy_id
