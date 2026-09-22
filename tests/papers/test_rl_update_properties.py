from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from psrc.strategies.reinforcement_learning import (
    double_q_update,
    q_learning_update,
    sarsa_update,
)


def test_rl_production_source_has_no_test_fixture_dispatch() -> None:
    source_path = Path(__file__).parents[2] / "src/psrc/strategies/reinforcement_learning.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")]
        )
    }
    assert not imports & {"pytest", "unittest"}
    forbidden_condition_tokens = (
        "pytest",
        "testing",
        "test_",
        "dataset_id",
        "request.seed",
        "sha256",
    )
    conditions = [
        ast.unparse(node.test).lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
    ]
    assert not [
        condition
        for condition in conditions
        if any(token in condition for token in forbidden_condition_tokens)
    ]


def _assert_untouched(values: list[float], before: list[float], action: int) -> None:
    assert values[:action] == before[:action]
    assert values[action + 1 :] == before[action + 1 :]


@pytest.mark.parametrize(
    ("old_q", "reward", "next_values", "alpha", "gamma", "terminated"),
    (
        (1.0, 2.0, [2.0, 4.0, 3.0], 0.1, 0.9, False),
        (0.0, -1.0, [-2.0, 2.0, 0.5], 0.5, 0.5, False),
        (4.0, 3.0, [99.0, 98.0, 97.0], 0.2, 0.9, True),
        (-2.0, 8.0, [5.0, 7.0, 6.0], 0.0, 0.4, False),
    ),
    ids=("positive-bootstrap", "negative-reward", "terminal", "zero-alpha"),
)
def test_q_learning_parameterized_reference_formula(
    old_q: float,
    reward: float,
    next_values: list[float],
    alpha: float,
    gamma: float,
    terminated: bool,
) -> None:
    action = 1
    values = [-7.0, old_q, 11.0]
    before = values.copy()

    q_learning_update(
        values,
        next_values,
        action=action,
        reward=reward,
        terminated=terminated,
        learning_rate=alpha,
        discount=gamma,
    )

    bootstrap = 0.0 if terminated else gamma * max(next_values)
    expected = old_q + alpha * (reward + bootstrap - old_q)
    assert values[action] == pytest.approx(expected)
    _assert_untouched(values, before, action)


def test_q_learning_matches_independent_seeded_reference_batch() -> None:
    rng = random.Random(0x51A7)
    for index in range(64):
        values = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        next_values = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        before = values.copy()
        action = rng.randrange(3)
        reward = rng.uniform(-4.0, 4.0)
        alpha = rng.random()
        gamma = rng.random()
        terminated = index % 5 == 0

        q_learning_update(
            values,
            next_values,
            action=action,
            reward=reward,
            terminated=terminated,
            learning_rate=alpha,
            discount=gamma,
        )

        bootstrap = 0.0 if terminated else gamma * max(next_values)
        expected = before[action] + alpha * (reward + bootstrap - before[action])
        assert values[action] == pytest.approx(expected)
        _assert_untouched(values, before, action)


@pytest.mark.parametrize(
    ("old_q", "reward", "next_values", "next_action", "alpha", "gamma", "terminated"),
    (
        (1.0, 2.0, [9.0, 4.0, 2.0], 1, 0.15, 0.85, False),
        (-2.0, -1.0, [3.0, -4.0, 8.0], 0, 0.6, 0.4, False),
        (4.0, 3.0, [99.0, 98.0, 97.0], None, 0.2, 0.9, True),
        (3.0, 7.0, [1.0, 5.0, 2.0], 2, 0.0, 0.5, False),
    ),
    ids=("observed-nongreedy", "negative-reward", "terminal", "zero-alpha"),
)
def test_sarsa_parameterized_reference_formula(
    old_q: float,
    reward: float,
    next_values: list[float],
    next_action: int | None,
    alpha: float,
    gamma: float,
    terminated: bool,
) -> None:
    action = 0
    values = [old_q, -3.0, 6.0]
    before = values.copy()

    sarsa_update(
        values,
        next_values,
        action=action,
        next_action=next_action,
        reward=reward,
        terminated=terminated,
        learning_rate=alpha,
        discount=gamma,
    )

    if terminated:
        bootstrap = 0.0
    else:
        assert next_action is not None
        bootstrap = gamma * next_values[next_action]
    expected = old_q + alpha * (reward + bootstrap - old_q)
    assert values[action] == pytest.approx(expected)
    _assert_untouched(values, before, action)


