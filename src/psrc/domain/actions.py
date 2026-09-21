from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, RootModel, model_validator

from psrc.contract.models import ContractModel, Identifier


class NoOp(ContractModel):
    kind: Literal["no_op"] = "no_op"
    reason_code: Identifier
    explanation: str


class Prediction(ContractModel):
    kind: Literal["prediction"] = "prediction"
    instrument_id: Identifier
    value: Decimal
    horizon: str
    model_artifact_id: Identifier
    reason_code: Identifier


class TargetPosition(ContractModel):
    kind: Literal["target_position"] = "target_position"
    instrument_id: Identifier
    quantity: Decimal
    reason_code: Identifier


class TargetWeight(ContractModel):
    kind: Literal["target_weight"] = "target_weight"
    instrument_id: Identifier
    weight: Decimal
    reason_code: Identifier

    @model_validator(mode="after")
    def validate_weight(self) -> TargetWeight:
        if abs(self.weight) > 1:
            raise ValueError("absolute target weight must not exceed one")
        return self


class SubmitOrder(ContractModel):
    kind: Literal["submit_order"] = "submit_order"
    client_order_id: Identifier
    instrument_id: Identifier
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    time_in_force: Literal["day", "gtc", "ioc", "fok"] = "day"
    reason_code: Identifier

    @model_validator(mode="after")
    def validate_prices(self) -> SubmitOrder:
        if self.order_type in {"limit", "stop_limit"} and self.limit_price is None:
            raise ValueError("limit and stop-limit orders require limit_price")
        if self.order_type in {"stop", "stop_limit"} and self.stop_price is None:
            raise ValueError("stop and stop-limit orders require stop_price")
        return self


class CancelOrder(ContractModel):
    kind: Literal["cancel_order"] = "cancel_order"
    client_order_id: Identifier
    reason_code: Identifier


class ReplaceOrder(ContractModel):
    kind: Literal["replace_order"] = "replace_order"
    client_order_id: Identifier
    new_quantity: Decimal | None = Field(default=None, gt=0)
    new_limit_price: Decimal | None = Field(default=None, gt=0)
    reason_code: Identifier


Action = Annotated[
    NoOp | Prediction | TargetPosition | TargetWeight | SubmitOrder | CancelOrder | ReplaceOrder,
    Field(discriminator="kind"),
]


class ActionEnvelope(RootModel[Action]):
    """Schema-export wrapper for the discriminated canonical action union."""

    pass
