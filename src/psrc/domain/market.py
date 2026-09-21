from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from psrc.contract.models import ContractModel, Identifier


class BarPayload(ContractModel):
    kind: Literal["bar"] = "bar"
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> BarPayload:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is below an OHLC component")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is above an OHLC component")
        return self


class TradePayload(ContractModel):
    kind: Literal["trade"] = "trade"
    price: Decimal = Field(gt=0)
    size: Decimal = Field(gt=0)
    aggressor_side: Literal["buy", "sell", "unknown"] = "unknown"
    trade_id: str | None = None


class QuoteL1Payload(ContractModel):
    kind: Literal["quote_l1"] = "quote_l1"
    bid_price: Decimal = Field(gt=0)
    bid_size: Decimal = Field(ge=0)
    ask_price: Decimal = Field(gt=0)
    ask_size: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_spread(self) -> QuoteL1Payload:
        if self.ask_price < self.bid_price:
            raise ValueError("ask price must not be below bid price")
        return self


class BookLevel(ContractModel):
    price: Decimal = Field(gt=0)
    size: Decimal = Field(ge=0)


class BookSnapshotL2Payload(ContractModel):
    kind: Literal["book_snapshot_l2"] = "book_snapshot_l2"
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @model_validator(mode="after")
    def validate_book(self) -> BookSnapshotL2Payload:
        if not self.bids or not self.asks:
            raise ValueError("L2 snapshot requires at least one bid and ask")
        if any(
            left.price <= right.price for left, right in zip(self.bids, self.bids[1:], strict=False)
        ):
            raise ValueError("bids must be strictly descending")
        if any(
            left.price >= right.price for left, right in zip(self.asks, self.asks[1:], strict=False)
        ):
            raise ValueError("asks must be strictly ascending")
        if self.bids[0].price > self.asks[0].price:
            raise ValueError("crossed L2 book is invalid for the reference domain")
        return self


MarketPayload = Annotated[
    BarPayload | TradePayload | QuoteL1Payload | BookSnapshotL2Payload,
    Field(discriminator="kind"),
]


class MarketEvent(ContractModel):
    event_id: Identifier
    instrument_id: Identifier
    event_time: datetime
    available_time: datetime
    receive_time: datetime
    sequence: int = Field(ge=0)
    source: Identifier
    payload: MarketPayload

    @model_validator(mode="after")
    def validate_times(self) -> MarketEvent:
        times = (self.event_time, self.available_time, self.receive_time)
        if any(value.tzinfo is None for value in times):
            raise ValueError("market-event timestamps must be timezone-aware")
        if self.available_time < self.event_time:
            raise ValueError("available_time must not precede event_time")
        if self.receive_time < self.available_time:
            raise ValueError("receive_time must not precede available_time")
        return self
