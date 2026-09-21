from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from psrc.contract.models import ContractModel, Identifier


class Position(ContractModel):
    instrument_id: Identifier
    quantity: Decimal
    average_price: Decimal = Field(ge=0)
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")


class OpenOrder(ContractModel):
    client_order_id: Identifier
    instrument_id: Identifier
    order_type: Literal["target", "market", "limit"]
    side: Literal["buy", "sell"] | None = None
    quantity: Decimal | None = Field(default=None, gt=0)
    target_quantity: Decimal | None = None
    limit_price: Decimal | None = Field(default=None, gt=0)
    status: Literal["accepted", "replaced", "partially_filled"] = "accepted"


class AccountSnapshot(ContractModel):
    timestamp: datetime
    base_currency: str = "USD"
    cash: Decimal
    equity: Decimal
    positions: tuple[Position, ...]
    open_orders: tuple[OpenOrder, ...] = ()


class Fill(ContractModel):
    fill_id: Identifier
    client_order_id: Identifier
    instrument_id: Identifier
    timestamp: datetime
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
