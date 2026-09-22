# Generated, reviewable package entrypoint.
# Bundled implementation: LinearActorCriticAllocationStrategy
from psrc.strategy_api import bundled_strategy_class

_BundledStrategy = bundled_strategy_class("reinforcement_learning.linear_actor_critic_allocation")


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
