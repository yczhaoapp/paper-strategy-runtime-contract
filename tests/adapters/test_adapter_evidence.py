from __future__ import annotations

from pathlib import Path

from psrc.evidence.adapters import generate_adapter_evidence


def test_adapter_evidence_is_materialized(tmp_path: Path) -> None:
    comparison = generate_adapter_evidence(tmp_path)
    assert comparison["native_engine_count"] == 2
    assert comparison["decisions_equal"] is True
    assert comparison["fill_shapes_equal"] is True
    assert (tmp_path / "comparison.json").is_file()
    for engine_id in ("reference", "backtrader"):
        assert (tmp_path / engine_id / "report.json").is_file()
        assert (tmp_path / engine_id / "report.html").is_file()
