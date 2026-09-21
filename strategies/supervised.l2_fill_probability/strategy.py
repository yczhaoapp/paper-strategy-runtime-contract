# Generated, reviewable package entrypoint.
from psrc.strategies.supervised import (
    L2FillProbabilityStrategy as _BundledStrategy,
)


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
