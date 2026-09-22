from __future__ import annotations

from abc import ABC, abstractmethod
from typing import final

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import EngineCapabilities, ExecutionPlan, SandboxMode
from psrc.domain.market import MarketEvent
from psrc.runtime.guards import prepare_adapter_invocation
from psrc.runtime.report import RunReport
from psrc.runtime.strategy import RuntimeStrategy


class BacktestAdapter(ABC):
    """Validated public execution boundary shared by every engine bridge."""

    @property
    @abstractmethod
    def capabilities(self) -> EngineCapabilities: ...

    @final
    def run(
        self,
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        events: tuple[MarketEvent, ...],
        sandbox_mode: SandboxMode,
    ) -> RunReport:
        try:
            effective_events = prepare_adapter_invocation(
                plan=plan,
                strategy=strategy,
                engine=self.capabilities,
                sandbox_mode=sandbox_mode,
                source_events=events,
            )
            return self._run_validated(
                plan=plan,
                strategy=strategy,
                events=effective_events,
                sandbox_mode=sandbox_mode,
            )
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation(
                ContractError(
                    run_id=plan.run_id,
                    strategy_id=plan.strategy_id,
                    engine_id=plan.engine_id,
                    stage=ErrorStage.BACKTEST,
                    code=ErrorCode.BACKTEST_FAILED,
                    message="Backtest adapter execution failed",
                    details={"adapter": type(self).__name__, "fallback_used": False},
                    cause_chain=(f"{type(exc).__name__}: {exc}",),
                )
            ) from exc

    @abstractmethod
    def _run_validated(
        self,
        *,
        plan: ExecutionPlan,
        strategy: RuntimeStrategy,
        events: tuple[MarketEvent, ...],
        sandbox_mode: SandboxMode,
    ) -> RunReport:
        """Execute events after the final public boundary has validated them."""
