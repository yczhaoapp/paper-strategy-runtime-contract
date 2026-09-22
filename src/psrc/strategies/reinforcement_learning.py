from __future__ import annotations

import json
import math
from collections import deque
from decimal import Decimal
from hashlib import sha256

import numpy as np

from psrc.contract.models import ActionKind, DataKind, StrategyKind, TrainingMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, NoOp, TargetPosition, TargetWeight
from psrc.domain.market import BarPayload, BookSnapshotL2Payload, MarketEvent
from psrc.runtime.artifacts import ArtifactIO, ArtifactManifest
from psrc.runtime.training import RLTransition, TrainingRequest
from psrc.strategies.common import (
    bar_requirement,
    canonicalize_numeric,
    event_requirement,
    make_manifest,
    stable_float,
)


def _state_key(state: tuple[float, ...]) -> str:
    return ",".join(str(max(-2, min(2, round(value)))) for value in state)


def _position(account: AccountSnapshot, instrument_id: str) -> Decimal:
    match = next(
        (position for position in account.positions if position.instrument_id == instrument_id),
        None,
    )
    return match.quantity if match is not None else Decimal("0")


def portfolio_allocation(account: AccountSnapshot, instrument_id: str, mark: Decimal) -> float:
    """Return the current asset weight in the same unit used by the training state."""
    if account.equity == 0:
        return 0.0
    return stable_float(float(_position(account, instrument_id) * mark / account.equity))


def allocation_for_action(action: int) -> float:
    if action not in {0, 1, 2}:
        raise ValueError("allocation action must be in {0,1,2}")
    return (-0.8, 0.0, 0.8)[action]


def q_learning_update(
    values: list[float],
    next_values: list[float],
    *,
    action: int,
    reward: float,
    terminated: bool,
    learning_rate: float = 0.2,
    discount: float = 0.9,
) -> None:
    """Apply one off-policy tabular Q-learning Bellman update."""
    bootstrap = 0.0 if terminated else discount * max(next_values)
    values[action] += learning_rate * (reward + bootstrap - values[action])


def sarsa_update(
    values: list[float],
    next_values: list[float],
    *,
    action: int,
    next_action: int | None,
    reward: float,
    terminated: bool,
    learning_rate: float = 0.15,
    discount: float = 0.85,
) -> None:
    """Apply one on-policy SARSA update using the observed next action."""
    if not terminated and next_action is None:
        raise ValueError("non-terminal SARSA transitions require the actual next action")
    if terminated:
        bootstrap = 0.0
    else:
        assert next_action is not None
        bootstrap = discount * next_values[next_action]
    values[action] += learning_rate * (reward + bootstrap - values[action])


def double_q_update(
    update_values: list[float],
    update_next_values: list[float],
    evaluation_next_values: list[float],
    *,
    action: int,
    reward: float,
    terminated: bool,
    learning_rate: float = 0.18,
    discount: float = 0.9,
) -> None:
    """Apply one Double-Q update: select with one table, evaluate with the other."""
    greedy = int(np.argmax(np.asarray(update_next_values)))
    bootstrap = 0.0 if terminated else discount * evaluation_next_values[greedy]
    update_values[action] += learning_rate * (
        reward + bootstrap - update_values[action]
    )


