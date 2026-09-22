# Generated, reviewable package entrypoint.
# Bundled implementation: GaussianVolumeBreakoutStrategy
from psrc.strategy_api import bundled_strategy_class

_BundledStrategy = bundled_strategy_class("supervised.gaussian_volume_breakout")


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
