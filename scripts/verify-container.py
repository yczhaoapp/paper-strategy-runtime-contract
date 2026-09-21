#!/usr/bin/env python3
"""Portable Docker launcher: build online, then verify with no network and read-only root."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=Path("reports/strict"))
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
subprocess.run([docker, "build", "--tag", "psrc-verifier:local", str(root)], check=True)
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
