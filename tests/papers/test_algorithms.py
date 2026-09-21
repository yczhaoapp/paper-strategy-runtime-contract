from __future__ import annotations

import math

import pytest

from psrc.papers.algorithms import (
    backward_q,
    fit_logistic,
    imbalance,
    probability,
    reservation_quotes,
)
from psrc.papers.data import execution_transitions, labeled_observations, quote_fixture
from psrc.runtime.training import RLTransition


def test_inventory_formula_matches_independent_numeric_oracle() -> None:
    bid, ask = reservation_quotes(100, 2, 0.1, 2, 1.5, 0.5)
    assert (bid + ask) / 2 == pytest.approx(99.6)
    assert ask - bid == pytest.approx(1.4907704227514235)
    empty = reservation_quotes(100, 0, 0.1, 2, 1.5, 0.5)
    assert empty[0] - bid == pytest.approx(0.4)
    assert empty[1] - ask == pytest.approx(0.4)
    terminal = reservation_quotes(100, 20, 0.1, 2, 1.5, 0)
    assert sum(terminal) / 2 == pytest.approx(100)


@pytest.mark.parametrize(
    "values",
    [
        (100, 0, 0, 1, 1, 1),
        (100, 0, 1, -1, 1, 1),
        (100, 0, 1, 1, 0, 1),
        (100, 0, 1, 1, 1, -1),
        (math.nan, 0, 1, 1, 1, 1),
    ],
)
def test_quotes_reject_undefined_parameters(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        reservation_quotes(*values)


def test_logistic_mle_has_known_solution_and_stable_extremes() -> None:
    # At I=-1: P(up)=1/4; at I=+1: P(up)=3/4 => intercept=0, slope=ln(3).
    x = ((-1.0,),) * 4 + ((1.0,),) * 4
    y = (0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0)
    fit = fit_logistic(x, y, 100, 1e-10)
    assert fit["intercept"] == pytest.approx(0, abs=1e-9)
    assert fit["slope"] == pytest.approx(math.log(3), abs=1e-9)
    assert probability(0, 1000, 1) == 1
    assert probability(0, 1000, -1) == 0
    assert imbalance(3, 1) == 0.5


@pytest.mark.parametrize("sizes", [(0, 0), (-1, 3), (math.inf, 1)])
def test_undefined_imbalance_is_not_zero_imputed(sizes: tuple[float, float]) -> None:
    with pytest.raises(ValueError):
        imbalance(*sizes)


@pytest.mark.parametrize(
    "x,y",
    [
        (((1.0,),) * 3, (0.0, 1.0, 0.0)),
        (((1.0,),) * 8, (1.0,) * 8),
        (((math.nan,),) * 8, (0.0, 1.0) * 4),
        (((2.0,),) * 8, (0.0, 1.0) * 4),
        (((0.0,),) * 8, (0.0,) * 6 + (1.0,) * 2),
        (((-1.0,),) * 4 + ((1.0,),) * 4, (0.0,) * 4 + (1.0,) * 4),
    ],
)
def test_logistic_training_fails_on_invalid_or_nonidentifiable_data(
    x: tuple[tuple[float, ...], ...],
    y: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError):
        fit_logistic(x, y, 100, 1e-12)


def test_logistic_iteration_budget_is_enforced() -> None:
    with pytest.raises(ValueError, match="converge"):
        fit_logistic(
            ((-1.0,),) * 4 + ((1.0,),) * 4, (0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0), 1, 1e-10
        )


def transition(t: int, action: int, reward: float) -> RLTransition:
    return RLTransition(
        episode_id=f"fixture:{t}:{action}",
        step=0,
        state=(t, 1, 1),
        action=action,
        reward=reward,
        next_state=(t - 1, 0 if t == 1 else 1, 1),
        terminated=t == 1,
    )


def test_empirical_bellman_backup_matches_hand_computed_returns() -> None:
    samples = tuple(transition(1, a, float(a - 3)) for a in range(3))
    samples += tuple(transition(2, a, float(a)) for a in range(3))
    table = backward_q(samples)
    assert table["1,1,1"] == [-3, -2, -1]
    assert table["2,1,1"] == [-1, 0, 1]
    assert backward_q(tuple(reversed(samples))) == table


@pytest.mark.parametrize(
    "change",
    [
        {"action": 4},
        {"reward": math.inf},
        {"truncated": True},
        {"state": (1.1, 1, 1)},
        {"state": (1, -1, 1)},
        {"state": (1, 1, 0)},
        {"next_state": (1, 0, 1)},
        {"next_state": (0, 2, 1)},
        {"terminated": False},
        {"next_state": (0, 1, 1)},
    ],
)
def test_rl_invalid_transitions_cannot_be_trained(change: dict[str, object]) -> None:
    sample = transition(1, 0, -1).model_copy(update=change)
    with pytest.raises((ValueError, KeyError)):
        backward_q((sample,))


def test_missing_rl_actions_and_bootstrap_states_fail() -> None:
    with pytest.raises(ValueError):
        backward_q(())
    with pytest.raises(ValueError, match="every action"):
        backward_q((transition(1, 0, -1),))
    with pytest.raises(KeyError):
        backward_q(tuple(transition(2, a, 0) for a in range(3)))


def test_labels_use_next_changed_mid_and_do_not_cross_split_boundary() -> None:
    events = quote_fixture()
    features, labels, pairs = labeled_observations(events[:160])
    assert len(features) == len(labels) == len(pairs)
    assert pairs[0] == (0, 2)  # unchanged mid at observation 1 must be skipped.
    assert max(j for _, j in pairs) < 160
    assert set(labels) == {0, 1}
    assert len(labeled_observations(events[:1])[0]) == 0


def test_execution_dataset_enforces_completion_and_covers_private_states() -> None:
    transitions = execution_transitions(quote_fixture()[:20], 4, 4, 0.01)
    table = backward_q(transitions)
    assert len(table) == 4 * 4 * 2
    assert all(t.next_state[1] == 0 for t in transitions if t.state[0] == 1)
    assert all(t.reward <= 1 for t in transitions)


def test_vma_band_matches_paper_long_short_and_closed_states() -> None:
    from decimal import Decimal

    from psrc.domain.account import AccountSnapshot
    from psrc.domain.actions import NoOp, TargetPosition
    from psrc.domain.market import BarPayload
    from psrc.papers.strategies import MovingAverageBandStrategy

    proto = quote_fixture()[0]
    account = AccountSnapshot(
        timestamp=proto.available_time, cash=Decimal(1000), equity=Decimal(1000), positions=()
    )
    for last, expected in ((150, 1), (50, -1), (100, 0)):
        strategy = MovingAverageBandStrategy()
        strategy.parameters = {"short_window": 1, "long_window": 3, "band": 0.1}
        strategy.on_start()
        for index, price in enumerate((100, 100, last)):
            event = proto.model_copy(
                update={
                    "payload": BarPayload(
                        open=Decimal(price),
                        high=Decimal(price),
                        low=Decimal(price),
                        close=Decimal(price),
                        volume=Decimal(100),
                    )
                }
            )
            result = strategy.on_event(event, account)
            if index < 2:
                assert isinstance(result[0], NoOp)
        assert isinstance(result[0], TargetPosition)
        assert result[0].quantity == expected
        strategy.on_start()
        assert strategy.closes == []
