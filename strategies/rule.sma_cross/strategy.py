# Generated, reviewable package entrypoint.
from psrc.examples.sma_cross import (
    SmaCrossStrategy as _BundledStrategy,
)


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
