from __future__ import annotations

import pytest

from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.runtime.lifecycle import Lifecycle, LifecycleState


def test_valid_rule_lifecycle() -> None:
    lifecycle = Lifecycle(run_id="test.lifecycle", strategy_id="rule.test", engine_id="reference")
    for state in (
        LifecycleState.VALIDATED,
        LifecycleState.CAPABILITIES_NEGOTIATED,
        LifecycleState.INITIALIZED,
        LifecycleState.INFERENCE_BACKTEST,
        LifecycleState.FINALIZED,
    ):
        lifecycle.transition(state)
    assert lifecycle.state == LifecycleState.FINALIZED


def test_invalid_lifecycle_transition_is_structured() -> None:
    lifecycle = Lifecycle(run_id="test.lifecycle", strategy_id="rule.test", engine_id="reference")
    with pytest.raises(ContractViolation) as raised:
        lifecycle.transition(LifecycleState.INFERENCE_BACKTEST)
    assert raised.value.error.code == ErrorCode.LIFECYCLE_TRANSITION_INVALID


def test_running_name_remains_a_compatible_sdk_alias() -> None:
    assert LifecycleState.__members__["RUNNING"] is LifecycleState.INFERENCE_BACKTEST
