from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from psrc.adapters.base import BacktestAdapter
from psrc.adapters.reference import ReferenceEngine
from psrc.adapters.reference import capabilities as reference_capabilities
from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import EngineCapabilities

EngineId = Literal["reference", "backtrader", "nautilus-trader"]
ENGINE_IDS: tuple[EngineId, ...] = ("reference", "backtrader", "nautilus-trader")


@dataclass(frozen=True)
class ResolvedAdapter:
    engine: BacktestAdapter
    capabilities: EngineCapabilities


def resolve_adapter(engine_id: EngineId, *, strict_container: bool) -> ResolvedAdapter:
    """Resolve an executable engine without importing optional adapters prematurely."""
    if engine_id == "reference":
        declared = reference_capabilities(strict_container=strict_container)
        return ResolvedAdapter(
            engine=ReferenceEngine(declared_capabilities=declared),
            capabilities=declared,
        )
    try:
        if engine_id == "backtrader":
            from psrc.adapters.backtrader import BacktraderAdapter, capabilities

            declared = capabilities(strict_container=strict_container)
            return ResolvedAdapter(
                engine=BacktraderAdapter(declared_capabilities=declared),
                capabilities=declared,
            )
        from psrc.adapters.nautilus import NautilusAdapter, capabilities

        declared = capabilities(strict_container=strict_container)
        return ResolvedAdapter(
            engine=NautilusAdapter(declared_capabilities=declared),
            capabilities=declared,
        )
    except ImportError as exc:
        raise ContractViolation(
            ContractError(
                run_id=f"adapter.resolve.{engine_id}",
                stage=ErrorStage.INITIALIZATION,
                code=ErrorCode.ENGINE_DEPENDENCY_MISSING,
                message="Selected native engine adapter dependencies are unavailable",
                engine_id=engine_id,
                details={
                    "selected_engine": engine_id,
                    "install": "uv sync --extra adapters",
                    "fallback_used": False,
                },
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
