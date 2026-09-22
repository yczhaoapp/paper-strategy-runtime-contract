# Generated, reviewable package entrypoint.
# Bundled implementation: DoubleQBookInventoryStrategy
from psrc.strategy_api import bundled_strategy_class

_BundledStrategy = bundled_strategy_class("reinforcement_learning.double_q_book_inventory")


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
