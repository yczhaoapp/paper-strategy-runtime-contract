from __future__ import annotations

from enum import StrEnum

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage


class LifecycleState(StrEnum):
    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    CAPABILITIES_NEGOTIATED = "CAPABILITIES_NEGOTIATED"
    INITIALIZED = "INITIALIZED"
    TRAINING = "TRAINING"
    ARTIFACT_SAVED = "ARTIFACT_SAVED"
    ARTIFACT_LOADED = "ARTIFACT_LOADED"
    INFERENCE_BACKTEST = "INFERENCE_BACKTEST"
    RUNNING = "INFERENCE_BACKTEST"  # Contract 1.0 SDK compatibility alias.
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"


_ALLOWED: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.DISCOVERED: frozenset({LifecycleState.VALIDATED, LifecycleState.FAILED}),
    LifecycleState.VALIDATED: frozenset(
        {LifecycleState.CAPABILITIES_NEGOTIATED, LifecycleState.FAILED}
    ),
    LifecycleState.CAPABILITIES_NEGOTIATED: frozenset(
        {LifecycleState.INITIALIZED, LifecycleState.FAILED}
    ),
    LifecycleState.INITIALIZED: frozenset(
        {
            LifecycleState.TRAINING,
            LifecycleState.ARTIFACT_LOADED,
            LifecycleState.RUNNING,
            LifecycleState.FAILED,
        }
    ),
    LifecycleState.TRAINING: frozenset({LifecycleState.ARTIFACT_SAVED, LifecycleState.FAILED}),
    LifecycleState.ARTIFACT_SAVED: frozenset(
        {LifecycleState.ARTIFACT_LOADED, LifecycleState.FAILED}
    ),
    LifecycleState.ARTIFACT_LOADED: frozenset({LifecycleState.RUNNING, LifecycleState.FAILED}),
    LifecycleState.RUNNING: frozenset({LifecycleState.FINALIZED, LifecycleState.FAILED}),
    LifecycleState.FINALIZED: frozenset(),
    LifecycleState.FAILED: frozenset(),
}


class Lifecycle:
    def __init__(self, *, run_id: str, strategy_id: str, engine_id: str) -> None:
        self.run_id = run_id
        self.strategy_id = strategy_id
        self.engine_id = engine_id
        self.state = LifecycleState.DISCOVERED
        self.history: list[LifecycleState] = [self.state]

    def transition(self, target: LifecycleState) -> None:
        if target not in _ALLOWED[self.state]:
            raise ContractViolation(
                ContractError(
                    run_id=self.run_id,
                    stage=ErrorStage.VALIDATION,
                    code=ErrorCode.LIFECYCLE_TRANSITION_INVALID,
                    message=f"Illegal lifecycle transition {self.state} -> {target}",
                    strategy_id=self.strategy_id,
                    engine_id=self.engine_id,
                    details={
                        "from": self.state,
                        "to": target,
                        "allowed": sorted(_ALLOWED[self.state]),
                    },
                )
            )
        self.state = target
        self.history.append(target)

    def fail(self) -> None:
        if self.state not in {LifecycleState.FINALIZED, LifecycleState.FAILED}:
            self.transition(LifecycleState.FAILED)
