# Generated, reviewable package entrypoint.
# Bundled implementation: PairsZScoreStrategy
from psrc.strategy_api import bundled_strategy_class

_BundledStrategy = bundled_strategy_class("rule.pairs_zscore")


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
