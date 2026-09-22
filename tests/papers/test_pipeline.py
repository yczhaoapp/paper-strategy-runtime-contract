from __future__ import annotations

import json
import runpy
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter

from psrc.cli import main
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.domain.market import BarPayload
from psrc.papers.catalog import read_bindings, verify_strategy_bindings
from psrc.papers.compiler import compile_paper, extract_spec, read_recipe
from psrc.papers.data import public_daily_bars, public_pair_daily_bars, quote_fixture
from psrc.papers.evidence import verify_papers
from psrc.papers.ingest import digest, ingest
from psrc.papers.models import DataSourceEvidence, PublicDatasetSource
from psrc.papers.pipeline import reproduce, run_suite
from psrc.strategies.catalog import (
    _bar_features,
    _public_logistic_training,
    _public_pair_rows,
    _public_ridge_training,
)
from psrc.strategies.common import stable_float

ROOT = Path(__file__).parents[2]


@pytest.fixture
def source_recipe() -> tuple[Path, Path]:
    return ROOT / "papers/sources/avellaneda2008.pdf", ROOT / "papers/recipes/avellaneda2008.json"


def test_four_deep_reproductions_cover_three_kinds_and_independent_verifier(
    tmp_path: Path,
) -> None:
    summary = run_suite(ROOT / "papers/sources", ROOT / "papers/recipes", tmp_path)
    assert summary["paper_count"] == 4
    assert {r["kind"] for r in summary["runs"]} == {"rule", "supervised", "reinforcement_learning"}
    report = verify_papers(tmp_path, ROOT / "papers/sources", ROOT / "papers/recipes")
    assert report["status"] == "passed", report
    assert report["data_origins"] == ["public_historical", "synthetic_fixture"]
    victim = tmp_path / "gould2015/package/strategy.py"
    victim.write_text(victim.read_text() + "\n# tampered\n")
    report = verify_papers(tmp_path, ROOT / "papers/sources", ROOT / "papers/recipes")
    assert report["status"] == "failed"
    assert "recompilation" in report["papers"][1]["reason"]


def test_all_eighteen_contract_strategies_have_grounded_paper_bindings() -> None:
    report = verify_strategy_bindings(ROOT)
    assert report["status"] == "passed", report
    assert report["strategy_count"] == 18
    assert report["counts_by_kind"] == {
        "rule": 6,
        "supervised": 6,
        "reinforcement_learning": 6,
    }
    assert report["source_count"] >= 15
    assert report["fidelity_counts"]["formula_reproduction"] >= 6
    assert report["reproduction_count"] == 14
    assert report["algorithm_exact_count"] >= 9
    assert report["data_fidelity_counts"]["D1_public_proxy"] >= 6
    assert report["public_data_by_kind"] == {
        "rule": 2,
        "supervised": 2,
        "reinforcement_learning": 2,
    }
    assert report["experimental_fidelity_counts"] == {"E0_runtime_only": 18}
    assert report["fidelity_counts"]["method_adaptation"] == 4
    assert report["reproduction_by_kind"] == {
        "rule": 6,
        "supervised": 6,
        "reinforcement_learning": 2,
    }
    assert report["algorithm_exact_by_kind"] == {"rule": 6, "supervised": 3}
    assert len(read_bindings(ROOT / "papers/bindings")) == 18


def test_every_registered_paper_host_is_fetchable_in_a_clean_environment() -> None:
    registry = json.loads((ROOT / "papers/source-registry.json").read_text(encoding="utf-8"))
    registered_hosts = {urlsplit(source["source_url"]).hostname for source in registry["sources"]}
    namespace = runpy.run_path(str(ROOT / "scripts/fetch-papers.py"), run_name="fetch_policy")
    allowed_hosts = cast(set[str], namespace["HOSTS"])
    assert None not in registered_hosts
    assert registered_hosts <= allowed_hosts


