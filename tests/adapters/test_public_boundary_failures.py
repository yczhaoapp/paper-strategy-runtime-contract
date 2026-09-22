from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from psrc.adapters.backtrader import BacktraderAdapter
from psrc.adapters.backtrader import capabilities as backtrader_capabilities
from psrc.adapters.reference import ReferenceEngine
from psrc.adapters.reference import capabilities as reference_capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    ActionRequirements,
    EngineCapabilities,
    RunPolicy,
    SandboxMode,
)
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, TargetPosition
from psrc.domain.market import MarketEvent
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.runtime.training import build_training_input_evidence
from psrc.strategies.catalog import supervised_examples

EngineFactory = Callable[[], ReferenceEngine | BacktraderAdapter]
CapabilitiesFactory = Callable[[], EngineCapabilities]


@pytest.mark.parametrize(
    "engine_factory,capability_factory",
    [
        (ReferenceEngine, reference_capabilities),
        (BacktraderAdapter, backtrader_capabilities),
    ],
)
@pytest.mark.parametrize("failure_callback", ["on_start", "on_event", "on_finish"])
def test_public_adapter_wraps_unexpected_strategy_exceptions(
    engine_factory: EngineFactory,
    capability_factory: CapabilitiesFactory,
    failure_callback: str,
) -> None:
    class Exploding(SmaCrossStrategy):
        def on_start(self) -> None:
            if failure_callback == "on_start":
                raise RuntimeError("start exploded")
            super().on_start()

        def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
            if failure_callback == "on_event":
                raise RuntimeError("event exploded")
            return super().on_event(event, account)

        def on_finish(self) -> None:
            if failure_callback == "on_finish":
                raise RuntimeError("finish exploded")

    events = minute_bars()
    strategy = Exploding()
    plan = compile_run(
        run_id=f"test.adapter-error-{failure_callback}",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capability_factory(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    with pytest.raises(ContractViolation) as raised:
        engine_factory().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )
    assert raised.value.error.code == ErrorCode.BACKTEST_FAILED
    assert raised.value.error.cause_chain


@pytest.mark.parametrize(
    "engine_factory,capability_factory",
    [
        (ReferenceEngine, reference_capabilities),
        (BacktraderAdapter, backtrader_capabilities),
    ],
)
@pytest.mark.parametrize("scenario", ["duplicate-target", "derived-order-limit"])
def test_target_batches_and_derived_native_orders_enforce_limits(
    engine_factory: EngineFactory,
    capability_factory: CapabilitiesFactory,
    scenario: str,
) -> None:
    class TargetProbe(SmaCrossStrategy):
        def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
            del account
            target = TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal(10 if scenario == "derived-order-limit" else 1),
                reason_code="test.target-boundary",
            )
            return (target, target) if scenario == "duplicate-target" else (target,)

    events = minute_bars()
    strategy = TargetProbe()
    current = strategy.manifest.action_requirements
    strategy.manifest = strategy.manifest.model_copy(
        update={
            "action_requirements": ActionRequirements(
                allowed=current.allowed,
                max_abs_position=Decimal(10),
                max_order_quantity=Decimal(1),
            )
        }
    )
    plan = compile_run(
        run_id=f"test.target-boundary-{scenario}",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capability_factory(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    with pytest.raises(ContractViolation) as raised:
        engine_factory().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )
    assert raised.value.error.code == ErrorCode.ORDER_REJECTED


@pytest.mark.parametrize(
    "engine_factory,capability_factory",
    [
        (ReferenceEngine, reference_capabilities),
        (BacktraderAdapter, backtrader_capabilities),
    ],
)
def test_public_adapter_checks_concrete_payload_fields(
    engine_factory: EngineFactory,
    capability_factory: CapabilitiesFactory,
) -> None:
    events = minute_bars()
    strategy = SmaCrossStrategy()
    requirement = strategy.manifest.data_requirements[0].model_copy(
        update={
            "required_fields": strategy.manifest.data_requirements[0].required_fields
            | {"nonexistent_field"}
        }
    )
    strategy.manifest = strategy.manifest.model_copy(update={"data_requirements": (requirement,)})
    dataset = minute_bar_manifest(events)
    stream = dataset.streams[0].model_copy(
        update={"fields": dataset.streams[0].fields | {"nonexistent_field"}}
    )
    dataset = dataset.model_copy(update={"streams": (stream,)})
    plan = compile_run(
        run_id="test.actual-fields",
        strategy=strategy.manifest,
        dataset=dataset,
        engine=capability_factory(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    with pytest.raises(ContractViolation) as raised:
        engine_factory().run(
            plan=plan,
            strategy=strategy,
            events=events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )
    assert raised.value.error.code == ErrorCode.DATA_FIELD_MISSING


def test_public_adapter_checks_concrete_l2_depth() -> None:
    example = supervised_examples()[4]
    strategy = example.factory()
    requirement = strategy.manifest.data_requirements[0].model_copy(update={"depth": 100})
    strategy.manifest = strategy.manifest.model_copy(update={"data_requirements": (requirement,)})
    plan = compile_run(
        run_id="test.actual-l2-depth",
        strategy=strategy.manifest,
        dataset=example.dataset,
        engine=reference_capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
        training_input_evidence_sha256=sha256_model(
            build_training_input_evidence(example.training)
        ),
    )
    with pytest.raises(ContractViolation) as raised:
        ReferenceEngine().run(
            plan=plan,
            strategy=strategy,
            events=example.events,
            sandbox_mode=SandboxMode.DEVELOPMENT,
        )
    assert raised.value.error.code == ErrorCode.DATA_FIELD_MISSING
