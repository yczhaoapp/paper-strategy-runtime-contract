# Generated, reviewable package entrypoint.
from psrc.strategies.supervised import (
    L1AdverseSelectionStrategy as _BundledStrategy,
)


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
