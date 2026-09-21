from __future__ import annotations

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.models import RunPolicy, SandboxMode, StrategyKind
from psrc.strategies.catalog import StrategyExample, rule_examples


def test_rule_catalog_contains_six_distinct_strategies() -> None:
    examples = rule_examples()
    assert len(examples) == 6
    assert len({example.manifest.strategy_id for example in examples}) == 6
    assert {example.manifest.kind for example in examples} == {StrategyKind.RULE}
    requirement_shapes = {
        tuple(
            (
                requirement.kind,
                requirement.timeframe.mode,
                requirement.timeframe.interval,
                tuple(sorted(requirement.symbols)),
            )
            for requirement in example.manifest.data_requirements
        )
        for example in examples
    }
    assert len(requirement_shapes) >= 5


@pytest.mark.parametrize("example", rule_examples(), ids=lambda item: item.manifest.strategy_id)
def test_every_rule_strategy_compiles_and_backtests(example: StrategyExample) -> None:
    strategy = example.factory()
    plan = compile_run(
        run_id=f"test.{strategy.manifest.strategy_id}",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    report = ReferenceEngine().run(
        plan=plan,
        strategy=strategy,
        events=example.events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )
    assert report.status == "succeeded"
    assert report.metrics.decisions == len(example.events)
