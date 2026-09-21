from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

import numpy as np

from psrc.runtime.training import RLTransition


def reservation_quotes(
    mid: float, inventory: float, gamma: float, sigma: float, k: float, remaining: float
) -> tuple[float, float]:
    """Avellaneda--Stoikov (2008), equations (29)--(30)."""
    if not all(math.isfinite(x) for x in (mid, inventory, gamma, sigma, k, remaining)):
        raise ValueError("non-finite quote input")
    if mid <= 0 or gamma <= 0 or sigma < 0 or k <= 0 or remaining < 0:
        raise ValueError("invalid quote parameters")
    risk = gamma * sigma**2 * remaining
    reservation = mid - inventory * risk
    spread = risk + 2 * math.log1p(gamma / k) / gamma
    return reservation - spread / 2, reservation + spread / 2


def imbalance(bid_size: float, ask_size: float) -> float:
    if not math.isfinite(bid_size + ask_size) or min(bid_size, ask_size) < 0:
        raise ValueError("invalid queue sizes")
    if bid_size + ask_size <= 0:
        raise ValueError("queue imbalance undefined for empty queues")
    return (bid_size - ask_size) / (bid_size + ask_size)


def probability(intercept: float, slope: float, value: float) -> float:
    z = intercept + slope * value
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def fit_logistic(
    features: tuple[tuple[float, ...], ...],
    labels: tuple[float, ...],
    iterations: int,
    tolerance: float,
) -> dict[str, Any]:
    """Unregularized Bernoulli maximum likelihood via damped Newton steps."""
    x = np.asarray(features, dtype=float)
    y = np.asarray(labels, dtype=float)
    if x.ndim != 2 or x.shape[1] != 1 or len(y) != len(x) or len(y) < 8:
        raise ValueError("expected >=8 aligned observations with one imbalance feature")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or set(y) != {0.0, 1.0}:
        raise ValueError("finite observations and both binary classes are required")
    if np.any(np.abs(x) > 1):
        raise ValueError("imbalance must lie in [-1,1]")
    design = np.column_stack([np.ones(len(x)), x])
    if np.linalg.matrix_rank(design) < 2:
        raise ValueError("logistic design is singular")
    positive, negative = x[y == 1, 0], x[y == 0, 0]
    if min(positive) > max(negative) or min(negative) > max(positive):
        raise ValueError("logistic MLE is completely separated")
    weights = np.zeros(2)
    for iteration in range(iterations):
        z = design @ weights
        p = np.array([probability(float(weights[0]), float(weights[1]), float(v[0])) for v in x])
        gradient = design.T @ (p - y)
        if float(np.max(np.abs(gradient))) < tolerance:
            return {
                "algorithm": "bernoulli-mle-newton-v1",
                "intercept": float(weights[0]),
                "slope": float(weights[1]),
                "iterations": iteration,
                "negative_log_likelihood": float(np.sum(np.logaddexp(0, z) - y * z)),
            }
        hessian = design.T @ ((p * (1 - p))[:, None] * design)
        if np.linalg.cond(hessian) > 1e12:
            raise ValueError("logistic MLE is singular or separated")
        step = np.linalg.solve(hessian, gradient)
        old_loss = np.sum(np.logaddexp(0, z) - y * z)
        scale = 1.0
        while scale > 1e-10:
            candidate = weights - scale * step
            next_z = design @ candidate
            if np.sum(np.logaddexp(0, next_z) - y * next_z) <= old_loss:
                weights = candidate
                break
            scale *= 0.5
        else:
            raise ValueError("logistic optimizer failed line search")
        if np.max(np.abs(weights)) > 40:
            raise ValueError("logistic MLE appears separated")
    raise ValueError("logistic optimizer did not converge")


def state_key(state: tuple[float, ...]) -> str:
    if len(state) != 3 or any(not math.isfinite(v) or int(v) != v for v in state):
        raise ValueError("execution state must be integral (time, inventory, imbalance_bin)")
    t, inventory, market = map(int, state)
    if t < 0 or inventory < 0 or market not in {-1, 1}:
        raise ValueError("invalid execution state")
    return f"{t},{inventory},{market}"


def backward_q(transitions: tuple[RLTransition, ...]) -> dict[str, list[float]]:
    """Finite-horizon empirical Bellman backups; never project an unseen state."""
    if not transitions:
        raise ValueError("execution training requires transitions")
    buckets: dict[tuple[float, ...], dict[int, list[RLTransition]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in transitions:
        state_key(item.state)
        state_key(item.next_state)
        if item.action not in {0, 1, 2} or not math.isfinite(item.reward) or item.truncated:
            raise ValueError("invalid execution transition")
        if item.next_state[0] != item.state[0] - 1 or item.next_state[1] > item.state[1]:
            raise ValueError("time must decrease by one; inventory cannot increase")
        terminal = item.next_state[0] == 0 or item.next_state[1] == 0
        if item.terminated != terminal or (item.next_state[0] == 0 and item.next_state[1] != 0):
            raise ValueError("terminal transitions must complete inventory")
        buckets[item.state][item.action].append(item)
    table: dict[str, list[float]] = {}
    for state in sorted(buckets):
        if set(buckets[state]) != {0, 1, 2}:
            raise ValueError("counterfactual execution training requires every action")
        values = []
        for action in range(3):
            samples = buckets[state][action]
            returns = []
            for item in samples:
                future = 0.0 if item.terminated else max(table[state_key(item.next_state)])
                returns.append(item.reward + future)
            values.append(sum(returns) / len(returns))
        table[state_key(state)] = values
    return table
