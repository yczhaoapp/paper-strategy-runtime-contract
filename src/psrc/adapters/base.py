from __future__ import annotations

from typing import Protocol

from psrc.contract.models import EngineCapabilities, ExecutionPlan, SandboxMode
from psrc.domain.market import MarketEvent
from psrc.runtime.report import RunReport
from psrc.runtime.strategy import RuntimeStrategy


class BacktestAdapter(Protocol):
    """Stable execution boundary implemented by every engine bridge."""

    @property
    def capabilities(self) -> EngineCapabilities: ...

    def run(
        self,
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        events: tuple[MarketEvent, ...],
        sandbox_mode: SandboxMode,
    ) -> RunReport: ...
