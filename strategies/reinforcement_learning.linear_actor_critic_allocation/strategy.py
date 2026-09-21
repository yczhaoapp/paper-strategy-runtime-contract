# Generated, reviewable package entrypoint.
from psrc.strategies.reinforcement_learning import (
    LinearActorCriticAllocationStrategy as _BundledStrategy,
)


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
