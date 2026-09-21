from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np

from psrc.runtime.artifacts import ArtifactStore
from psrc.strategies.catalog import reinforcement_learning_examples
from psrc.strategies.common import canonicalize_numeric, stable_float
from psrc.strategies.reinforcement_learning import (
    advantage_actor_critic_update,
    linear_actor_critic_update,
    mean_variance_score,
)
from psrc.strategies.rule import avellaneda_stoikov_quotes, weighted_microprice
from psrc.strategies.supervised import (
    erlang_race_probability,
    fit_binary_logistic,
    fit_pooled_ols,
)


def _rl_policy(strategy_id: str, tmp_path: Path) -> dict[str, object]:
    example = next(
        item
        for item in reinforcement_learning_examples()
        if item.manifest.strategy_id == strategy_id
    )
    strategy = example.factory()
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = strategy.train(example.training, store)
    payload = store.load_bytes(
        run_id=example.training.run_id,
        strategy_id=strategy_id,
        manifest=artifact,
    )["policy.json"]
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def test_learned_numbers_have_cross_platform_canonical_precision() -> None:
    assert stable_float(0.09765076688605281) == stable_float(0.09765076688605286)
    assert canonicalize_numeric({"x": -1.5e-17, "nested": [0.6000000000000006]}) == {
        "x": 0.0,
        "nested": [0.6],
    }


def test_weighted_microprice_reproduces_stoikov_toy_formula() -> None:
    observed = weighted_microprice(Decimal("100"), Decimal("10"), Decimal("101"), Decimal("30"))
    assert observed == Decimal("100.25")


def test_avellaneda_stoikov_quotes_reproduce_equations_29_30() -> None:
    bid, ask = avellaneda_stoikov_quotes(
        mid=Decimal("100"),
        inventory=Decimal("2"),
        risk_aversion=Decimal("0.1"),
        variance=Decimal("0.04"),
        remaining_time=Decimal("0.5"),
        arrival_decay=Decimal("1.5"),
    )
    reservation = (bid + ask) / 2
    assert reservation == Decimal("99.996")
    expected_spread = Decimal("0.002") + Decimal(str(20 * np.log(1 + 1 / 15)))
    assert abs((ask - bid) - expected_spread) < Decimal("1e-14")


def test_queue_imbalance_logistic_matches_maximum_likelihood_direction() -> None:
    x = np.asarray([[-1.0], [-0.5], [-0.2], [0.2], [0.5], [1.0]])
    y = np.asarray([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0])
    weights, intercept = fit_binary_logistic(x, y, learning_rate=0.1, epochs=1000)
    negative = 1 / (1 + np.exp(-(x[0] @ weights + intercept)))
    positive = 1 / (1 + np.exp(-(x[-1] @ weights + intercept)))
    assert negative < 0.1
    assert positive > 0.9


def test_fill_probability_reproduces_constant_rate_queue_race() -> None:
    assert erlang_race_probability(1, 1, 2.0, 3.0) == 0.4
    observed = erlang_race_probability(2, 2, 1.0, 1.0)
    assert abs(observed - 0.5) < 1e-12


def test_directional_logistic_reproduces_paper_gradient_descent() -> None:
    x = np.asarray(
        [
            [-1.0, -0.5, -0.2, 0.3, 0.1],
            [-0.8, -0.3, -0.1, 0.2, 0.2],
            [0.8, 0.3, 0.1, 0.2, 0.2],
            [1.0, 0.5, 0.2, 0.3, 0.1],
        ]
    )
    y = np.asarray([-1.0, -1.0, 1.0, 1.0])
    weights, intercept = fit_binary_logistic(x, y, learning_rate=0.01, epochs=1000)
    probabilities = 1 / (1 + np.exp(-(x @ weights + intercept)))
    assert probabilities[:2].max() < 0.5
    assert probabilities[2:].min() > 0.5


def test_cross_sectional_ranker_reproduces_pooled_ols_and_sort() -> None:
    x = np.asarray([[-1.0, 0.0, 1.0], [0.0, 1.0, 1.0], [1.0, 0.0, -1.0], [2.0, 1.0, 0.0]])
    expected_weights = np.asarray([0.6, -0.25, 0.4])
    y = x @ expected_weights + 0.1
    weights, intercept = fit_pooled_ols(x, y)
    assert np.allclose(weights, expected_weights)
    assert abs(intercept - 0.1) < 1e-12
    assert list(np.argsort(x @ weights + intercept)) == [0, 1, 2, 3]


def test_risk_averse_bandit_reproduces_mean_variance_thompson_objective(
    tmp_path: Path,
) -> None:
    assert abs(mean_variance_score(0.8, 0.2, 0.5) - 0.7) < 1e-12
    policy = _rl_policy("reinforcement_learning.risk_averse_contextual_bandit", tmp_path)
    assert policy["algorithm"] == "mean-variance-thompson-disjoint-v1"
    assert len(policy["arms"]) == 3  # type: ignore[arg-type]


def test_a2c_pairs_reproduces_advantage_actor_critic_update(tmp_path: Path) -> None:
    actor, critic, advantage = advantage_actor_critic_update(
        np.zeros((3, 3)),
        np.zeros(3),
        state=np.asarray([1.0, 0.0, 0.0]),
        next_state=np.asarray([0.0, 1.0, 0.0]),
        action=1,
        reward=2.0,
        terminated=True,
    )
    assert advantage == 2.0
    assert np.allclose(critic, [0.08, 0.0, 0.0])
    assert np.allclose(actor[:, 0], [-0.01, 0.02, -0.01])
    assert np.allclose(actor[:, 1:], 0.0)
    policy = _rl_policy("reinforcement_learning.a2c_pairs", tmp_path)
    trained_actor = np.asarray(policy["actor_weights"])
    trained_critic = np.asarray(policy["critic_weights"])
    assert policy["algorithm"] == "advantage-actor-critic-pairs-v1"
    assert trained_actor.shape == (3, 3) and np.any(trained_actor != 0)
    assert trained_critic.shape == (3,) and np.any(trained_critic != 0)


def test_continuous_actor_critic_reproduces_policy_and_value_updates(tmp_path: Path) -> None:
    actor, critic, delta = linear_actor_critic_update(
        np.zeros(3),
        np.zeros(3),
        state=np.asarray([1.0, 0.0, 0.0]),
        next_state=np.asarray([0.0, 1.0, 0.0]),
        chosen_weight=0.8,
        reward=2.0,
        terminated=True,
    )
    assert delta == 2.0
    assert np.allclose(critic, [0.1, 0.0, 0.0])
    assert np.allclose(actor, [0.032, 0.0, 0.0])
    policy = _rl_policy("reinforcement_learning.linear_actor_critic_allocation", tmp_path)
    assert policy["algorithm"] == "linear-actor-critic-v1"
    assert np.any(np.asarray(policy["actor_weights"]) != 0)
    assert np.any(np.asarray(policy["critic_weights"]) != 0)
