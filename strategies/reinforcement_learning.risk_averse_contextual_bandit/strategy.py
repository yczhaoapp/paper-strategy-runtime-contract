# Generated, reviewable package entrypoint.
# Bundled implementation: RiskAverseContextualBanditStrategy
from psrc.strategy_api import bundled_strategy_class

_BundledStrategy = bundled_strategy_class("reinforcement_learning.risk_averse_contextual_bandit")


class Strategy(_BundledStrategy):
    manifest = _BundledStrategy.manifest.model_copy(
        update={"entrypoint": "strategy.py:Strategy"}
    )