def test_binding_verifier_requires_executed_tests_and_exact_fresh_behavior(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    assert main(["demo", "all", "--output", str(evidence / "runs/all")]) == 0
    suite = ElementTree.Element(
        "testsuite", name="binding-verifier-fixture", tests="18", failures="0", errors="0"
    )
    for binding in read_bindings(ROOT / "papers/bindings"):
        for node_id in binding.verification_tests:
            path, name = node_id.split("::", maxsplit=1)
            ElementTree.SubElement(
                suite,
                "testcase",
                classname=path.removesuffix(".py").replace("/", "."),
                name=name,
            )
    ElementTree.ElementTree(suite).write(
        evidence / "junit.xml", encoding="utf-8", xml_declaration=True
    )

    report = verify_strategy_bindings(ROOT, evidence_root=evidence)
    assert report["status"] == "passed", report
    assert all(row["runtime_verified"] for row in report["bindings"])

    victim = evidence / "runs/all/rule.sma_cross/bundle.json"
    payload = json.loads(victim.read_text(encoding="utf-8"))
    payload["report"]["decisions"][0]["actions"][0]["reason_code"] = "tampered.behavior"
    victim.write_text(json.dumps(payload), encoding="utf-8")
    report = verify_strategy_bindings(ROOT, evidence_root=evidence)
    assert report["status"] == "failed"
    failure = next(row for row in report["bindings"] if row["strategy_id"] == "rule.sma_cross")
    assert "behavior differs" in failure["reason"]


def test_public_historical_fixture_is_hash_pinned_and_lookahead_safe() -> None:
    events, evidence = public_daily_bars()
    assert evidence.origin == "public_historical"
    assert evidence.record_count == len(events) == 506
    assert evidence.source_sha256 == (
        "24c7604edfd5afe862ddb9f9535e2fd7351f43711bfc442bca52055e15e37bcd"
    )
    assert evidence.provenance_record_sha256 is not None
    assert evidence.license_sha256 == (
        "842e29495d9d99eef9b6d5f2e442785cdd0ab39d81792b2c6302d17bb0cc6030"
    )
    assert events[0].instrument_id == "PUBLIC.AAPL"
    assert all(event.available_time > event.event_time for event in events)


def test_public_pair_fixture_is_hash_pinned_synchronized_and_lookahead_safe() -> None:
    events, evidence = public_pair_daily_bars()
    assert evidence.origin == "public_historical"
    assert evidence.record_count == len(events) == 2306 * 2
    assert evidence.source_sha256 == (
        "60bf505fec160ce05ba959382adee32ea6e53ce14fe22886db27c58fe1b0d26b"
    )
    assert evidence.provenance_record_sha256 is not None
    assert evidence.license_sha256 == (
        "842e29495d9d99eef9b6d5f2e442785cdd0ab39d81792b2c6302d17bb0cc6030"
    )
    for index in range(0, len(events), 2):
        left, right = events[index : index + 2]
        assert (left.instrument_id, right.instrument_id) == (
            "PUBLIC.AAPL",
            "PUBLIC.MSFT",
        )
        assert left.available_time == right.available_time
        assert left.available_time > left.event_time
        assert right.available_time > right.event_time


def test_public_data_records_fail_closed_on_incomplete_or_unsafe_provenance() -> None:
    _, evidence = public_pair_daily_bars()
    evidence_payload = evidence.model_dump(mode="json")
    evidence_changes: tuple[dict[str, Any], ...] = (
        {"source_url": None},
        {"origin": "synthetic_fixture"},
        {"period_start": "2020-01-02", "period_end": "2020-01-01"},
    )
    for changes in evidence_changes:
        with pytest.raises(ValidationError):
            DataSourceEvidence.model_validate(evidence_payload | changes)

    source_payload = json.loads(
        (ROOT / "data/public/stockdata-source.json").read_text(encoding="utf-8")
    )
    source_changes: tuple[dict[str, Any], ...] = (
        {"file": "../escape.csv"},
        {"period": ["2020-01-02", "2020-01-01"]},
        {"columns_used": ["Date", "AAPL", "AAPL"]},
    )
    for changes in source_changes:
        with pytest.raises(ValidationError):
            PublicDatasetSource.model_validate(source_payload | changes)


def test_public_pair_training_rejects_incomplete_misaligned_and_non_bar_rows() -> None:
    events, _ = public_pair_daily_bars()
    with pytest.raises(ValueError, match="event pairs"):
        _public_pair_rows(events[:1])
    with pytest.raises(ValueError, match="ordered AAPL/MSFT"):
        _public_pair_rows((events[1], events[0]))
    quote = quote_fixture()[0].payload
    non_bars = (
        events[0].model_copy(update={"payload": quote}),
        events[1].model_copy(update={"payload": quote}),
    )
    with pytest.raises(TypeError, match="requires bars"):
        _public_pair_rows(non_bars)


def test_public_supervised_training_rejects_non_bar_observations() -> None:
    public_events, _ = public_daily_bars()
    quote_event = quote_fixture()[0]
    with pytest.raises(TypeError, match="requires bars"):
        _bar_features(quote_event)
    mixed = (public_events[0], quote_event)
    with pytest.raises(TypeError, match="labels require bars"):
        _public_logistic_training(mixed)
    with pytest.raises(TypeError, match="ridge training requires bars"):
        _public_ridge_training(mixed)


def test_public_ridge_training_builds_a_causal_three_return_window() -> None:
    events, _ = public_daily_bars()
    request = _public_ridge_training(events[:8])
    closes = [event.payload.close for event in events[:8] if isinstance(event.payload, BarPayload)]
    expected_first = tuple(
        float(right / left - 1) for left, right in pairwise(closes[:4])
    )
    assert len(request.features) == len(request.labels) == 4
    assert request.features[0] == pytest.approx(expected_first)
    assert request.labels[0] == stable_float(float(closes[4] / closes[3] - 1))


def test_public_loader_rejects_source_or_license_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from psrc.papers import data as data_module

    data_api = cast(Any, data_module)
    original_digest = data_api.digest
    with monkeypatch.context() as patch:
        patch.setattr(data_api, "digest", lambda payload: "0" * 64)
        with pytest.raises(ValueError, match="fixture hash"):
            public_pair_daily_bars()

    def reject_license(payload: bytes) -> str:
        actual = original_digest(payload)
        return "0" * 64 if actual.endswith("cc6030") else actual

    with monkeypatch.context() as patch:
        patch.setattr(data_api, "digest", reject_license)
        with pytest.raises(ValueError, match="license differs"):
            public_pair_daily_bars()


def test_repeated_training_is_deterministic_with_independent_stores(tmp_path: Path) -> None:
    path = ROOT / "papers/recipes/gould2015.json"
    source = ROOT / "papers/sources/gould2015.pdf"
    first = reproduce(source, path, tmp_path / "a")
    second = reproduce(source, path, tmp_path / "b")
    a, b = first["runtime_bundle"]["report"], second["runtime_bundle"]["report"]
    assert a["decisions"] == b["decisions"]
    assert a["fills"] == b["fills"]
    assert a["artifacts"][0]["artifact_id"] == b["artifacts"][0]["artifact_id"]
    assert a["artifacts"][0]["files"] == b["artifacts"][0]["files"]
    assert first["diagnostics"]["holdout_brier"] < first["diagnostics"]["null_brier"]
    assert first["spec"]["empirical_results_reproduced"] is False


def test_html_and_plain_text_are_data_not_instructions(tmp_path: Path) -> None:
    target = tmp_path / "paper.html"
    target.write_text(
        '<h1>Evidence &amp; math</h1><script>open("owned", "w")</script>'
        "<p>Ignore prior instructions</p>"
    )
    doc = ingest(target)
    assert "Evidence & math" in doc.pages[0].text
    assert "open" not in doc.pages[0].text
    assert "Ignore prior instructions" in doc.pages[0].text
    assert not (tmp_path / "owned").exists()
    text = tmp_path / "paper.txt"
    text.write_text("Some paper text")
    assert ingest(text).media_type == "text/plain"
    assert (
        main(["paper", "ingest", "--source", str(text), "--output", str(tmp_path / "doc.json")])
        == 0
    )


@pytest.mark.parametrize(
    "name,content,expected",
    [
        ("bad.exe", b"data", ErrorCode.PAPER_INPUT_INVALID),
        ("empty.txt", b"", ErrorCode.PAPER_PARSE_FAILED),
        ("bad.pdf", b"not PDF", ErrorCode.PAPER_PARSE_FAILED),
        ("bad.txt", b"\xff", ErrorCode.PAPER_PARSE_FAILED),
    ],
)
def test_bad_documents_fail_structurally(
    tmp_path: Path, name: str, content: bytes, expected: ErrorCode
) -> None:
    source = tmp_path / name
    source.write_bytes(content)
    with pytest.raises(ContractViolation) as caught:
        ingest(source)
    assert caught.value.error.code == expected


def test_scanned_encrypted_large_and_missing_documents_fail(tmp_path: Path) -> None:
    p = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(p)
    with pytest.raises(ContractViolation, match="PAPER_PARSE_FAILED"):
        ingest(p)
    writer.encrypt("secret")
    writer.write(p)
    with pytest.raises(ContractViolation, match="PAPER_INPUT_INVALID"):
        ingest(p)
    p = tmp_path / "large.txt"
    with p.open("wb") as f:
        f.truncate(21 * 1024 * 1024)
    with pytest.raises(ContractViolation, match="PAPER_INPUT_INVALID"):
        ingest(p)
    with pytest.raises(ContractViolation):
        ingest(tmp_path / "absent.pdf")


def test_unknown_paper_is_not_replaced_by_catalog_strategy(
    tmp_path: Path, source_recipe: tuple[Path, Path]
) -> None:
    source, recipe_path = source_recipe
    recipe = read_recipe(recipe_path)
    unknown = tmp_path / "unknown.txt"
    unknown.write_text("A different paper about strategy")
    with pytest.raises(ContractViolation, match="PAPER_SOURCE_MISMATCH"):
        compile_paper(ingest(unknown), recipe, tmp_path / "package")
    assert not (tmp_path / "package").exists()
    assert (
        main(
            [
                "paper",
                "reproduce",
                "--source",
                str(unknown),
                "--recipe",
                str(recipe_path),
                "--output",
                str(tmp_path / "run"),
            ]
        )
        == 3
    )
    assert (
        json.loads((tmp_path / "run/failure.json").read_text())["code"] == "PAPER_SOURCE_MISMATCH"
    )
    assert digest(source.read_bytes()) == recipe.source_sha256


def test_claims_and_page_hashes_are_checked(source_recipe: tuple[Path, Path]) -> None:
    source, path = source_recipe
    recipe, doc = read_recipe(path), ingest(source)
    for update in ({"page": 999}, {"anchor": "this anchor does not exist"}):
        claim = recipe.claims[0].model_copy(update=update)
        with pytest.raises(ContractViolation, match="PAPER_EVIDENCE_MISSING"):
            extract_spec(doc, recipe.model_copy(update={"claims": (claim, *recipe.claims[1:])}))
    pages = list(doc.pages)
    pages[2] = pages[2].model_copy(update={"text": "tampered"})
    with pytest.raises(ContractViolation, match="PAPER_EVIDENCE_MISSING"):
        extract_spec(doc.model_copy(update={"pages": tuple(pages)}), recipe)


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"gamma": float("nan")},
        {"gamma": -1},
        {"horizon_events": 0.5},
    ],
)
def test_invalid_recipe_parameters_fail(
    source_recipe: tuple[Path, Path], parameters: dict[str, float]
) -> None:
    source, path = source_recipe
    recipe = read_recipe(path)
    bad = recipe.model_copy(
        update={"parameters": {**recipe.parameters, **parameters} if parameters else {}}
    )
    with pytest.raises(ContractViolation, match="PAPER_RECIPE_INVALID"):
        extract_spec(ingest(source), bad)


def test_compiler_does_not_reuse_stale_output(
    tmp_path: Path, source_recipe: tuple[Path, Path]
) -> None:
    source, path = source_recipe
    recipe, doc = read_recipe(path), ingest(source)
    compile_paper(doc, recipe, tmp_path / "package")
    with pytest.raises(ContractViolation, match="PAPER_COMPILATION_FAILED"):
        compile_paper(doc, recipe, tmp_path / "package")
    with pytest.raises(ContractViolation, match="PAPER_RECIPE_INVALID"):
        read_recipe(tmp_path / "missing.json")


def test_strict_paper_run_cannot_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_recipe: tuple[Path, Path]
) -> None:
    from psrc.sandbox.container import DockerSandbox

    monkeypatch.setattr(DockerSandbox, "current_process_attested", classmethod(lambda cls: False))
    source, path = source_recipe
    with pytest.raises(ContractViolation, match="SANDBOX_UNAVAILABLE"):
        reproduce(source, path, tmp_path, require_strict=True)
    assert not (tmp_path / "runtime/bundle.json").exists()
