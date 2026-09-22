from __future__ import annotations

from psrc.constants import CONTRACT_VERSION
from psrc.contract.models import RuntimeCapabilities


def capabilities() -> RuntimeCapabilities:
    return RuntimeCapabilities(
        runtime_id="psrc.orchestrator",
        runtime_version=CONTRACT_VERSION,
        profiles=frozenset({"training.rl.v1", "training.supervised.v1"}),
    )
