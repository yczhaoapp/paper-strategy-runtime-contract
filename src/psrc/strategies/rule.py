from __future__ import annotations

from collections import deque
from decimal import Decimal
from math import log

from psrc.contract.models import ActionKind, DataKind, StrategyKind, TrainingMode
from psrc.domain.account import AccountSnapshot
from psrc.domain.actions import Action, NoOp, ReplaceOrder, SubmitOrder, TargetPosition
from psrc.domain.market import (
    BarPayload,
    BookSnapshotL2Payload,
    MarketEvent,
    QuoteL1Payload,
)
from psrc.strategies.common import bar_requirement, event_requirement, make_manifest


def weighted_microprice(
    bid_price: Decimal,
    bid_size: Decimal,
    ask_price: Decimal,
    ask_size: Decimal,
) -> Decimal:
    """Stoikov's weighted-mid toy model: I * ask + (1-I) * bid."""
    total = bid_size + ask_size
    if total <= 0:
        raise ValueError("microprice requires positive displayed size")
    imbalance = bid_size / total
    return imbalance * ask_price + (Decimal("1") - imbalance) * bid_price


def avellaneda_stoikov_quotes(
    *,
    mid: Decimal,
    inventory: Decimal,
    risk_aversion: Decimal,
    variance: Decimal,
    remaining_time: Decimal,
    arrival_decay: Decimal,
) -> tuple[Decimal, Decimal]:
    """Equations (29)-(30) of Avellaneda and Stoikov (2008)."""
    if risk_aversion <= 0 or variance < 0 or remaining_time < 0 or arrival_decay <= 0:
        raise ValueError("invalid Avellaneda-Stoikov parameter")
    reservation = mid - inventory * risk_aversion * variance * remaining_time
    spread = risk_aversion * variance * remaining_time + Decimal(
        str(2 / float(risk_aversion) * log(1 + float(risk_aversion / arrival_decay)))
    )
    return reservation - spread / 2, reservation + spread / 2


class DonchianBreakoutStrategy:
    manifest = make_manifest(
        strategy_id="rule.donchian_breakout",
        kind=StrategyKind.RULE,
        entrypoint="psrc.strategies.rule:DonchianBreakoutStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("PUBLIC.AAPL",), lookback=5),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        training=TrainingMode.NOT_REQUIRED,
        max_position=Decimal("2"),
    )

    def __init__(self, lookback: int = 5) -> None:
        self.lookback = lookback
        self.highs: deque[Decimal] = deque(maxlen=lookback)
        self.lows: deque[Decimal] = deque(maxlen=lookback)

    def on_start(self) -> None:
        self.highs.clear()
        self.lows.clear()

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires daily bars"),)
        payload = event.payload
        if len(self.highs) < self.lookback:
            self.highs.append(payload.high)
            self.lows.append(payload.low)
            return (NoOp(reason_code="warmup.donchian", explanation="channel is incomplete"),)
        upper, lower = max(self.highs), min(self.lows)
        self.highs.append(payload.high)
        self.lows.append(payload.low)
        if payload.close > upper:
            target = Decimal("2")
        elif payload.close < lower:
            target = Decimal("-2")
        else:
            return (NoOp(reason_code="signal.inside_channel", explanation="no breakout"),)
        return (
            TargetPosition(
                instrument_id=event.instrument_id,
                quantity=target,
                reason_code="signal.donchian_breakout",
            ),
        )

    def on_finish(self) -> None:
        pass


