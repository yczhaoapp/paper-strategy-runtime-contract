from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from psrc.contract.models import CompatibilityResult, ExecutionPlan
from psrc.domain.market import BarPayload, MarketEvent


def apply_compatibility_plan(
    events: tuple[MarketEvent, ...], plan: ExecutionPlan
) -> tuple[MarketEvent, ...]:
    transformed = events
    for record in plan.compatibility:
        if record.result == CompatibilityResult.EXACT:
            continue
        if record.transformation_id == "symbol.map.v1":
            mapping = record.parameters.get("mapping")
            if not isinstance(mapping, dict) or not all(
                isinstance(source, str) and isinstance(target, str)
                for source, target in mapping.items()
            ):
                raise ValueError("symbol-map compatibility record lacks a valid mapping")
            transformed = tuple(
                event.model_copy(
                    update={
                        "instrument_id": mapping.get(event.instrument_id, event.instrument_id),
                        "source": f"compat.symbol-map.v1:{event.source}",
                    }
                )
                if event.instrument_id in mapping
                else event
                for event in transformed
            )
            continue
        if record.transformation_id == "bar.resample.v1":
            target = record.parameters.get("target_interval")
            if not isinstance(target, str):
                raise ValueError("resample compatibility record lacks target_interval")
            transformed = resample_bars(transformed, target)
            continue
        raise ValueError(f"unsupported planned transformation: {record.transformation_id}")
    return transformed


def _seconds(interval: str) -> int:
    units = {"S": 1, "M": 60, "H": 3600}
    if interval == "P1D":
        return 86400
    if interval.startswith("PT") and interval[-1] in units:
        return int(interval[2:-1]) * units[interval[-1]]
    raise ValueError(f"unsupported resample interval {interval!r}")


def resample_bars(events: tuple[MarketEvent, ...], target_interval: str) -> tuple[MarketEvent, ...]:
    interval_seconds = _seconds(target_interval)
    groups: dict[tuple[str, int], list[MarketEvent]] = defaultdict(list)
    for event in events:
        if not isinstance(event.payload, BarPayload):
            raise ValueError("bar resampling cannot consume non-bar events")
        # Bar timestamps denote interval closes, so an event exactly on a target
        # boundary belongs to the interval that just ended.
        bucket = (int(event.event_time.timestamp()) - 1) // interval_seconds
        groups[(event.instrument_id, bucket)].append(event)

    output: list[MarketEvent] = []
    for sequence, ((instrument_id, bucket), group) in enumerate(sorted(groups.items())):
        ordered = sorted(group, key=lambda event: (event.available_time, event.sequence))
        first = ordered[0]
        payloads = [event.payload for event in ordered]
        assert all(isinstance(payload, BarPayload) for payload in payloads)
        bars = [payload for payload in payloads if isinstance(payload, BarPayload)]
        bucket_end = datetime.fromtimestamp((bucket + 1) * interval_seconds, tz=UTC)
        output.append(
            MarketEvent(
                event_id=f"resampled:{instrument_id}:{bucket}",
                instrument_id=instrument_id,
                event_time=bucket_end,
                available_time=max(event.available_time for event in ordered),
                receive_time=max(event.receive_time for event in ordered),
                sequence=sequence,
                source=f"compat.bar-resample.v1:{first.source}",
                payload=BarPayload(
                    open=bars[0].open,
                    high=max(bar.high for bar in bars),
                    low=min(bar.low for bar in bars),
                    close=bars[-1].close,
                    volume=sum((bar.volume for bar in bars), Decimal("0")),
                ),
            )
        )
    return tuple(sorted(output, key=lambda event: (event.available_time, event.sequence)))
