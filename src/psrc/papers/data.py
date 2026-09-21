from __future__ import annotations

import csv
import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from psrc.contract.hashing import sha256_model
from psrc.domain.market import BarPayload, MarketEvent, QuoteL1Payload
from psrc.papers.algorithms import imbalance
from psrc.papers.ingest import digest
from psrc.papers.models import DataSourceEvidence, PaperRecipe, PublicDatasetSource
from psrc.runtime.training import RLTransition, TrainingRequest


def _read_public_source(
    record_file: str,
) -> tuple[Path, PublicDatasetSource, tuple[dict[str, str], ...]]:
    root = Path(__file__).parents[3] / "data/public"
    source_record_path = root / record_file
    source_record = PublicDatasetSource.model_validate(
        json.loads(source_record_path.read_text(encoding="utf-8"))
    )
    path = root / source_record.file
    payload = path.read_bytes()
    if digest(payload) != source_record.source_sha256:
        raise ValueError("public historical fixture hash differs from reviewed bytes")
    license_payload = (root / source_record.repository_license_file).read_bytes()
    if digest(license_payload) != source_record.repository_license_sha256:
        raise ValueError("public historical fixture license differs from reviewed bytes")
    reader = csv.DictReader(payload.decode("utf-8").splitlines())
    if not set(source_record.columns_used).issubset(reader.fieldnames or ()):
        raise ValueError("public historical fixture columns differ from source record")
    rows = tuple(reader)
    if len(rows) != source_record.records:
        raise ValueError("public historical fixture row count differs from source record")
    if not rows or (rows[0]["Date"], rows[-1]["Date"]) != source_record.period:
        raise ValueError("public historical fixture period differs from source record")
    return source_record_path, source_record, rows


def quote_fixture() -> tuple[MarketEvent, ...]:
    """Seeded public simulation; prices follow noisy queue-dependent innovations."""
    rng = random.Random(20260921)
    mid = Decimal("100")
    result = []
    for index in range(241):
        signal = rng.choice((-0.8, -0.4, 0.0, 0.4, 0.8))
        ts = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
        result.append(
            MarketEvent(
                event_id=f"paper.quote:{index}",
                instrument_id="PAPER.SIM",
                event_time=ts,
                available_time=ts,
                receive_time=ts,
                sequence=index,
                source="paper.synthetic.v1",
                payload=QuoteL1Payload(
                    bid_price=mid - Decimal("0.01"),
                    ask_price=mid + Decimal("0.01"),
                    bid_size=Decimal(str(100 * (1 + signal))),
                    ask_size=Decimal(str(100 * (1 - signal))),
                ),
            )
        )
        direction = 1 if rng.random() < (0.5 + 0.3 * signal) else -1
        # Includes unchanged midprices, so labels must search for the NEXT price change.
        if index % 5 != 0:
            mid += Decimal(direction) * Decimal("0.04")
    return tuple(result)


def public_daily_bars() -> tuple[tuple[MarketEvent, ...], DataSourceEvidence]:
    """Load the immutable public AAPL OHLCV sample shipped with the repository."""
    source_record_path, source_record, rows = _read_public_source("source.json")
    events = []
    for index, row in enumerate(rows):
        timestamp = datetime.fromisoformat(row["Date"]).replace(tzinfo=UTC)
        events.append(
            MarketEvent(
                event_id=f"public.aapl:{index}",
                instrument_id=f"PUBLIC.{source_record.instrument}",
                event_time=timestamp,
                available_time=timestamp + timedelta(days=1),
                receive_time=timestamp + timedelta(days=1),
                sequence=index,
                source=f"public.fixture.sha256-{source_record.source_sha256[:8]}",
                payload=BarPayload(
                    open=Decimal(row["AAPL.Open"]),
                    high=Decimal(row["AAPL.High"]),
                    low=Decimal(row["AAPL.Low"]),
                    close=Decimal(row["AAPL.Close"]),
                    volume=Decimal(row["AAPL.Volume"]),
                ),
            )
        )
    evidence = DataSourceEvidence(
        origin="public_historical",
        dataset_id=source_record.dataset_id,
        source_url=source_record.source_url,
        source_sha256=source_record.source_sha256,
        provenance_record_sha256=digest(source_record_path.read_bytes()),
        license_name=(
            f"{source_record.repository_license} repository license; "
            "upstream market-data origin not separately stated"
        ),
        license_url=source_record.repository_license_url,
        license_sha256=source_record.repository_license_sha256,
        instrument_id=f"PUBLIC.{source_record.instrument}",
        period_start=events[0].event_time.date().isoformat(),
        period_end=events[-1].event_time.date().isoformat(),
        record_count=len(events),
        transforms=(
            "Parse published Date and AAPL OHLCV columns without price adjustment",
            "Set availability to the next UTC day to prevent same-bar look-ahead",
        ),
    )
    return tuple(events), evidence