class PairsZScoreStrategy:
    manifest = make_manifest(
        strategy_id="rule.pairs_zscore",
        kind=StrategyKind.RULE,
        entrypoint="psrc.strategies.rule:PairsZScoreStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        data=(bar_requirement(interval="P1D", symbols=("PUBLIC.AAPL", "PUBLIC.MSFT"), lookback=6),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.TARGET_POSITION}),
        training=TrainingMode.NOT_REQUIRED,
        max_position=Decimal("1"),
    )

    def __init__(self, lookback: int = 6, threshold: Decimal = Decimal("1")) -> None:
        self.symbols = ("PUBLIC.AAPL", "PUBLIC.MSFT")
        self.latest: dict[str, tuple[object, Decimal]] = {}
        self.lookback = lookback
        self.spreads: deque[Decimal] = deque(maxlen=lookback)
        self.threshold = threshold

    def on_start(self) -> None:
        self.latest.clear()
        self.spreads.clear()

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires synchronized bars"),)
        self.latest[event.instrument_id] = (event.available_time, event.payload.close)
        if any(symbol not in self.latest for symbol in self.symbols):
            return (NoOp(reason_code="pair.awaiting_peer", explanation="peer bar is unavailable"),)
        left_time, left = self.latest[self.symbols[0]]
        right_time, right = self.latest[self.symbols[1]]
        if left_time != right_time:
            return (NoOp(reason_code="pair.not_synchronized", explanation="bar times differ"),)
        spread = left - right
        if len(self.spreads) < self.lookback:
            self.spreads.append(spread)
            return (NoOp(reason_code="warmup.spread", explanation="spread history is incomplete"),)
        mean = sum(self.spreads, Decimal("0")) / len(self.spreads)
        variance = sum(((value - mean) ** 2 for value in self.spreads), Decimal("0")) / Decimal(
            len(self.spreads)
        )
        self.spreads.append(spread)
        if variance == 0:
            return (NoOp(reason_code="spread.zero_variance", explanation="z-score is undefined"),)
        zscore = (spread - mean) / variance.sqrt()
        if abs(zscore) < self.threshold:
            targets = (Decimal("0"), Decimal("0"))
        elif zscore > 0:
            targets = (Decimal("-1"), Decimal("1"))
        else:
            targets = (Decimal("1"), Decimal("-1"))
        return tuple(
            TargetPosition(
                instrument_id=symbol,
                quantity=target,
                reason_code="signal.pairs_zscore",
            )
            for symbol, target in zip(self.symbols, targets, strict=True)
        )

    def on_finish(self) -> None:
        pass


class L1MicropriceStrategy:
    manifest = make_manifest(
        strategy_id="rule.l1_microprice",
        kind=StrategyKind.RULE,
        entrypoint="psrc.strategies.rule:L1MicropriceStrategy",
        profiles=frozenset({"event.l1.v1", "execution.basic.v1"}),
        data=(
            event_requirement(
                stream_id="quotes",
                kind=DataKind.QUOTE_L1,
                symbols=("SYNTH.L1",),
                fields=frozenset({"bid_price", "bid_size", "ask_price", "ask_size"}),
            ),
        ),
        actions=frozenset({ActionKind.NO_OP, ActionKind.SUBMIT_ORDER}),
        training=TrainingMode.NOT_REQUIRED,
        max_order=Decimal("1"),
    )

    def __init__(self, threshold_bps: Decimal = Decimal("0.5")) -> None:
        self.threshold_bps = threshold_bps
        self.counter = 0

    def on_start(self) -> None:
        self.counter = 0

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, QuoteL1Payload):
            return (NoOp(reason_code="event.not_l1", explanation="requires L1 quote"),)
        quote = event.payload
        total = quote.bid_size + quote.ask_size
        if total <= 0:
            return (NoOp(reason_code="quote.empty_sizes", explanation="microprice is undefined"),)
        mid = (quote.bid_price + quote.ask_price) / 2
        microprice = weighted_microprice(
            quote.bid_price, quote.bid_size, quote.ask_price, quote.ask_size
        )
        displacement_bps = (microprice - mid) / mid * Decimal("10000")
        if abs(displacement_bps) < self.threshold_bps:
            return (NoOp(reason_code="signal.weak_microprice", explanation="below threshold"),)
        self.counter += 1
        return (
            SubmitOrder(
                client_order_id=f"micro:{self.counter}",
                instrument_id=event.instrument_id,
                side="buy" if displacement_bps > 0 else "sell",
                order_type="market",
                quantity=Decimal("1"),
                reason_code="signal.microprice",
            ),
        )

    def on_finish(self) -> None:
        pass