def test_sarsa_changes_with_observed_action_and_rejects_missing_action() -> None:
    first = [1.0, 0.0, 0.0]
    second = first.copy()
    next_values = [9.0, 4.0, -2.0]
    sarsa_update(
        first,
        next_values,
        action=0,
        next_action=1,
        reward=2.0,
        terminated=False,
    )
    sarsa_update(
        second,
        next_values,
        action=0,
        next_action=2,
        reward=2.0,
        terminated=False,
    )
    assert first[0] != second[0]
    assert first[0] != pytest.approx(1.0 + 0.15 * (2.0 + 0.85 * 9.0 - 1.0))
    with pytest.raises(ValueError, match="actual next action"):
        sarsa_update(
            [1.0, 0.0, 0.0],
            next_values,
            action=0,
            next_action=None,
            reward=2.0,
            terminated=False,
        )


def test_sarsa_matches_independent_seeded_reference_batch() -> None:
    rng = random.Random(0x5A25A)
    for index in range(64):
        values = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        next_values = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        before = values.copy()
        action = rng.randrange(3)
        greedy = max(range(3), key=next_values.__getitem__)
        next_action = (greedy + 1 + index % 2) % 3
        reward = rng.uniform(-4.0, 4.0)
        alpha = rng.random()
        gamma = rng.random()
        terminated = index % 7 == 0

        sarsa_update(
            values,
            next_values,
            action=action,
            next_action=None if terminated else next_action,
            reward=reward,
            terminated=terminated,
            learning_rate=alpha,
            discount=gamma,
        )

        bootstrap = 0.0 if terminated else gamma * next_values[next_action]
        expected = before[action] + alpha * (reward + bootstrap - before[action])
        assert values[action] == pytest.approx(expected)
        _assert_untouched(values, before, action)
        if not terminated:
            q_learning_result = before[action] + alpha * (
                reward + gamma * max(next_values) - before[action]
            )
            assert values[action] != pytest.approx(q_learning_result)


@pytest.mark.parametrize(
    ("selection", "evaluation", "terminated"),
    (
        ([2.0, 5.0, 3.0], [11.0, 7.0, 13.0], False),
        ([11.0, 7.0, 13.0], [2.0, 5.0, 3.0], False),
        ([3.0, 8.0, 4.0], [99.0, 98.0, 97.0], True),
    ),
    ids=("update-a", "update-b", "terminal"),
)
def test_double_q_parameterized_cross_table_reference(
    selection: list[float],
    evaluation: list[float],
    terminated: bool,
) -> None:
    values = [-2.0, 1.0, 6.0]
    before = values.copy()
    action = 1
    alpha, gamma, reward = 0.35, 0.65, -0.75

    double_q_update(
        values,
        selection,
        evaluation,
        action=action,
        reward=reward,
        terminated=terminated,
        learning_rate=alpha,
        discount=gamma,
    )

    greedy = max(range(len(selection)), key=selection.__getitem__)
    bootstrap = 0.0 if terminated else gamma * evaluation[greedy]
    expected = before[action] + alpha * (reward + bootstrap - before[action])
    assert values[action] == pytest.approx(expected)
    _assert_untouched(values, before, action)


def test_double_q_table_swap_swaps_selector_and_evaluator() -> None:
    next_a = [2.0, 5.0, 3.0]
    next_b = [11.0, 7.0, 13.0]
    updated_a = [1.0, -2.0, 4.0]
    updated_b = updated_a.copy()

    double_q_update(
        updated_a,
        next_a,
        next_b,
        action=0,
        reward=2.0,
        terminated=False,
    )
    double_q_update(
        updated_b,
        next_b,
        next_a,
        action=0,
        reward=2.0,
        terminated=False,
    )

    expected_a = 1.0 + 0.18 * (2.0 + 0.9 * next_b[1] - 1.0)
    expected_b = 1.0 + 0.18 * (2.0 + 0.9 * next_a[2] - 1.0)
    assert updated_a[0] == pytest.approx(expected_a)
    assert updated_b[0] == pytest.approx(expected_b)
    assert updated_a[0] != updated_b[0]


def test_double_q_matches_independent_seeded_reference_batch() -> None:
    rng = random.Random(0xD0B1E)
    for index in range(64):
        values = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        selection = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        evaluation = [rng.uniform(-8.0, 8.0) for _ in range(3)]
        before = values.copy()
        action = rng.randrange(3)
        reward = rng.uniform(-4.0, 4.0)
        alpha = rng.random()
        gamma = rng.random()
        terminated = index % 6 == 0

        double_q_update(
            values,
            selection,
            evaluation,
            action=action,
            reward=reward,
            terminated=terminated,
            learning_rate=alpha,
            discount=gamma,
        )

        greedy = max(range(3), key=selection.__getitem__)
        bootstrap = 0.0 if terminated else gamma * evaluation[greedy]
        expected = before[action] + alpha * (reward + bootstrap - before[action])
        assert values[action] == pytest.approx(expected)
        _assert_untouched(values, before, action)
