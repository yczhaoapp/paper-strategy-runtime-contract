from __future__ import annotations

from typing import Protocol

from psrc.contract.models import StrategyManifest
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action
from psrc.domain.market import MarketEvent


class StrategyContext(Protocol):
    @property
    def manifest(self) -> StrategyManifest: ...


class RuntimeStrategy(Protocol):
    manifest: StrategyManifest

    def on_start(self) -> None: ...

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]: ...

    def on_finish(self) -> None: ...
