from __future__ import annotations

from decimal import Decimal
from random import Random

import pytest

from psrc.domain.position_ledger import CostBasis, PositionLedger


def test_cost_basis_handles_partial_close_flat_and_reversal() -> None:
    state = CostBasis()
    for delta, price, expected in (
        ("2", "100", ("2", "100", "0")),
        ("1", "103", ("3", "101", "0")),
        ("-2", "105", ("1", "101", "8")),
        ("-3", "99", ("-2", "99", "6")),
        ("2", "97", ("0", "0", "10")),
    ):
        state = state.after_fill(Decimal(delta), Decimal(price))
        assert (state.quantity, state.average_price, state.realized_pnl) == tuple(
            Decimal(value) for value in expected
        )


def test_random_fill_sequences_obey_mark_to_market_cash_identity() -> None:
    rng = Random(220923)
    ledger = PositionLedger()
    cash_flow: dict[str, Decimal] = {}
    for _ in range(160):
        instrument = rng.choice(("AAA", "BBB", "CCC"))
        delta = Decimal(rng.choice((-4, -2, -1, 1, 2, 4)))
        price = Decimal(rng.randrange(50, 200)) / Decimal("2")
        cash_flow[instrument] = cash_flow.get(instrument, Decimal("0")) - delta * price
        ledger.apply_fill(instrument, delta, price)
        for symbol in ledger.instruments():
            mark = Decimal(rng.randrange(50, 200)) / Decimal("2")
            position = ledger.state(symbol).position(symbol, mark)
            assert abs(
                position.realized_pnl
                + position.unrealized_pnl
                - cash_flow[symbol]
                - position.quantity * mark
            ) <= Decimal("1e-20")
            if position.quantity == 0:
                assert position.average_price == 0
                assert position.unrealized_pnl == 0


@pytest.mark.parametrize("quantity,price", [("0", "100"), ("1", "0"), ("-1", "-5")])
def test_cost_basis_rejects_non_fills(quantity: str, price: str) -> None:
    with pytest.raises(ValueError):
        CostBasis().after_fill(Decimal(quantity), Decimal(price))
