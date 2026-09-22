#!/usr/bin/env python3
"""Portable Docker launcher: build online, then verify with no network and read-only root."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=Path("reports/strict"))
parser.add_argument(
    "--allow-cache",
    action="store_true",
    help="permit Docker layer cache (release verification defaults to a clean rebuild)",
)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
uid = getattr(os, "getuid", lambda: 65532)()
gid = getattr(os, "getgid", lambda: 65532)()
user = f"{uid}:{gid}" if uid != 0 else "65532:65532"
docker = shutil.which("docker")
if docker is None:
    desktop_cli = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    docker = str(desktop_cli) if desktop_cli.is_file() else None
if docker is None:
    raise SystemExit("Docker CLI is unavailable; install or start Docker Desktop")
build = [docker, "build", "--pull", "--tag", "psrc-verifier:local"]
if not args.allow_cache:
    build.append("--no-cache")
build.append(str(root))
subprocess.run(build, check=True)
image_id = subprocess.run(
    [docker, "image", "inspect", "--format", "{{.Id}}", "psrc-verifier:local"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
(output / "image.json").write_text(
    json.dumps(
        {
            "image": "psrc-verifier:local",
            "image_id": image_id,
            "build_no_cache": not args.allow_cache,
            "base_pull_requested": True,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
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
        f"type=bind,src={output},dst=/psrc/reports",
        "--entrypoint",
        "bash",
        "psrc-verifier:local",
        "/app/scripts/verify-container.sh",
        "/psrc/reports",
    ],
    check=True,
)