def public_pair_daily_bars() -> tuple[tuple[MarketEvent, ...], DataSourceEvidence]:
    """Load immutable public AAPL/MSFT closes as synchronized daily bar events."""
    source_record_path, source_record, rows = _read_public_source("stockdata-source.json")
    events = []
    symbols = ("AAPL", "MSFT")
    for index, row in enumerate(rows):
        timestamp = datetime.fromisoformat(row["Date"]).replace(tzinfo=UTC)
        for offset, symbol in enumerate(symbols):
            price = Decimal(row[symbol])
            events.append(
                MarketEvent(
                    event_id=f"public.aapl-msft:{index}:{symbol.lower()}",
                    instrument_id=f"PUBLIC.{symbol}",
                    event_time=timestamp,
                    available_time=timestamp + timedelta(days=1),
                    receive_time=timestamp + timedelta(days=1),
                    sequence=index * len(symbols) + offset,
                    source=f"public.fixture.sha256-{source_record.source_sha256[:8]}",
                    payload=BarPayload(
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=Decimal("1"),
                    ),
                )
            )
    evidence = DataSourceEvidence(
        origin="public_historical",
        dataset_id=source_record.dataset_id,
        source_url=source_record.source_url,
        source_sha256=source_record.source_sha256,
        provenance_record_sha256=digest(source_record_path.read_bytes()),
        license_name=(
            f"{source_record.repository_license} repository license; "
            "upstream market-data origin not separately stated"
        ),
        license_url=source_record.repository_license_url,
        license_sha256=source_record.repository_license_sha256,
        instrument_id="PUBLIC.AAPL-MSFT",
        period_start=events[0].event_time.date().isoformat(),
        period_end=events[-1].event_time.date().isoformat(),
        record_count=len(events),
        transforms=(
            "Select published Date, AAPL and MSFT close columns",
            "Expand each close to a flat OHLC bar with unit placeholder volume",
            "Set availability to the next UTC day to prevent same-bar look-ahead",
        ),
    )
    return tuple(events), evidence


def labeled_observations(
    events: tuple[MarketEvent, ...],
) -> tuple[tuple[tuple[float, ...], ...], tuple[float, ...], tuple[tuple[int, int], ...]]:
    features, labels, pairs = [], [], []
    for i, event in enumerate(events[:-1]):
        q = event.payload
        if not isinstance(q, QuoteL1Payload):
            raise ValueError("labels require quotes")
        mid = q.bid_price + q.ask_price
        for j in range(i + 1, len(events)):
            future = events[j].payload
            if not isinstance(future, QuoteL1Payload):
                raise ValueError("labels require quotes")
            if future.bid_price + future.ask_price != mid:
                features.append((imbalance(float(q.bid_size), float(q.ask_size)),))
                labels.append(float(future.bid_price + future.ask_price > mid))
                pairs.append((i, j))
                break
    return tuple(features), tuple(labels), tuple(pairs)