class L2ImbalanceMakerStrategy:
    manifest = make_manifest(
        strategy_id="rule.l2_imbalance_maker",
        kind=StrategyKind.RULE,
        entrypoint="psrc.strategies.rule:L2ImbalanceMakerStrategy",
        profiles=frozenset({"event.l2.v1", "execution.advanced.v1"}),
        data=(
            event_requirement(
                stream_id="book",
                kind=DataKind.BOOK_SNAPSHOT_L2,
                symbols=("SYNTH.L2",),
                fields=frozenset({"bids.price", "bids.size", "asks.price", "asks.size"}),
                depth=3,
            ),
        ),
        actions=frozenset({ActionKind.NO_OP, ActionKind.SUBMIT_ORDER, ActionKind.REPLACE_ORDER}),
        training=TrainingMode.NOT_REQUIRED,
        max_order=Decimal("1"),
    )

    def __init__(
        self,
        *,
        risk_aversion: Decimal = Decimal("0.1"),
        volatility: Decimal = Decimal("0.02"),
        arrival_decay: Decimal = Decimal("200"),
        horizon_events: int = 10,
    ) -> None:
        self.risk_aversion = risk_aversion
        self.variance = volatility**2
        self.arrival_decay = arrival_decay
        self.horizon_events = horizon_events
        self.event_index = 0

    def on_start(self) -> None:
        self.event_index = 0

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        if not isinstance(event.payload, BookSnapshotL2Payload):
            return (NoOp(reason_code="event.not_l2", explanation="requires L2 snapshot"),)
        book = event.payload
        if len(book.bids) < 2 or len(book.asks) < 2:
            return (NoOp(reason_code="book.depth_insufficient", explanation="requires depth 2"),)
        self.event_index += 1
        mid = (book.bids[0].price + book.asks[0].price) / 2
        remaining = Decimal(max(0, self.horizon_events - self.event_index)) / Decimal(
            self.horizon_events
        )
        inventory = next(
            (
                position.quantity
                for position in account.positions
                if position.instrument_id == event.instrument_id
            ),
            Decimal("0"),
        )
        bid_price, ask_price = avellaneda_stoikov_quotes(
            mid=mid,
            inventory=inventory,
            risk_aversion=self.risk_aversion,
            variance=self.variance,
            remaining_time=remaining,
            arrival_decay=self.arrival_decay,
        )
        live = {order.client_order_id for order in account.open_orders}
        actions: list[Action] = []
        if "maker:bid" not in live:
            actions.append(
                SubmitOrder(
                    client_order_id="maker:bid",
                    instrument_id=event.instrument_id,
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("1"),
                    limit_price=bid_price,
                    reason_code="quote.inventory_aware_bid",
                )
            )
        else:
            actions.append(
                ReplaceOrder(
                    client_order_id="maker:bid",
                    new_limit_price=bid_price,
                    reason_code="quote.refresh_bid",
                )
            )
        if "maker:ask" not in live:
            actions.append(
                SubmitOrder(
                    client_order_id="maker:ask",
                    instrument_id=event.instrument_id,
                    side="sell",
                    order_type="limit",
                    quantity=Decimal("1"),
                    limit_price=ask_price,
                    reason_code="quote.inventory_aware_ask",
                )
            )
        else:
            actions.append(
                ReplaceOrder(
                    client_order_id="maker:ask",
                    new_limit_price=ask_price,
                    reason_code="quote.refresh_ask",
                )
            )
        return tuple(actions)

    def on_finish(self) -> None:
        pass


class TwapExecutionStrategy:
    manifest = make_manifest(
        strategy_id="rule.twap_execution",
        kind=StrategyKind.RULE,
        entrypoint="psrc.strategies.rule:TwapExecutionStrategy",
        profiles=frozenset({"core.bar.v1", "execution.basic.v1"}),
        data=(bar_requirement(interval="PT1M", symbols=("SYNTH.TWAP",), lookback=1),),
        actions=frozenset({ActionKind.NO_OP, ActionKind.SUBMIT_ORDER}),
        training=TrainingMode.NOT_REQUIRED,
        max_position=None,
        max_order=Decimal("2"),
    )

    def __init__(self, total_quantity: Decimal = Decimal("8"), slices: int = 4) -> None:
        self.total_quantity = total_quantity
        self.slices = slices
        self.sent = 0

    def on_start(self) -> None:
        self.sent = 0

    def on_event(self, event: MarketEvent, account: AccountSnapshot) -> tuple[Action, ...]:
        del account
        if not isinstance(event.payload, BarPayload):
            return (NoOp(reason_code="event.not_bar", explanation="requires minute bars"),)
        if self.sent >= self.slices:
            return (NoOp(reason_code="twap.complete", explanation="parent order completed"),)
        self.sent += 1
        return (
            SubmitOrder(
                client_order_id=f"twap:{self.sent}",
                instrument_id=event.instrument_id,
                side="buy",
                order_type="market",
                quantity=self.total_quantity / self.slices,
                reason_code="execution.twap_slice",
            ),
        )

    def on_finish(self) -> None:
        pass