class _RLStrategy:
    manifest = make_manifest(
        strategy_id="reinforcement_learning.abstract",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint="invalid",
        profiles=frozenset({"training.rl.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("SYNTH.DAILY",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP}),
        training=TrainingMode.REQUIRED,
    )

    def __init__(self) -> None:
        self.policy: dict[str, object] | None = None
        self.artifact_id: str | None = None

    def _transitions(self, request: TrainingRequest) -> tuple[RLTransition, ...]:
        transitions = request.transitions
        if len(transitions) < 9:
            raise ValueError("RL training requires at least nine transitions")
        if any(
            len(item.state) != 3 or len(item.next_state) != 3 or item.action not in {0, 1, 2}
            for item in transitions
        ):
            raise ValueError("RL transition requires 3-D states and actions in {0,1,2}")
        return transitions

    def _save(
        self, request: TrainingRequest, store: ArtifactIO, policy: dict[str, object]
    ) -> ArtifactManifest:
        canonical_policy = canonicalize_numeric(policy)
        if not isinstance(canonical_policy, dict):
            raise TypeError("canonical policy must remain an object")
        payload = json.dumps(canonical_policy, sort_keys=True, separators=(",", ":")).encode()
        artifact_id = f"sha256-{sha256(payload).hexdigest()}"
        return store.save_bytes(
            run_id=request.run_id,
            artifact_id=artifact_id,
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.strategy_version,
            artifact_kind="policy",
            framework="numpy-json",
            logical_name="policy.json",
            media_type="application/json",
            payload=payload,
            training_dataset_id=request.dataset_id,
            seed=request.seed,
            training_request_sha256=request.request_sha256,
            metadata={"algorithm": str(policy["algorithm"])},
        )

    def load(self, manifest: ArtifactManifest, store: ArtifactIO, *, run_id: str) -> None:
        if manifest.strategy_id != self.manifest.strategy_id:
            raise ValueError("policy strategy_id does not match strategy")
        payload = store.load_bytes(
            run_id=run_id, strategy_id=self.manifest.strategy_id, manifest=manifest
        )["policy.json"]
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("policy artifact root must be an object")
        self.policy = value
        self.artifact_id = manifest.artifact_id

    def _require_policy(self) -> dict[str, object]:
        if self.policy is None:
            raise RuntimeError("policy artifact has not been loaded")
        return self.policy

    @staticmethod
    def _q_action(policy: dict[str, object], state: tuple[float, ...], table_name: str) -> int:
        table = policy[table_name]
        assert isinstance(table, dict)
        values = table.get(_state_key(state))
        if values is None:
            # Tabular policies use a deterministic nearest-state projection for
            # previously unseen observations; the policy artifact records this rule.
            target = tuple(max(-2, min(2, round(value))) for value in state)

            def distance(key: str) -> tuple[float, str]:
                candidate = tuple(float(value) for value in key.split(","))
                return (
                    sum((left - right) ** 2 for left, right in zip(target, candidate, strict=True)),
                    key,
                )

            nearest = min(table, key=distance)
            values = table[nearest]
        assert isinstance(values, list)
        return int(np.argmax(np.asarray(values, dtype=float)))

    def on_start(self) -> None:
        pass

    def on_finish(self) -> None:
        pass


class TabularQInventoryStrategy(_RLStrategy):
    manifest = make_manifest(
        strategy_id="reinforcement_learning.tabular_q_inventory",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint="psrc.strategies.reinforcement_learning:TabularQInventoryStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1", "training.rl.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("SYNTH.DAILY",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("1"),
    )

    def train(self, request: TrainingRequest, store: ArtifactIO) -> ArtifactManifest:
        transitions = self._transitions(request)
        q: dict[str, list[float]] = {}
        for _ in range(25):
            for item in transitions:
                state = _state_key(item.state)
                next_state = _state_key(item.next_state)
                q.setdefault(state, [0.0, 0.0, 0.0])
                q.setdefault(next_state, [0.0, 0.0, 0.0])
                q_learning_update(
                    q[state],
                    q[next_state],
                    action=item.action,
                    reward=item.reward,
                    terminated=item.terminated,
                )
        return self._save(
            request,
            store,
            {
                "algorithm": "tabular-q-learning-v1",
                "q": q,
                "unseen_action": "nearest-state-v1",
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        bar = event.payload
        momentum = float((bar.close - bar.open) / bar.open * 100)
        inventory = float(_position(account, event.instrument_id))
        range_state = float((bar.high - bar.low) / bar.open * 100)
        action = self._q_action(self._require_policy(), (momentum, inventory, range_state), "q")
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal(action - 1),
                reason_code="policy.tabular_q_inventory",
            ),
        )


class SarsaTrendStrategy(_RLStrategy):
    manifest = make_manifest(
        strategy_id="reinforcement_learning.sarsa_trend",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint="psrc.strategies.reinforcement_learning:SarsaTrendStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1", "training.rl.v1"}),
        data=(bar_requirement(interval="PT1M", symbols=("SYNTH.TEST",), lookback=3),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("2"),
    )

    def train(self, request: TrainingRequest, store: ArtifactIO) -> ArtifactManifest:
        transitions = self._transitions(request)
        q: dict[str, list[float]] = {}
        for _ in range(20):
            for item in transitions:
                state = _state_key(item.state)
                next_state = _state_key(item.next_state)
                q.setdefault(state, [0.0, 0.0, 0.0])
                q.setdefault(next_state, [0.0, 0.0, 0.0])
                sarsa_update(
                    q[state],
                    q[next_state],
                    action=item.action,
                    next_action=item.next_action,
                    reward=item.reward,
                    terminated=item.terminated,
                )
        return self._save(
            request,
            store,
            {
                "algorithm": "on-policy-sarsa-v1",
                "q": q,
                "unseen_action": "nearest-state-v1",
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        bar = event.payload
        state = (
            float((bar.close - bar.open) / bar.open * 100),
            float((bar.high - bar.low) / bar.open * 100),
            float(_position(account, event.instrument_id)),
        )
        action = self._q_action(self._require_policy(), state, "q")
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal((action - 1) * 2),
                reason_code="policy.sarsa_trend",
            ),
        )


def mean_variance_score(mean: float, variance: float, risk_tolerance: float) -> float:
    """Mean-variance objective used by Lin, Wang and Zhou's contextual bandit."""
    if variance < 0 or risk_tolerance <= 0:
        raise ValueError("variance and risk tolerance must be valid")
    return mean - risk_tolerance * variance


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values)
    exponent = np.exp(shifted)
    return np.asarray(exponent / exponent.sum(), dtype=float)


