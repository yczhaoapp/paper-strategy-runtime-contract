from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import ResourcePolicy


def docker_executable() -> str | None:
    """Locate Docker CLI, including Docker Desktop's macOS app bundle."""
    executable = shutil.which("docker")
    if executable is not None:
        return executable
    desktop_cli = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    return str(desktop_cli) if desktop_cli.is_file() else None


def _network_namespace_isolated(
    interfaces_root: Path = Path("/sys/class/net"),
    ipv4_routes_path: Path = Path("/proc/net/route"),
    ipv6_routes_path: Path = Path("/proc/net/ipv6_route"),
) -> bool:
    """Prove that the namespace has no usable non-loopback network path.

    Some container kernels expose down tunnel devices even for ``--network none``.
    Device names alone therefore do not establish connectivity; interface flags and
    route tables do.
    """
    try:
        interfaces = [path for path in interfaces_root.iterdir() if path.is_dir()]
        if not any(path.name == "lo" for path in interfaces):
            return False
        for interface in interfaces:
            if (
                interface.name != "lo"
                and int((interface / "flags").read_text(encoding="utf-8").strip(), 0) & 1
            ):
                return False

        ipv4_rows = [
            line.split()
            for line in ipv4_routes_path.read_text(encoding="utf-8").splitlines()[1:]
            if line.strip()
        ]
        if any(row[0] != "lo" for row in ipv4_rows):
            return False

        ipv6_rows = [
            line.split()
            for line in ipv6_routes_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return not any(row[-1] != "lo" for row in ipv6_rows)
    except (OSError, IndexError, ValueError):
        return False


@dataclass(frozen=True)
class ContainerMounts:
    data: Path
    artifacts: Path
    reports: Path


@dataclass(frozen=True)
class SandboxExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    attested: bool = True


class DockerSandbox:
    """Builds a fail-closed Docker invocation for untrusted strategy execution."""

    image = "psrc-verifier:local"

    @staticmethod
    def available() -> bool:
        return docker_executable() is not None

    @classmethod
    def require_available(cls, *, run_id: str, strategy_id: str) -> None:
        if not cls.available():
            cls._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.SANDBOX_UNAVAILABLE,
                message="Strict-container execution was requested but Docker is unavailable",
                details={"required_backend": "docker", "fallback_used": False},
            )

    @classmethod
    def command(
        cls,
        *,
        policy: ResourcePolicy,
        mounts: ContainerMounts,
        command: tuple[str, ...],
        container_name: str | None = None,
    ) -> tuple[str, ...]:
        uid = getattr(os, "getuid", lambda: 65532)()
        gid = getattr(os, "getgid", lambda: 65532)()
        container_user = f"{uid}:{gid}" if uid != 0 else "65532:65532"
        identity = ("--name", container_name) if container_name is not None else ()
        return (
            docker_executable() or "docker",
            "run",
            "--rm",
            *identity,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(policy.process_limit),
            "--memory",
            f"{policy.memory_mb}m",
            "--cpus",
            "1.0",
            "--user",
            container_user,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--mount",
            f"type=bind,src={mounts.data.resolve()},dst=/psrc/data,readonly",
            "--mount",
            f"type=bind,src={mounts.artifacts.resolve()},dst=/psrc/artifacts",
            "--mount",
            f"type=bind,src={mounts.reports.resolve()},dst=/psrc/reports",
            "--env",
            "PSRC_STRICT_SANDBOX=1",
            "--env",
            "PSRC_SANDBOX_ATTESTATION=strict-container-v1",
            cls.image,
            *command,
        )

    @staticmethod
    def _container_marker_present() -> bool:
        return Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file()

    @staticmethod
    def _runtime_controls_present() -> bool:
        try:
            status = {
                key: value.strip()
                for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
                if ":" in line
                for key, value in (line.split(":", maxsplit=1),)
            }
            mounts = [line.split() for line in Path("/proc/mounts").read_text().splitlines()]
            root_options = next(parts[3].split(",") for parts in mounts if parts[1] == "/")
            tmp_options = next(parts[3].split(",") for parts in mounts if parts[1] == "/tmp")
            effective_capabilities = int(status.get("CapEff", "1"), 16)
        except (OSError, StopIteration, IndexError, ValueError):
            return False
        return (
            _network_namespace_isolated()
            and effective_capabilities == 0
            and status.get("NoNewPrivs") == "1"
            and "ro" in root_options
            and {"noexec", "nosuid", "nodev"} <= set(tmp_options)
        )

    @classmethod
    def current_process_attested(cls) -> bool:
        return (
            os.environ.get("PSRC_STRICT_SANDBOX") == "1"
            and os.environ.get("PSRC_SANDBOX_ATTESTATION") == "strict-container-v1"
            and getattr(os, "geteuid", lambda: 0)() != 0
            and cls._container_marker_present()
            and cls._runtime_controls_present()
        )

    @classmethod
    def execute(
        cls,
        *,
        run_id: str,
        strategy_id: str,
        policy: ResourcePolicy,
        mounts: ContainerMounts,
        command: tuple[str, ...],
    ) -> SandboxExecutionResult:
        cls.require_available(run_id=run_id, strategy_id=strategy_id)
        container_name = f"psrc-{uuid.uuid4().hex}"
        invocation = cls.command(
            policy=policy,
            mounts=mounts,
            command=command,
            container_name=container_name,
        )
        try:
            completed = subprocess.run(
                invocation,
                check=False,
                capture_output=True,
                text=True,
                timeout=policy.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            cleanup_succeeded = cls._force_remove(container_name)
            cls._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=ErrorCode.SANDBOX_TIMEOUT,
                message="Strict-container strategy exceeded its declared timeout",
                details={
                    "timeout_seconds": policy.timeout_seconds,
                    "container_cleanup_attempted": True,
                    "container_cleanup_succeeded": cleanup_succeeded,
                    "fallback_used": False,
                },
            )
        if completed.returncode != 0:
            exhausted = completed.returncode in {137, -9}
            cls._fail(
                run_id=run_id,
                strategy_id=strategy_id,
                code=(
                    ErrorCode.SANDBOX_RESOURCE_EXHAUSTED
                    if exhausted
                    else ErrorCode.SANDBOX_EXECUTION_FAILED
                ),
                message="Strict-container strategy process failed",
                details={
                    "returncode": completed.returncode,
                    "stderr_tail": completed.stderr[-2000:],
                    "fallback_used": False,
                },
            )
        return SandboxExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    @staticmethod
    def _force_remove(container_name: str) -> bool:
        """Best-effort cleanup after the Docker client itself times out."""

        try:
            completed = subprocess.run(
                (docker_executable() or "docker", "rm", "--force", container_name),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    @staticmethod
    def _fail(
        *,
        run_id: str,
        strategy_id: str,
        code: ErrorCode,
        message: str,
        details: dict[str, object],
    ) -> NoReturn:
        raise ContractViolation(
            ContractError(
                run_id=run_id,
                stage=ErrorStage.SANDBOX,
                code=code,
                message=message,
                strategy_id=strategy_id,
                details=details,
            )
        )
