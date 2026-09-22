# Generated, reviewable package entrypoint.
# Bundled implementation: L1AdverseSelectionStrategy
from psrc.strategy_api import bundled_strategy_class

_BundledStrategy = bundled_strategy_class("supervised.l1_adverse_selection")


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
