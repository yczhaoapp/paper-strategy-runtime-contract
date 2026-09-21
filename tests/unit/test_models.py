from __future__ import annotations

import pytest
from pydantic import ValidationError

from psrc.contract.models import ResourcePolicy, StrategyManifest, Timeframe, TimeframeMode
from psrc.examples.sma_cross import SmaCrossStrategy


def test_event_timeframe_rejects_interval() -> None:
    with pytest.raises(ValidationError):
        Timeframe(mode=TimeframeMode.EVENT, interval="PT1S")


def test_bar_timeframe_requires_interval() -> None:
    with pytest.raises(ValidationError):
        Timeframe(mode=TimeframeMode.BAR)


def test_extensions_require_reverse_dns_namespace() -> None:
    payload = SmaCrossStrategy.manifest.model_dump(mode="python")
    payload["extensions"] = {"backtrader": {"cheat_on_close": False}}
    with pytest.raises(ValidationError):
        StrategyManifest.model_validate(payload)

    payload["extensions"] = {"org.backtrader": {"cheat_on_close": False}}
    validated = StrategyManifest.model_validate(payload)
    assert validated.extensions == payload["extensions"]


def test_import_allow_list_cannot_override_denied_resources() -> None:
    with pytest.raises(ValidationError):
        ResourcePolicy(network="deny", allowed_imports=frozenset({"numpy", "socket"}))
    with pytest.raises(ValidationError):
        ResourcePolicy(
            filesystem="artifact_store_only",
            allowed_imports=frozenset({"numpy", "pathlib"}),
        )
