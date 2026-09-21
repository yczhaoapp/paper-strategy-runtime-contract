from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from psrc.contract.hashing import sha256_model
from psrc.contract.models import ActionRequirements, DataRequirement, StrategyManifest
from psrc.examples.sma_cross import SmaCrossStrategy


@given(st.permutations(["open", "high", "low", "close", "volume"]))
def test_manifest_hash_is_independent_of_set_input_order(fields: list[str]) -> None:
    canonical = SmaCrossStrategy.manifest
    requirement = canonical.data_requirements[0]
    reordered_requirement = DataRequirement.model_validate(
        {
            **requirement.model_dump(mode="python"),
            "required_fields": fields,
        }
    )
    reordered = StrategyManifest.model_validate(
        {
            **canonical.model_dump(mode="python"),
            "data_requirements": [reordered_requirement.model_dump(mode="python")],
        }
    )
    assert sha256_model(reordered) == sha256_model(canonical)


@given(
    max_position=st.decimals(
        min_value="0.000001", max_value="1000000", places=6, allow_nan=False, allow_infinity=False
    )
)
def test_positive_position_limits_round_trip_exactly(max_position: Decimal) -> None:
    requirements = ActionRequirements(
        allowed=SmaCrossStrategy.manifest.action_requirements.allowed,
        max_abs_position=max_position,
    )
    restored = ActionRequirements.model_validate_json(requirements.model_dump_json())
    assert restored == requirements
