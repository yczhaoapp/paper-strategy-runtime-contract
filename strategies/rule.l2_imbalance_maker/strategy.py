# Generated, reviewable package entrypoint.
from psrc.strategies.rule import (
    L2ImbalanceMakerStrategy as _BundledStrategy,
)


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
