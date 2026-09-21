from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from psrc.contract.hashing import sha256_model
from psrc.contract.models import (
    DataKind,
    DatasetManifest,
    DatasetStream,
    Timeframe,
    TimeframeMode,
)
from psrc.domain.market import (
    BarPayload,
    BookLevel,
    BookSnapshotL2Payload,
    MarketEvent,
    QuoteL1Payload,
)


def minute_bars() -> tuple[MarketEvent, ...]:
    closes = ("100", "101", "102", "103", "104", "103", "102", "101", "100", "99")
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    events: list[MarketEvent] = []
    previous = Decimal(closes[0])
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        timestamp = start + timedelta(minutes=index + 1)
        high = max(previous, close) + Decimal("0.5")
        low = min(previous, close) - Decimal("0.5")
        events.append(
            MarketEvent(
                event_id=f"bar:{index + 1}",
                instrument_id="SYNTH.TEST",
                event_time=timestamp,
                available_time=timestamp,
                receive_time=timestamp,
                sequence=index,
                source="synthetic.v1",
                payload=BarPayload(
                    open=previous,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal("1000") + index,
                ),
            )
        )
        previous = close
    return tuple(events)


def minute_bar_manifest(events: tuple[MarketEvent, ...]) -> DatasetManifest:
    payload_hash = sha256_model({"events": [event.model_dump(mode="json") for event in events]})
    schema_hash = sha256_model(BarPayload.model_json_schema())
    return DatasetManifest(
        dataset_id="synthetic.minute-bars",
        dataset_version="1.0.0",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        streams=(
            DatasetStream(
                stream_id="bars",
                kind=DataKind.BAR,
                timeframe=Timeframe(mode=TimeframeMode.BAR, interval="PT1M"),
                symbols=frozenset({"SYNTH.TEST"}),
                fields=frozenset({"open", "high", "low", "close", "volume"}),
                record_count=len(events),
                schema_sha256=schema_hash,
                data_sha256=payload_hash,
            ),
        ),
        public_or_synthetic=True,
    )


def daily_bars() -> tuple[MarketEvent, ...]:
    closes = ("100", "101", "100", "102", "101", "106", "108", "104", "98", "94")
    start = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
    return _bar_series(
        symbol="SYNTH.DAILY",
        closes=closes,
        start=start,
        step=timedelta(days=1),
        prefix="daily",
    )


def pair_daily_bars() -> tuple[MarketEvent, ...]:
    left = ("100", "101", "102", "101", "100", "101", "108", "105", "99", "96")
    right = ("50", "50.5", "51", "50.5", "50", "50.5", "50", "51", "52", "53")
    start = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
    events: list[MarketEvent] = []
    previous = {"SYNTH.PAIR-A": Decimal(left[0]), "SYNTH.PAIR-B": Decimal(right[0])}
    sequence = 0
    for index, (left_close, right_close) in enumerate(zip(left, right, strict=True)):
        timestamp = start + timedelta(days=index)
        for symbol, close_text in (
            ("SYNTH.PAIR-A", left_close),
            ("SYNTH.PAIR-B", right_close),
        ):
            close = Decimal(close_text)
            open_price = previous[symbol]
            events.append(
                MarketEvent(
                    event_id=f"pair:{sequence + 1}",
                    instrument_id=symbol,
                    event_time=timestamp,
                    available_time=timestamp,
                    receive_time=timestamp,
                    sequence=sequence,
                    source="synthetic.v1",
                    payload=BarPayload(
                        open=open_price,
                        high=max(open_price, close) + Decimal("0.5"),
                        low=min(open_price, close) - Decimal("0.5"),
                        close=close,
                        volume=Decimal("1000") + sequence,
                    ),
                )
            )
            previous[symbol] = close
            sequence += 1
    return tuple(events)


def cross_sectional_daily_bars() -> tuple[MarketEvent, ...]:
    series = {
        "SYNTH.XS-A": ("100", "102", "103", "105", "104", "108"),
        "SYNTH.XS-B": ("80", "79", "81", "80", "78", "77"),
        "SYNTH.XS-C": ("60", "61", "60", "62", "63", "62"),
    }
    start = datetime(2026, 1, 2, 16, 0, tzinfo=UTC)
    previous = {symbol: Decimal(values[0]) for symbol, values in series.items()}
    events: list[MarketEvent] = []
    sequence = 0
    for index in range(6):
        timestamp = start + timedelta(days=index)
        for symbol in sorted(series):
            close = Decimal(series[symbol][index])
            open_price = previous[symbol]
            events.append(
                MarketEvent(
                    event_id=f"xs:{sequence + 1}",
                    instrument_id=symbol,
                    event_time=timestamp,
                    available_time=timestamp,
                    receive_time=timestamp,
                    sequence=sequence,
                    source="synthetic.v1",
                    payload=BarPayload(
                        open=open_price,
                        high=max(open_price, close) + Decimal("0.25"),
                        low=min(open_price, close) - Decimal("0.25"),
                        close=close,
                        volume=Decimal("1500") + sequence * 5,
                    ),
                )
            )
            previous[symbol] = close
            sequence += 1
    return tuple(events)


