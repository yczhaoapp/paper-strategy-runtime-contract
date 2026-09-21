from __future__ import annotations

from pathlib import Path

from psrc.adapters.backtrader import capabilities as backtrader_capabilities
from psrc.adapters.profiles import discover_engine_profiles
from psrc.adapters.reference import capabilities as reference_capabilities
from psrc.contract.models import SupportLevel


def test_engine_profile_inventory_is_valid_and_honest() -> None:
    root = Path(__file__).parents[2] / "engine_profiles"
    profiles = discover_engine_profiles(root)
    by_id = {profile.engine_id: profile for profile in profiles}
    assert set(by_id) == {"reference", "backtrader", "nautilus-trader", "lean", "qlib", "vnpy"}
    assert {
        engine_id
        for engine_id, profile in by_id.items()
        if profile.support_level == SupportLevel.CONFORMANCE_VERIFIED
    } == {"reference", "backtrader"}
    assert {
        engine_id
        for engine_id, profile in by_id.items()
        if profile.support_level == SupportLevel.PROFILED
    } == {"lean", "qlib", "vnpy"}
    for engine_id in ("lean", "qlib", "vnpy"):
        assert by_id[engine_id].sandbox_modes == frozenset()
        assert by_id[engine_id].extensions

    live_profiles = {
        "reference": reference_capabilities(),
        "backtrader": backtrader_capabilities(),
    }
    for engine_id, live in live_profiles.items():
        declared = by_id[engine_id]
        assert declared.engine_version == live.engine_version
        assert declared.adapter_version == live.adapter_version
        assert declared.support_level == live.support_level
        assert declared.profiles == live.profiles
        assert declared.data_kinds == live.data_kinds
        assert declared.action_kinds == live.action_kinds
        assert declared.execution == live.execution
