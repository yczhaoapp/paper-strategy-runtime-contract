from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from psrc.domain.account import Position


@dataclass(frozen=True)
class CostBasis:
    """Gross price P&L for a net position; commissions belong to cash and fills."""

    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

    def after_fill(self, signed_quantity: Decimal, price: Decimal) -> CostBasis:
        if signed_quantity == 0 or price <= 0:
            raise ValueError("a fill requires nonzero signed quantity and positive price")

        old_quantity = self.quantity
        new_quantity = old_quantity + signed_quantity
        if old_quantity == 0 or old_quantity * signed_quantity > 0:
            total_cost = abs(old_quantity) * self.average_price + abs(signed_quantity) * price
            average_price = total_cost / abs(new_quantity)
            realized_pnl = self.realized_pnl
        else:
            closed_quantity = min(abs(old_quantity), abs(signed_quantity))
            direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
            realized_pnl = (
                self.realized_pnl + closed_quantity * (price - self.average_price) * direction
            )
            if new_quantity == 0:
                average_price = Decimal("0")
            elif old_quantity * new_quantity < 0:
                average_price = price
            else:
                average_price = self.average_price
        return CostBasis(new_quantity, average_price, realized_pnl)

    def position(self, instrument_id: str, mark: Decimal) -> Position:
        if mark <= 0:
            raise ValueError("a position mark must be positive")
        return Position(
            instrument_id=instrument_id,
            quantity=self.quantity,
            average_price=self.average_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.quantity * (mark - self.average_price),
        )


class PositionLedger:
    """Engine-neutral cost basis built only from observed fills."""

    def __init__(self) -> None:
        self._positions: dict[str, CostBasis] = {}

    def apply_fill(self, instrument_id: str, signed_quantity: Decimal, price: Decimal) -> CostBasis:
        state = self.state(instrument_id).after_fill(signed_quantity, price)
        self._positions[instrument_id] = state
        return state

    def state(self, instrument_id: str) -> CostBasis:
        return self._positions.get(instrument_id, CostBasis())

    def instruments(self) -> tuple[str, ...]:
        return tuple(sorted(self._positions))
