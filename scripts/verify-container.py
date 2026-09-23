#!/usr/bin/env python3
"""Build online, then publish only a complete offline strict-verification attempt."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4


def _write_json(path: Path, value: dict[str, Any], attempt_id: str) -> None:
    temporary = path.with_name(f".{path.name}.{attempt_id}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _receipt(attempt_id: str, status: str, stage: str, **extra: Any) -> dict[str, Any]:
    return {"attempt_id": attempt_id, "status": status, "stage": stage, **extra}


def _read_passed(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("status") != "passed":
        raise RuntimeError(f"strict verification did not produce a passing {path.name}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("reports/strict"))
    parser.add_argument(
        "--allow-cache",
        action="store_true",
        help="permit Docker layer cache (release verification defaults to a clean rebuild)",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid4().hex
    staged = output / "_attempts" / attempt_id

    # Invalidate old success before build, inspect, or docker run can fail.
    for name in ("verification.json", "acceptance-report.json", "image.json"):
        (output / name).unlink(missing_ok=True)
    _write_json(
        output / "verification.json",
        _receipt(attempt_id, "running", "preparation"),
        attempt_id,
    )
    _write_json(
        output / "acceptance-report.json",
        _receipt(attempt_id, "running", "preparation", evidence_complete=False),
        attempt_id,
    )

    stage = "preparation"
    try:
        staged.mkdir(parents=True)
        stage = "docker_lookup"
        uid = getattr(os, "getuid", lambda: 65532)()
        gid = getattr(os, "getgid", lambda: 65532)()
        user = f"{uid}:{gid}" if uid != 0 else "65532:65532"
        docker = shutil.which("docker")
        if docker is None:
            desktop_cli = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
            docker = str(desktop_cli) if desktop_cli.is_file() else None
        if docker is None:
            raise RuntimeError("Docker CLI is unavailable; install or start Docker Desktop")

        stage = "docker_build"
        build = [docker, "build", "--pull", "--tag", "psrc-verifier:local"]
        if not args.allow_cache:
            build.append("--no-cache")
        build.append(str(root))
        subprocess.run(build, check=True, timeout=1800)

        stage = "docker_inspect"
        image_id = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", "psrc-verifier:local"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not image_id.startswith("sha256:"):
            raise RuntimeError("Docker inspect did not return an image content ID")

        stage = "docker_run"
        subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "128",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--user",
                user,
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=512m",
                "--mount",
                f"type=bind,src={staged},dst=/psrc/reports",
                "--entrypoint",
                "bash",
                "psrc-verifier:local",
                "/app/scripts/verify-container.sh",
                "/psrc/reports",
            ],
            check=True,
            timeout=1200,
        )

        stage = "publication"
        verification = _read_passed(staged / "verification.json")
        acceptance = _read_passed(staged / "acceptance-report.json")
        if verification.get("strict_requested") is not True:
            raise RuntimeError("container receipt does not attest a strict request")
        for item in staged.iterdir():
            if item.name in {"verification.json", "acceptance-report.json"}:
                continue
            target = output / item.name
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            item.replace(target)

        image = {
            "attempt_id": attempt_id,
            "image": "psrc-verifier:local",
            "image_id": image_id,
            "build_no_cache": not args.allow_cache,
            "base_pull_requested": True,
        }
        verification.update(attempt_id=attempt_id, image_id=image_id)
        acceptance.update(attempt_id=attempt_id, image_id=image_id)
        _write_json(output / "image.json", image, attempt_id)
        _write_json(output / "verification.json", verification, attempt_id)
        _write_json(output / "acceptance-report.json", acceptance, attempt_id)
        shutil.rmtree(staged)
        return 0
    except Exception as exc:
        failure = _receipt(
            attempt_id,
            "failed",
            stage,
            error=f"{type(exc).__name__}: {exc}",
            evidence_complete=False,
            attempt_output=f"_attempts/{attempt_id}",
        )
        (output / "image.json").unlink(missing_ok=True)
        _write_json(output / "verification.json", failure, attempt_id)
        _write_json(output / "acceptance-report.json", failure, attempt_id)
        print(f"Strict verification failed during {stage}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
