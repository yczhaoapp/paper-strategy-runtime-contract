from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from psrc.adapters.reference import ReferenceEngine, capabilities
from psrc.contract.compiler import compile_run
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.hashing import sha256_model
from psrc.contract.models import ActionKind, ActionRequirements, RunPolicy, SandboxMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, NoOp, ReplaceOrder, SubmitOrder, TargetPosition
from psrc.domain.market import MarketEvent, QuoteL1Payload
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.examples.synthetic import minute_bar_manifest, minute_bars
from psrc.runtime.guards import validate_dataset_events


def _run(strategy: SmaCrossStrategy) -> None:
    events = minute_bars()
    plan = compile_run(
        run_id="test.runtime-enforcement",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        engine=capabilities(),
        policy=RunPolicy(required_sandbox=SandboxMode.DEVELOPMENT),
    )
    ReferenceEngine().run(
        plan=plan,
        strategy=strategy,
        events=events,
        sandbox_mode=SandboxMode.DEVELOPMENT,
    )


def test_reference_rejects_action_not_declared_by_strategy() -> None:
    class Undeclared(SmaCrossStrategy):
        manifest = SmaCrossStrategy.manifest.model_copy(
            update={
                "action_requirements": ActionRequirements(
                    allowed=frozenset({ActionKind.NO_OP}), max_abs_position=Decimal("1")
                )
            }
        )

        def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
            del account
            return (
                TargetPosition(
                    instrument_id=event.instrument_id,
                    quantity=Decimal("1"),
                    reason_code="test.undeclared",
                ),
            )

    with pytest.raises(ContractViolation) as caught:
        _run(Undeclared())
    assert caught.value.error.code == ErrorCode.ACTION_INVALID


def test_reference_revalidates_replacement_quantity() -> None:
    class OversizedReplacement(SmaCrossStrategy):
        manifest = SmaCrossStrategy.manifest.model_copy(
            update={
                "action_requirements": ActionRequirements(
                    allowed=frozenset(
                        {ActionKind.NO_OP, ActionKind.SUBMIT_ORDER, ActionKind.REPLACE_ORDER}
                    ),
                    max_abs_position=Decimal("1"),
                    max_order_quantity=Decimal("1"),
                )
            }
        )

        def on_start(self) -> None:
            self.index = 0

        def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
            del account
            self.index += 1
            if self.index == 1:
                return (
                    SubmitOrder(
                        client_order_id="test.order",
                        instrument_id=event.instrument_id,
                        side="buy",
                        order_type="limit",
                        quantity=Decimal("1"),
                        limit_price=Decimal("1"),
                        reason_code="test.submit",
                    ),
                )
            if self.index == 2:
                return (
                    ReplaceOrder(
                        client_order_id="test.order",
                        new_quantity=Decimal("1000"),
                        new_limit_price=Decimal("1000"),
                        reason_code="test.replace",
                    ),
                )
            return (NoOp(reason_code="test.done", explanation="done"),)

    with pytest.raises(ContractViolation) as caught:
        _run(OversizedReplacement())
    assert caught.value.error.code == ErrorCode.ORDER_REJECTED


def test_reference_rejects_unsupported_time_in_force() -> None:
    class UnsupportedIoc(SmaCrossStrategy):
        manifest = SmaCrossStrategy.manifest.model_copy(
            update={
                "action_requirements": ActionRequirements(
                    allowed=frozenset({ActionKind.SUBMIT_ORDER}),
                    max_order_quantity=Decimal("1"),
                )
            }
        )

        def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
            del account
            return (
                SubmitOrder(
                    client_order_id="test.ioc",
                    instrument_id=event.instrument_id,
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("1"),
                    limit_price=Decimal("1"),
                    time_in_force="ioc",
                    reason_code="test.ioc",
                ),
            )

    with pytest.raises(ContractViolation) as caught:
        _run(UnsupportedIoc())
    assert caught.value.error.code == ErrorCode.ORDER_REJECTED


def test_actual_payload_kind_must_match_dataset_manifest() -> None:
    strategy = SmaCrossStrategy()
    events = tuple(
        event.model_copy(
            update={
                "payload": QuoteL1Payload(
                    bid_price=Decimal("100"),
                    bid_size=Decimal("10"),
                    ask_price=Decimal("101"),
                    ask_size=Decimal("10"),
                )
            }
        )
        for event in minute_bars()
    )
    dataset = minute_bar_manifest(events).model_copy(
        update={
            "streams": (
                minute_bar_manifest(events)
                .streams[0]
                .model_copy(
                    update={
                        "kind": "bar",
                        "fields": frozenset({"open", "high", "low", "close", "volume"}),
                        "data_sha256": sha256_model(
                            {"events": [event.model_dump(mode="json") for event in events]}
                        ),
                    }
                ),
            )
        }
    )
    with pytest.raises(ContractViolation) as caught:
        validate_dataset_events(
            run_id="test.dataset-kind",
            strategy=strategy.manifest,
            dataset=dataset,
            events=events,
        )
    assert caught.value.error.code == ErrorCode.DATA_KIND_MISMATCH


def test_ambiguous_multi_stream_input_fails_closed() -> None:
    strategy = SmaCrossStrategy()
    events = minute_bars()
    dataset = minute_bar_manifest(events)
    dataset = dataset.model_copy(update={"streams": (*dataset.streams, dataset.streams[0])})
    with pytest.raises(ContractViolation) as caught:
        validate_dataset_events(
            run_id="test.multi-stream",
            strategy=strategy.manifest,
            dataset=dataset,
            events=events,
        )
    assert caught.value.error.code == ErrorCode.DATA_STREAM_MISSING


def test_actual_bar_timestamps_must_match_declared_epoch_grid() -> None:
    strategy = SmaCrossStrategy()
    events = tuple(
        event.model_copy(
            update={
                "event_time": event.event_time + timedelta(seconds=30),
                "available_time": event.available_time + timedelta(seconds=30),
                "receive_time": event.receive_time + timedelta(seconds=30),
            }
        )
        for event in minute_bars()
    )
    dataset = minute_bar_manifest(events)
    with pytest.raises(ContractViolation) as caught:
        validate_dataset_events(
            run_id="test.bar-alignment",
            strategy=strategy.manifest,
            dataset=dataset,
            events=events,
        )
    assert caught.value.error.code == ErrorCode.DATA_TIMEFRAME_MISMATCH
    assert caught.value.error.details["alignment"] == "epoch"
    assert caught.value.error.details["gaps_allowed"] is True


def test_aligned_missing_bars_are_allowed() -> None:
    strategy = SmaCrossStrategy()
    events = tuple(
        event.model_copy(
            update={
                "event_time": event.event_time + timedelta(minutes=index),
                "available_time": event.available_time + timedelta(minutes=index),
                "receive_time": event.receive_time + timedelta(minutes=index),
            }
        )
        for index, event in enumerate(minute_bars())
    )
    validate_dataset_events(
        run_id="test.bar-gaps",
        strategy=strategy.manifest,
        dataset=minute_bar_manifest(events),
        events=events,
    )
