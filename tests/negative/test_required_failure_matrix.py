from __future__ import annotations

import json
from pathlib import Path

from psrc.evidence.failures import generate_failure_evidence


def test_all_eight_required_failures_emit_auditable_reports(tmp_path: Path) -> None:
    observed = generate_failure_evidence(tmp_path)
    assert set(observed.values()) == {
        "DATA_FIELD_MISSING",
        "DATA_TIMEFRAME_MISMATCH",
        "SYMBOL_MAPPING_FAILED",
        "ARTIFACT_NOT_FOUND",
        "ACTION_INVALID",
        "ORDER_REJECTED",
        "TRAINING_FAILED",
        "BACKTEST_FAILED",
    }
    assert len(observed) == 8
    for name, code in observed.items():
        payload = json.loads((tmp_path / name / "report.json").read_text(encoding="utf-8"))
        assert payload["status"] == "failed"
        assert payload["error"]["code"] == code
        assert (tmp_path / name / "report.html").is_file()
