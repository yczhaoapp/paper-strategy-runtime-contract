from __future__ import annotations

import math
from typing import Literal, Protocol

from pydantic import Field, model_validator

from psrc.constants import CONTRACT_VERSION
from psrc.contract.hashing import sha256_model
from psrc.contract.models import ContractModel, Identifier
from psrc.runtime.artifacts import ArtifactManifest, ArtifactStore
from psrc.runtime.strategy import RuntimeStrategy


class RLTransition(ContractModel):
    episode_id: Identifier
    step: int = Field(ge=0)
    state: tuple[float, ...]
    action: int = Field(ge=0)
    reward: float
    next_state: tuple[float, ...]
    next_action: int | None = Field(default=None, ge=0)
    terminated: bool
    truncated: bool = False


class TrainingRequest(ContractModel):
    run_id: Identifier
    dataset_id: Identifier
    seed: int
    features: tuple[tuple[float, ...], ...] = ()
    labels: tuple[float, ...] = ()
    transitions: tuple[RLTransition, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload_shape(self) -> TrainingRequest:
        supervised = bool(self.features or self.labels)
        reinforcement = bool(self.transitions)
        if supervised == reinforcement:
            raise ValueError(
                "training request must contain exactly one of supervised samples or RL transitions"
            )
        if supervised:
            if not self.features or len(self.features) != len(self.labels):
                raise ValueError("supervised features and labels must be non-empty and aligned")
            width = len(self.features[0])
            if width == 0 or any(len(row) != width for row in self.features):
                raise ValueError("supervised feature rows must have one consistent non-zero width")
            values = (*self.labels, *(value for row in self.features for value in row))
        else:
            state_width = len(self.transitions[0].state)
            if state_width == 0 or any(
                len(item.state) != state_width or len(item.next_state) != state_width
                for item in self.transitions
            ):
                raise ValueError("RL state and next-state rows must have one non-zero width")
            values = tuple(
                value
                for item in self.transitions
                for value in (*item.state, item.reward, *item.next_state)
            )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("training payload values must be finite")
        return self


class TrainingInputEvidence(ContractModel):
    """Immutable declaration of the exact embedded training payload."""

    contract_version: str = CONTRACT_VERSION
    dataset_id: Identifier
    kind: Literal["supervised", "reinforcement_learning"]
    record_count: int = Field(gt=0)
    input_width: int = Field(gt=0)
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def build_training_input_evidence(request: TrainingRequest) -> TrainingInputEvidence:
    if request.transitions:
        kind: Literal["supervised", "reinforcement_learning"] = "reinforcement_learning"
        record_count = len(request.transitions)
        input_width = len(request.transitions[0].state)
    else:
        kind = "supervised"
        record_count = len(request.features)
        input_width = len(request.features[0])
    return TrainingInputEvidence(
        dataset_id=request.dataset_id,
        kind=kind,
        record_count=record_count,
        input_width=input_width,
        request_sha256=sha256_model(request),
    )


def validate_training_input_evidence(
    request: TrainingRequest, evidence: TrainingInputEvidence
) -> None:
    expected = build_training_input_evidence(request)
    if evidence != expected:
        raise ValueError(
            "training input evidence does not match the embedded training request: "
            f"expected={expected.model_dump(mode='json')}, "
            f"actual={evidence.model_dump(mode='json')}"
        )


class TrainableStrategy(Protocol):
    def train(self, request: TrainingRequest, store: ArtifactStore) -> ArtifactManifest: ...

    def load(self, manifest: ArtifactManifest, store: ArtifactStore, *, run_id: str) -> None: ...


class TrainableRuntimeStrategy(RuntimeStrategy, TrainableStrategy, Protocol):
    pass