def advantage_actor_critic_update(
    actor: np.ndarray,
    critic: np.ndarray,
    *,
    state: np.ndarray,
    next_state: np.ndarray,
    action: int,
    reward: float,
    terminated: bool,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Apply one explicit three-action A2C update for independent numerical checks."""
    probabilities = _softmax(actor @ state)
    next_value = 0.0 if terminated else float(critic @ next_state)
    advantage = reward + 0.9 * next_value - float(critic @ state)
    updated_critic = critic + 0.04 * advantage * state
    log_policy_gradient = -probabilities
    log_policy_gradient[action] += 1.0
    updated_actor = actor + 0.015 * advantage * np.outer(log_policy_gradient, state)
    return updated_actor, updated_critic, advantage


def linear_actor_critic_update(
    actor: np.ndarray,
    critic: np.ndarray,
    *,
    state: np.ndarray,
    next_state: np.ndarray,
    chosen_weight: float,
    reward: float,
    terminated: bool,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Apply one linear continuous-action actor/critic update."""
    next_value = 0.0 if terminated else float(critic @ next_state)
    delta = reward + 0.9 * next_value - float(critic @ state)
    updated_critic = critic + 0.05 * delta * state
    predicted_weight = math.tanh(float(actor @ state))
    policy_gradient = (chosen_weight - predicted_weight) * (1 - predicted_weight**2) * state
    updated_actor = actor + 0.02 * delta * policy_gradient
    return updated_actor, updated_critic, delta


class RiskAverseContextualBanditStrategy(_RLStrategy):
    manifest = make_manifest(
        strategy_id="reinforcement_learning.risk_averse_contextual_bandit",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint=("psrc.strategies.reinforcement_learning:RiskAverseContextualBanditStrategy"),
        profiles=frozenset({"core.bar.v1", "portfolio.batch.v1", "training.rl.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("SYNTH.DAILY",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_WEIGHT}),
        training=TrainingMode.REQUIRED,
        max_position=None,
    )

    def __init__(self) -> None:
        super().__init__()
        self.counter = 0

    def train(self, request: TrainingRequest, store: ArtifactIO) -> ArtifactManifest:
        transitions = self._transitions(request)
        arms: list[dict[str, object]] = []
        for action in range(3):
            rows = np.asarray(
                [item.state for item in transitions if item.action == action], dtype=float
            )
            rewards = np.asarray(
                [item.reward for item in transitions if item.action == action], dtype=float
            )
            design = np.eye(3) + rows.T @ rows
            response = rows.T @ rewards
            posterior_mean = np.linalg.solve(design, response)
            residuals = rewards - rows @ posterior_mean
            reward_variance = float((residuals @ residuals + 1.0) / (len(rows) + 2.0))
            arms.append(
                {
                    "mean": posterior_mean.tolist(),
                    "covariance": np.linalg.inv(design).tolist(),
                    "reward_variance": reward_variance,
                }
            )
        return self._save(
            request,
            store,
            {
                "algorithm": "mean-variance-thompson-disjoint-v1",
                "arms": arms,
                "risk_tolerance": 0.5,
                "seed": request.seed,
            },
        )

    def on_start(self) -> None:
        self.counter = 0

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        self.counter += 1
        policy = self._require_policy()
        arms = policy["arms"]
        assert isinstance(arms, list)
        seed = policy["seed"]
        risk_tolerance = policy["risk_tolerance"]
        if not isinstance(seed, int) or not isinstance(risk_tolerance, (int, float)):
            raise ValueError("bandit artifact seed and risk tolerance must be numeric")
        bar = event.payload
        context = np.asarray(
            [
                float((bar.close - bar.open) / bar.open * 10),
                float((bar.high - bar.low) / bar.open * 10),
                math.log1p(float(bar.volume)) / 10,
            ]
        )
        rng = np.random.default_rng(seed + self.counter)
        scores = []
        for arm in arms:
            assert isinstance(arm, dict)
            reward_variance = arm["reward_variance"]
            if not isinstance(reward_variance, (int, float)):
                raise ValueError("bandit artifact variance must be numeric")
            sampled = rng.multivariate_normal(
                np.asarray(arm["mean"], dtype=float),
                np.asarray(arm["covariance"], dtype=float),
            )
            scores.append(
                mean_variance_score(
                    float(context @ sampled),
                    float(reward_variance),
                    float(risk_tolerance),
                )
            )
        action = int(np.argmax(np.asarray(scores)))
        return (
            TargetWeight(
                instrument_id=event.instrument_id,
                weight=Decimal(str((action - 1) * 0.8)),
                reason_code="policy.mean_variance_thompson",
            ),
        )


class DoubleQBookInventoryStrategy(_RLStrategy):
    manifest = make_manifest(
        strategy_id="reinforcement_learning.double_q_book_inventory",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint="psrc.strategies.reinforcement_learning:DoubleQBookInventoryStrategy",
        profiles=frozenset({"event.l2.v1", "execution.basic.v1", "training.rl.v1"}),
        data=(
            event_requirement(
                stream_id="book",
                kind=DataKind.BOOK_SNAPSHOT_L2,
                symbols=("SYNTH.L2",),
                fields=frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
                depth=3,
            ),
        ),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("1"),
    )

    def train(self, request: TrainingRequest, store: ArtifactIO) -> ArtifactManifest:
        transitions = self._transitions(request)
        q1: dict[str, list[float]] = {}
        q2: dict[str, list[float]] = {}
        for epoch in range(20):
            for index, item in enumerate(transitions):
                state, next_state = _state_key(item.state), _state_key(item.next_state)
                q1.setdefault(state, [0.0] * 3)
                q1.setdefault(next_state, [0.0] * 3)
                q2.setdefault(state, [0.0] * 3)
                q2.setdefault(next_state, [0.0] * 3)
                left, right = (q1, q2) if (epoch + index) % 2 == 0 else (q2, q1)
                double_q_update(
                    left[state],
                    left[next_state],
                    right[next_state],
                    action=item.action,
                    reward=item.reward,
                    terminated=item.terminated,
                )
        combined = {
            state: [q1[state][index] + q2[state][index] for index in range(3)] for state in q1
        }
        return self._save(
            request,
            store,
            {
                "algorithm": "double-q-learning-v1",
                "q": combined,
                "unseen_action": "nearest-state-v1",
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BookSnapshotL2Payload):
            return (NoOp(reason_code="event.not_l2", explanation="requires L2 book"),)
        book = event.payload
        bids = sum(level.size for level in book.bids)
        asks = sum(level.size for level in book.asks)
        state = (
            float((bids - asks) / max(bids + asks, Decimal("1")) * 2),
            float(_position(account, event.instrument_id)),
            float((book.asks[0].price - book.bids[0].price) * 10),
        )
        action = self._q_action(self._require_policy(), state, "q")
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=Decimal(action - 1),
                reason_code="policy.double_q_book_inventory",
            ),
        )


class A2CPairsStrategy(_RLStrategy):
    manifest = make_manifest(
        strategy_id="reinforcement_learning.a2c_pairs",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint="psrc.strategies.reinforcement_learning:A2CPairsStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1", "training.rl.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("PUBLIC.AAPL", "PUBLIC.MSFT"), lookback=2),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        training=TrainingMode.REQUIRED,
        max_position=Decimal("1"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.symbols = ("PUBLIC.AAPL", "PUBLIC.MSFT")
        self.latest: dict[str, tuple[object, Decimal]] = {}
        self.spreads: deque[float] = deque(maxlen=6)

    def train(self, request: TrainingRequest, store: ArtifactIO) -> ArtifactManifest:
        transitions = self._transitions(request)
        actor = np.zeros((3, 3), dtype=float)
        critic = np.zeros(3, dtype=float)
        for _ in range(80):
            for item in transitions:
                state = np.asarray(item.state, dtype=float)
                next_state = np.asarray(item.next_state, dtype=float)
                actor, critic, _ = advantage_actor_critic_update(
                    actor,
                    critic,
                    state=state,
                    next_state=next_state,
                    action=item.action,
                    reward=item.reward,
                    terminated=item.terminated,
                )
        return self._save(
            request,
            store,
            {
                "algorithm": "advantage-actor-critic-pairs-v1",
                "actor_weights": actor.tolist(),
                "critic_weights": critic.tolist(),
            },
        )

    def on_start(self) -> None:
        self.latest.clear()
        self.spreads.clear()

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires pair bars"),)
        self.latest[event.instrument_id] = (event.available_time, event.payload.close)
        if any(symbol not in self.latest for symbol in self.symbols):
            return (NoOp(reason_code="pair.awaiting_peer", explanation="pair batch incomplete"),)
        if self.latest[self.symbols[0]][0] != self.latest[self.symbols[1]][0]:
            return (NoOp(reason_code="pair.not_synchronized", explanation="bar times differ"),)
        spread = float(self.latest[self.symbols[0]][1] - 2 * self.latest[self.symbols[1]][1])
        inventory = _position(account, self.symbols[0])
        history = np.asarray(tuple(self.spreads), dtype=float)
        mean = float(history.mean()) if len(history) else spread
        deviation = float(history.std()) if len(history) > 1 else 1.0
        zscore = (spread - mean) / max(deviation, 1e-6)
        change = spread - self.spreads[-1] if self.spreads else 0.0
        self.spreads.append(spread)
        state = np.asarray([zscore, float(inventory), change / 5], dtype=float)
        policy = self._require_policy()
        action = int(np.argmax(np.asarray(policy["actor_weights"], dtype=float) @ state))
        direction = Decimal(action - 1)
        return (
            TargetPosition(
                instrument_id=self.symbols[0],
                quantity=direction,
                reason_code="policy.a2c_pair_left",
            ),
            TargetPosition(
                instrument_id=self.symbols[1],
                quantity=-direction,
                reason_code="policy.a2c_pair_right",
            ),
        )


class LinearActorCriticAllocationStrategy(_RLStrategy):
    manifest = make_manifest(
        strategy_id="reinforcement_learning.linear_actor_critic_allocation",
        kind=StrategyKind.REINFORCEMENT_LEARNING,
        entrypoint=("psrc.strategies.reinforcement_learning:LinearActorCriticAllocationStrategy"),
        profiles=frozenset({"core.bar.v1", "portfolio.batch.v1", "training.rl.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("PUBLIC.AAPL",), lookback=2),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_WEIGHT}),
        training=TrainingMode.REQUIRED,
        max_position=None,
    )

    def train(self, request: TrainingRequest, store: ArtifactIO) -> ArtifactManifest:
        transitions = self._transitions(request)
        actor = np.zeros(3)
        critic = np.zeros(3)
        for _ in range(30):
            for item in transitions:
                state = np.asarray(item.state, dtype=float)
                next_state = np.asarray(item.next_state, dtype=float)
                actor, critic, _ = linear_actor_critic_update(
                    actor,
                    critic,
                    state=state,
                    next_state=next_state,
                    chosen_weight=allocation_for_action(item.action),
                    reward=item.reward,
                    terminated=item.terminated,
                )
        return self._save(
            request,
            store,
            {
                "algorithm": "linear-actor-critic-v1",
                "actor_weights": actor.tolist(),
                "critic_weights": critic.tolist(),
            },
        )

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        policy = self._require_policy()
        weights = np.asarray(policy["actor_weights"], dtype=float)
        bar = event.payload
        state = np.asarray(
            [
                float((bar.close - bar.open) / bar.open * 10),
                float((bar.high - bar.low) / bar.open * 10),
                portfolio_allocation(account, event.instrument_id, bar.close),
            ]
        )
        weight = stable_float(max(-0.8, min(0.8, math.tanh(float(weights @ state)))))
        return (
            TargetWeight(
                instrument_id=event.instrument_id,
                weight=Decimal(str(weight)),
                reason_code="policy.linear_actor_allocation",
            ),
        )
