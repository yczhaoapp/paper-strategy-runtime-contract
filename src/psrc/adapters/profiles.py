from __future__ import annotations

from pathlib import Path

import yaml

from psrc.contract.models import EngineCapabilities


def load_engine_profile(path: Path) -> EngineCapabilities:
    with path.open("r", encoding="utf-8") as handle:
        return EngineCapabilities.model_validate(yaml.safe_load(handle))


def discover_engine_profiles(root: Path) -> tuple[EngineCapabilities, ...]:
    profiles = (load_engine_profile(path) for path in root.glob("*.yaml"))
    return tuple(sorted(profiles, key=lambda profile: profile.engine_id))
