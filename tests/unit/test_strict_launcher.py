from __future__ import annotations

import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest


def _launcher() -> Callable[[list[str]], int]:
    script = Path(__file__).resolve().parents[2] / "scripts/verify-container.py"
    return cast(Callable[[list[str]], int], runpy.run_path(str(script))["main"])


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _old_success(output: Path) -> None:
    output.mkdir()
    _write(output / "verification.json", {"status": "passed", "attempt_id": "old"})
    _write(output / "acceptance-report.json", {"status": "passed", "attempt_id": "old"})
    _write(output / "image.json", {"image_id": "sha256:old", "attempt_id": "old"})


@pytest.mark.parametrize("failure_stage", ["build", "image", "run", "missing_receipt"])
def test_strict_launcher_invalidates_old_success_on_each_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    output = tmp_path / "strict"
    _old_success(output)
    monkeypatch.setattr("shutil.which", lambda _: "docker")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        command = argv[1]
        if command == failure_stage:
            raise subprocess.CalledProcessError(42, argv)
        if command == "image":
            return subprocess.CompletedProcess(argv, 0, "sha256:new\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _launcher()(["--output", str(output)]) == 1
    verification = json.loads((output / "verification.json").read_text())
    acceptance = json.loads((output / "acceptance-report.json").read_text())
    assert verification["status"] == acceptance["status"] == "failed"
    assert verification["attempt_id"] == acceptance["attempt_id"]
    assert verification["attempt_id"] != "old"
    assert verification["stage"] == acceptance["stage"]
    assert not (output / "image.json").exists()


def test_strict_launcher_publishes_only_one_complete_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "strict"
    _old_success(output)
    monkeypatch.setattr("shutil.which", lambda _: "docker")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "image":
            return subprocess.CompletedProcess(argv, 0, "sha256:new\n", "")
        if argv[1] == "run":
            mount = argv[argv.index("--mount") + 1]
            staged = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            _write(
                staged / "verification.json",
                {"status": "passed", "strict_requested": True, "scope": "strict_container"},
            )
            _write(
                staged / "acceptance-report.json",
                {"status": "passed", "verification_scope": "strict_container_verification"},
            )
            (staged / "junit.xml").write_text("current-attempt", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _launcher()(["--output", str(output)]) == 0
    verification = json.loads((output / "verification.json").read_text())
    acceptance = json.loads((output / "acceptance-report.json").read_text())
    image = json.loads((output / "image.json").read_text())
    attempt_id = verification["attempt_id"]
    assert len(attempt_id) == 32
    assert acceptance["attempt_id"] == image["attempt_id"] == attempt_id
    assert verification["image_id"] == acceptance["image_id"] == image["image_id"]
    assert image["image_id"] == "sha256:new"
    assert (output / "junit.xml").read_text() == "current-attempt"


def test_release_publisher_rejects_mixed_strict_attempts_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    host = tmp_path / "host"
    strict = tmp_path / "strict"
    host.mkdir()
    strict.mkdir()
    _write(host / "verification.json", {"status": "passed", "verification_input_sha256": "tree"})
    _write(host / "acceptance-report.json", {"status": "passed"})
    _write(
        strict / "verification.json",
        {
            "status": "passed",
            "verification_input_sha256": "tree",
            "scope": "strict_container",
            "attempt_id": "a" * 32,
            "image_id": "sha256:new",
        },
    )
    _write(
        strict / "acceptance-report.json",
        {
            "status": "passed",
            "verification_scope": "strict_container_verification",
            "attempt_id": "b" * 32,
            "image_id": "sha256:new",
        },
    )
    _write(
        strict / "image.json",
        {
            "attempt_id": "a" * 32,
            "image_id": "sha256:new",
            "build_no_cache": True,
            "base_pull_requested": True,
        },
    )
    main = cast(Callable[[], int], runpy.run_path(str(scripts / "publish-evidence.py"))["main"])
    main.__globals__["verification_input_sha256"] = lambda _: "tree"
    published = tmp_path / "published"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish-evidence.py",
            "--host",
            str(host),
            "--strict",
            str(strict),
            "--output",
            str(published),
            "--summary",
            str(tmp_path / "summary.json"),
        ],
    )
    with pytest.raises(ValueError, match="do not share one attempt"):
        main()
    assert not published.exists()