def twap_bars() -> tuple[MarketEvent, ...]:
    return _bar_series(
        symbol="SYNTH.TWAP",
        closes=("25", "25.1", "25.2", "25.1", "25.3", "25.4"),
        start=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        step=timedelta(minutes=1),
        prefix="twap",
    )


def l1_quotes() -> tuple[MarketEvent, ...]:
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    events: list[MarketEvent] = []
    for index in range(10):
        mid = Decimal("100") + Decimal(index) / Decimal("10")
        bid_size = Decimal("9") if index % 2 == 0 else Decimal("2")
        ask_size = Decimal("2") if index % 2 == 0 else Decimal("9")
        timestamp = start + timedelta(seconds=index)
        events.append(
            MarketEvent(
                event_id=f"quote:{index + 1}",
                instrument_id="SYNTH.L1",
                event_time=timestamp,
                available_time=timestamp,
                receive_time=timestamp,
                sequence=index,
                source="synthetic.v1",
                payload=QuoteL1Payload(
                    bid_price=mid - Decimal("0.05"),
                    bid_size=bid_size,
                    ask_price=mid + Decimal("0.05"),
                    ask_size=ask_size,
                ),
            )
        )
    return tuple(events)


def l2_books() -> tuple[MarketEvent, ...]:
    start = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    events: list[MarketEvent] = []
    for index in range(10):
        mid = Decimal("200") + Decimal(index) / Decimal("10")
        bid_bias = Decimal("3") if index % 2 == 0 else Decimal("1")
        ask_bias = Decimal("1") if index % 2 == 0 else Decimal("3")
        timestamp = start + timedelta(milliseconds=100 * index)
        events.append(
            MarketEvent(
                event_id=f"book:{index + 1}",
                instrument_id="SYNTH.L2",
                event_time=timestamp,
                available_time=timestamp,
                receive_time=timestamp,
                sequence=index,
                source="synthetic.v1",
                payload=BookSnapshotL2Payload(
                    bids=tuple(
                        BookLevel(
                            price=mid - Decimal("0.05") - Decimal(level) / Decimal("10"),
                            size=bid_bias + level,
                        )
                        for level in range(3)
                    ),
                    asks=tuple(
                        BookLevel(
                            price=mid + Decimal("0.05") + Decimal(level) / Decimal("10"),
                            size=ask_bias + level,
                        )
                        for level in range(3)
                    ),
                ),
            )
        )
    return tuple(events)


def manifest_for_events(
    *,
    dataset_id: str,
    stream_id: str,
    kind: DataKind,
    timeframe: Timeframe,
    fields: frozenset[str],
    events: tuple[MarketEvent, ...],
) -> DatasetManifest:
    return DatasetManifest(
        dataset_id=dataset_id,
        dataset_version="1.0.0",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        streams=(
            DatasetStream(
                stream_id=stream_id,
                kind=kind,
                timeframe=timeframe,
                symbols=frozenset(event.instrument_id for event in events),
                fields=fields,
                record_count=len(events),
                schema_sha256=sha256_model({"kind": kind, "fields": sorted(fields)}),
                data_sha256=sha256_model(
                    {"events": [event.model_dump(mode="json") for event in events]}
                ),
            ),
        ),
        public_or_synthetic=True,
    )


def _bar_series(
    *,
    symbol: str,
    closes: tuple[str, ...],
    start: datetime,
    step: timedelta,
    prefix: str,
) -> tuple[MarketEvent, ...]:
    events: list[MarketEvent] = []
    previous = Decimal(closes[0])
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        timestamp = start + step * index
        events.append(
            MarketEvent(
                event_id=f"{prefix}:{index + 1}",
                instrument_id=symbol,
                event_time=timestamp,
                available_time=timestamp,
                receive_time=timestamp,
                sequence=index,
                source="synthetic.v1",
                payload=BarPayload(
                    open=previous,
                    high=max(previous, close) + Decimal("0.5"),
                    low=min(previous, close) - Decimal("0.5"),
                    close=close,
                    volume=Decimal("1000") + index,
                ),
            )
        )
        previous = close
    return tuple(events)
