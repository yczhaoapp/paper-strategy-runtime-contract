from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from psrc.contract.errors import ContractError, ErrorCode, ErrorStage
from psrc.runtime.report import FailureReport, UnifiedRunReport, write_failure_bundle


def test_failure_report_is_machine_readable_and_has_human_html(tmp_path: Path) -> None:
    error = ContractError(
        run_id="test.failure-report",
        stage=ErrorStage.BACKTEST,
        code=ErrorCode.BACKTEST_FAILED,
        message="synthetic failure",
    )
    report = FailureReport(
        run_id=error.run_id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        error=error,
    )
    UnifiedRunReport.model_validate(report.model_dump(mode="json"))
    write_failure_bundle(report, tmp_path)
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "BACKTEST_FAILED"
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "FAILED" in html
    assert "BACKTEST_FAILED" in html