def execution_transitions(
    events: tuple[MarketEvent, ...], horizon: int, target: int, tick: float
) -> tuple[RLTransition, ...]:
    transitions = []
    for periods in range(1, horizon + 1):
        for inventory in range(1, target + 1):
            for i, (current, nxt) in enumerate(pairwise(events)):
                q, future = current.payload, nxt.payload
                assert isinstance(q, QuoteL1Payload) and isinstance(future, QuoteL1Payload)
                market = 1 if q.bid_size >= q.ask_size else -1
                next_market = 1 if future.bid_size >= future.ask_size else -1
                for action in range(3):
                    is_market = periods == 1 or action == 2
                    price = q.bid_price + (Decimal(str(tick)) if action == 1 else 0)
                    filled = inventory if is_market or future.ask_price <= price else 0
                    execution = future.ask_price * (Decimal("1.0001") if is_market else 1)
                    # Negative dollar implementation shortfall incl. fees; same reference semantics.
                    benchmark = (q.bid_price + q.ask_price) / 2
                    cost = float(filled * (execution * Decimal("1.0005") - benchmark))
                    # Add mark movement on remaining inventory to telescope the arrival benchmark.
                    next_mid = (future.bid_price + future.ask_price) / 2
                    cost += float((inventory - filled) * (next_mid - benchmark))
                    transitions.append(
                        RLTransition(
                            episode_id=f"counterfactual:{i}:{periods}:{inventory}",
                            step=0,
                            state=(float(periods), float(inventory), float(market)),
                            action=action,
                            reward=-cost,
                            next_state=(
                                float(periods - 1),
                                float(inventory - filled),
                                float(next_market),
                            ),
                            terminated=periods == 1 or filled == inventory,
                        )
                    )
    return tuple(transitions)


def paper_inputs(
    recipe: PaperRecipe,
) -> tuple[
    tuple[MarketEvent, ...],
    TrainingRequest | None,
    dict[str, object],
    DataSourceEvidence,
]:
    if recipe.algorithm == "moving_average_band":
        if recipe.data_mode != "public_historical":
            raise ValueError("moving-average paper case requires reviewed public historical data")
        bars, data_evidence = public_daily_bars()
        return (
            bars,
            None,
            {
                "data_origin": "public historical AAPL OHLCV; not original Chinese index data",
                "split": "rule strategy: no training; lookback warms up causally",
                "training_events": 0,
                "test_events": len(bars),
                "train_end": "1970-01-01T00:00:00+00:00",
                "test_start": bars[0].available_time.isoformat(),
            },
            data_evidence,
        )
    events = quote_fixture()
    train, test = events[:160], events[160:]
    metadata = {
        "data_origin": "synthetic; not NASDAQ or original-paper data",
        "split": "chronological; label endpoints constrained to training partition",
        "train_events_sha256": sha256_model({"events": [e.model_dump(mode="json") for e in train]}),
        "train_end": train[-1].available_time.isoformat(),
        "test_start": test[0].available_time.isoformat(),
    }
    request = None
    evidence: dict[str, object] = {
        **metadata,
        "training_events": len(train),
        "test_events": len(test),
        "label_endpoint_max": None,
    }
    if recipe.algorithm == "queue_logistic":
        x, y, pairs = labeled_observations(train)
        request = TrainingRequest(
            run_id=f"train.{recipe.paper_id}",
            dataset_id="paper.train",
            seed=20260921,
            features=x,
            labels=y,
            metadata=metadata,
        )
        evidence["label_endpoint_max"] = max(j for _, j in pairs)
        evidence["label_pairs"] = pairs
    elif recipe.algorithm == "execution_dynamic_q":
        horizon = int(recipe.parameters["horizon_events"])
        request = TrainingRequest(
            run_id=f"train.{recipe.paper_id}",
            dataset_id="paper.train",
            seed=20260921,
            transitions=execution_transitions(
                train, horizon, int(recipe.parameters["target_quantity"]), recipe.parameters["tick"]
            ),
            metadata=metadata,
        )
        test = test[
            : horizon + 1
        ]  # final quote settles the last decision; never a training sample.
        evidence["test_events"] = len(test)
    data_evidence = DataSourceEvidence(
        origin="synthetic_fixture",
        dataset_id=f"paper.{recipe.paper_id}.deterministic-fixture",
        source_sha256=sha256_model({"events": [event.model_dump(mode="json") for event in events]}),
        license_name="CC0-1.0 project-generated fixture",
        instrument_id="PAPER.SIM",
        period_start=events[0].event_time.isoformat(),
        period_end=events[-1].event_time.isoformat(),
        record_count=len(events),
        transforms=("Seeded generator 20260921; no external market observations",),
    )
    return test, request, evidence, data_evidence
