from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import TargetPosition
from psrc.domain.market import (
    BarPayload,
    BookLevel,
    BookSnapshotL2Payload,
    MarketEvent,
)
from psrc.runtime.artifacts import ArtifactManifest, ArtifactStore
from psrc.runtime.training import RLTransition, TrainingRequest
from psrc.strategies.reinforcement_learning import (
    DoubleQBookInventoryStrategy,
    SarsaTrendStrategy,
    TabularQInventoryStrategy,
)


def _state_key(state: tuple[float, ...]) -> str:
    """Independent transcription of the paper adapter's declared discretization."""
    return ",".join(str(max(-2, min(2, round(value)))) for value in state)


@st.composite
def _training_requests(draw: st.DrawFn, *, name: str) -> TrainingRequest:
    row = st.tuples(
        st.tuples(*(st.integers(-1, 1) for _ in range(3))),
        st.integers(0, 2),
        st.floats(
            min_value=-3,
            max_value=3,
            allow_nan=False,
            allow_infinity=False,
            width=32,
        ),
        st.tuples(*(st.integers(-1, 1) for _ in range(3))),
        st.integers(0, 2),
        st.booleans(),
    )
    rows = draw(st.lists(row, min_size=9, max_size=14))
    transitions = []
    for index, (state, action, reward, next_state, next_action, terminal) in enumerate(rows):
        # Every generated training request exercises both terminal and bootstrapped updates.
        terminated = True if index == 0 else False if index == 1 else terminal
        transitions.append(
            RLTransition(
                episode_id=f"episode-{index // 5}",
                step=index,
                state=tuple(float(value) for value in state),
                action=action,
                reward=reward,
                next_state=tuple(float(value) for value in next_state),
                next_action=next_action,
                terminated=terminated,
            )
        )
    return TrainingRequest(
        run_id=f"differential.{name}",
        dataset_id=f"generated.{name}",
        seed=draw(st.integers(0, 2**16)),
        transitions=tuple(transitions),
        metadata={"oracle": "independent-full-training"},
    )


def _reference_q_learning(request: TrainingRequest) -> dict[str, list[float]]:
    table: dict[str, list[float]] = {}
    for _ in range(25):
        for transition in request.transitions:
            state = _state_key(transition.state)
            next_state = _state_key(transition.next_state)
            table.setdefault(state, [0.0, 0.0, 0.0])
            table.setdefault(next_state, [0.0, 0.0, 0.0])
            current = table[state][transition.action]
            bootstrap = 0.0 if transition.terminated else 0.9 * max(table[next_state])
            table[state][transition.action] = current + 0.2 * (
                transition.reward + bootstrap - current
            )
    return table


def _reference_sarsa(request: TrainingRequest) -> dict[str, list[float]]:
    table: dict[str, list[float]] = {}
    for _ in range(20):
        for transition in request.transitions:
            state = _state_key(transition.state)
            next_state = _state_key(transition.next_state)
            table.setdefault(state, [0.0, 0.0, 0.0])
            table.setdefault(next_state, [0.0, 0.0, 0.0])
            current = table[state][transition.action]
            if transition.terminated:
                bootstrap = 0.0
            else:
                assert transition.next_action is not None
                bootstrap = 0.85 * table[next_state][transition.next_action]
            table[state][transition.action] = current + 0.15 * (
                transition.reward + bootstrap - current
            )
    return table


def _reference_double_q(request: TrainingRequest) -> dict[str, list[float]]:
    q_a: dict[str, list[float]] = {}
    q_b: dict[str, list[float]] = {}
    for epoch in range(20):
        for index, transition in enumerate(request.transitions):
            state = _state_key(transition.state)
            next_state = _state_key(transition.next_state)
            for table in (q_a, q_b):
                table.setdefault(state, [0.0, 0.0, 0.0])
                table.setdefault(next_state, [0.0, 0.0, 0.0])
            update, evaluate = (q_a, q_b) if (epoch + index) % 2 == 0 else (q_b, q_a)
            greedy = max(range(3), key=lambda action: update[next_state][action])
            bootstrap = (
                0.0 if transition.terminated else 0.9 * evaluate[next_state][greedy]
            )
            current = update[state][transition.action]
            update[state][transition.action] = current + 0.18 * (
                transition.reward + bootstrap - current
            )
    return {
        state: [q_a[state][action] + q_b[state][action] for action in range(3)]
        for state in q_a
    }


def _load_policy(
    strategy: object,
    request: TrainingRequest,
    tmp_path: Path,
) -> tuple[ArtifactManifest, ArtifactStore, dict[str, object]]:
    store = ArtifactStore(tmp_path / request.request_sha256)
    artifact = strategy.train(request, store)  # type: ignore[attr-defined]
    payload = store.load_bytes(
        run_id=request.run_id,
        strategy_id=strategy.manifest.strategy_id,  # type: ignore[attr-defined]
        manifest=artifact,
    )["policy.json"]
    policy = json.loads(payload)
    assert artifact.training_request_sha256 == request.request_sha256
    assert artifact.training_dataset_id == request.dataset_id
    assert artifact.seed == request.seed
    return artifact, store, policy


