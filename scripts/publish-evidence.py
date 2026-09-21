#!/usr/bin/env python3
"""Publish compact, portable verification receipts from completed local gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from verify import verification_input_sha256

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def absolute_paths(value: object, location: str = "$") -> list[str]:
    if isinstance(value, dict):
        return [
            issue
            for key, item in value.items()
            for issue in absolute_paths(item, f"{location}.{key}")
        ]
    if isinstance(value, list):
        return [
            issue
            for index, item in enumerate(value)
            for issue in absolute_paths(item, f"{location}[{index}]")
        ]
    if isinstance(value, str) and (
        value.startswith("/") or WINDOWS_ABSOLUTE.match(value) is not None
    ):
        return [f"{location}: {value}"]
    return []


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accepted_summary(receipt: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("acceptance report has no checks object")
    tests = checks["automated_tests_no_failures_or_skips"]["detail"]
    coverage = checks["code_coverage"]["detail"]
    return {
        "scope": receipt["scope"],
        "python": receipt["python"],
        "status": receipt["status"],
        "tests": tests["tests"],
        "failures": tests["failures"],
        "errors": tests["errors"],
        "skipped": tests["skipped"],
        "coverage_percent": coverage["percent_covered"],
        "acceptance_checks_passed": sum(
            1 for check in checks.values() if check.get("passed") is True
        ),
        "acceptance_checks_total": len(checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, default=ROOT / "reports/generated")
    parser.add_argument("--strict", type=Path, default=ROOT / "reports/strict")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/release")
    parser.add_argument(
        "--summary", type=Path, default=ROOT / "evidence/verification-summary.json"
    )
    args = parser.parse_args()

    current_hash = verification_input_sha256(ROOT)
    published: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for name, directory in (("host", args.host), ("strict", args.strict)):
        receipt = read_json(directory / "verification.json")
        report = read_json(directory / "acceptance-report.json")
        if receipt.get("status") != "passed" or report.get("status") != "passed":
            raise ValueError(f"{name} verification has not passed")
        if receipt.get("verification_input_sha256") != current_hash:
            raise ValueError(f"{name} receipt does not match the current verification input tree")
        issues = absolute_paths(report)
        if issues:
            raise ValueError(f"{name} acceptance report contains absolute paths: {issues}")
        published[name] = (receipt, report)

    args.output.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for name, (receipt, report) in published.items():
        for suffix, value in (("verification", receipt), ("acceptance", report)):
            path = args.output / f"{name}-{suffix}.json"
            write_json(path, value)
            files.append(path)

    host_receipt, host_report = published["host"]
    strict_receipt, strict_report = published["strict"]
    summary = {
        "software_version": host_receipt["software_version"],
        "contract_version": host_report["contract_version"],
        "verification_input_sha256": current_hash,
        "host": {
            **accepted_summary(host_receipt, host_report),
            "full_receipt": "evidence/release/host-verification.json",
            "acceptance_report": "evidence/release/host-acceptance.json",
        },
        "strict_container": {
            **accepted_summary(strict_receipt, strict_report),
            "full_receipt": "evidence/release/strict-verification.json",
            "acceptance_report": "evidence/release/strict-acceptance.json",
        },
        "remote_ci": {"status": "pending_repository_creation", "claims": []},
        "published_files": {
            path.relative_to(ROOT).as_posix(): sha256(path) for path in sorted(files)
        },
    }
    write_json(args.summary, summary)
    print(f"published portable evidence for input tree {current_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