def _assert_table_matches(
    actual: object, expected: dict[str, list[float]]
) -> None:
    assert isinstance(actual, dict)
    assert actual.keys() == expected.keys()
    for state, values in expected.items():
        assert actual[state] == pytest.approx(values, rel=1e-10, abs=1e-10)


def _expected_unseen_action(
    table: dict[str, list[float]], state: tuple[float, float, float]
) -> int:
    target = tuple(max(-2, min(2, round(value))) for value in state)

    def distance(key: str) -> tuple[float, str]:
        candidate = tuple(float(value) for value in key.split(","))
        squared = sum(
            (left - right) ** 2
            for left, right in zip(target, candidate, strict=True)
        )
        return squared, key

    nearest = min(table, key=distance)
    return max(range(3), key=lambda action: table[nearest][action])


def _account() -> AccountSnapshot:
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    return AccountSnapshot(
        timestamp=timestamp,
        cash=Decimal("100000"),
        equity=Decimal("100000"),
        positions=(),
    )


def _bar_event(*, instrument_id: str) -> MarketEvent:
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    return MarketEvent(
        event_id=f"{instrument_id}.unseen",
        instrument_id=instrument_id,
        event_time=timestamp,
        available_time=timestamp,
        receive_time=timestamp,
        sequence=1,
        source="generated-oracle",
        payload=BarPayload(
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("102"),
            volume=Decimal("1000"),
        ),
    )


def _book_event() -> MarketEvent:
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    return MarketEvent(
        event_id="SYNTH.L2.unseen",
        instrument_id="SYNTH.L2",
        event_time=timestamp,
        available_time=timestamp,
        receive_time=timestamp,
        sequence=1,
        source="generated-oracle",
        payload=BookSnapshotL2Payload(
            bids=(BookLevel(price=Decimal("99.9"), size=Decimal("100")),),
            asks=(BookLevel(price=Decimal("100.1"), size=Decimal("0")),),
        ),
    )


def _assert_public_unseen_inference(
    *,
    strategy_factory: Callable[[], object],
    artifact: ArtifactManifest,
    store: ArtifactStore,
    request: TrainingRequest,
    policy: dict[str, object],
    event: MarketEvent,
    inference_state: tuple[float, float, float],
    position_scale: int,
) -> None:
    table = policy["q"]
    assert isinstance(table, dict)
    assert _state_key(inference_state) not in table
    typed_table = {key: list(values) for key, values in table.items()}
    expected_action = _expected_unseen_action(typed_table, inference_state)

    reloaded = strategy_factory()
    reloaded.load(artifact, store, run_id=request.run_id)  # type: ignore[attr-defined]
    actions = reloaded.on_event(event, _account())  # type: ignore[attr-defined]
    assert len(actions) == 1
    assert isinstance(actions[0], TargetPosition)
    assert actions[0].quantity == Decimal((expected_action - 1) * position_scale)


ORACLE_SETTINGS = settings(
    max_examples=16,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)


@ORACLE_SETTINGS
@given(request=_training_requests(name="tabular-q"))
def test_tabular_q_randomized_full_training_artifact_and_unseen_inference(
    request: TrainingRequest, tmp_path: Path
) -> None:
    artifact, store, policy = _load_policy(TabularQInventoryStrategy(), request, tmp_path)
    expected = _reference_q_learning(request)
    _assert_table_matches(policy["q"], expected)
    _assert_public_unseen_inference(
        strategy_factory=TabularQInventoryStrategy,
        artifact=artifact,
        store=store,
        request=request,
        policy=policy,
        event=_bar_event(instrument_id="SYNTH.DAILY"),
        inference_state=(2.0, 0.0, 2.0),
        position_scale=1,
    )


@ORACLE_SETTINGS
@given(request=_training_requests(name="sarsa"))
def test_sarsa_randomized_full_training_artifact_and_unseen_inference(
    request: TrainingRequest, tmp_path: Path
) -> None:
    artifact, store, policy = _load_policy(SarsaTrendStrategy(), request, tmp_path)
    expected = _reference_sarsa(request)
    _assert_table_matches(policy["q"], expected)
    _assert_public_unseen_inference(
        strategy_factory=SarsaTrendStrategy,
        artifact=artifact,
        store=store,
        request=request,
        policy=policy,
        event=_bar_event(instrument_id="SYNTH.TEST"),
        inference_state=(2.0, 2.0, 0.0),
        position_scale=2,
    )


@ORACLE_SETTINGS
@given(request=_training_requests(name="double-q"))
def test_double_q_randomized_full_training_artifact_and_unseen_inference(
    request: TrainingRequest, tmp_path: Path
) -> None:
    artifact, store, policy = _load_policy(DoubleQBookInventoryStrategy(), request, tmp_path)
    expected = _reference_double_q(request)
    _assert_table_matches(policy["q"], expected)
    _assert_public_unseen_inference(
        strategy_factory=DoubleQBookInventoryStrategy,
        artifact=artifact,
        store=store,
        request=request,
        policy=policy,
        event=_book_event(),
        inference_state=(2.0, 0.0, 2.0),
        position_scale=1,
    )
