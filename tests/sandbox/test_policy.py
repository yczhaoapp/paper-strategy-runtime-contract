from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from psrc.cli import main
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import ResourcePolicy
from psrc.sandbox.container import (
    ContainerMounts,
    DockerSandbox,
    SandboxExecutionResult,
    _network_namespace_isolated,
)
from psrc.sandbox.static import StaticPolicyScanner


def _network_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    interfaces = tmp_path / "net"
    interfaces.mkdir()
    for name, flags in (("lo", "0x9"), ("gre0", "0x80"), ("erspan0", "0x1002")):
        device = interfaces / name
        device.mkdir()
        (device / "flags").write_text(flags, encoding="utf-8")
    ipv4 = tmp_path / "route"
    ipv4.write_text("Iface\tDestination\tGateway\tFlags\n", encoding="utf-8")
    ipv6 = tmp_path / "ipv6_route"
    ipv6.write_text("", encoding="utf-8")
    return interfaces, ipv4, ipv6


def test_network_none_accepts_down_kernel_tunnel_devices(tmp_path: Path) -> None:
    interfaces, ipv4, ipv6 = _network_fixture(tmp_path)
    assert _network_namespace_isolated(interfaces, ipv4, ipv6) is True


def test_network_attestation_rejects_enabled_interface_or_route(tmp_path: Path) -> None:
    interfaces, ipv4, ipv6 = _network_fixture(tmp_path)
    (interfaces / "gre0" / "flags").write_text("0x81", encoding="utf-8")
    assert _network_namespace_isolated(interfaces, ipv4, ipv6) is False
    (interfaces / "gre0" / "flags").write_text("0x80", encoding="utf-8")
    ipv4.write_text(
        "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t00000000\t0003\n",
        encoding="utf-8",
    )
    assert _network_namespace_isolated(interfaces, ipv4, ipv6) is False


def test_static_scanner_rejects_import_file_network_and_reflection() -> None:
    source = """
import socket
from pathlib import Path
open('/etc/passwd').read()
object.__subclasses__()
"""
    findings = StaticPolicyScanner.scan(source, frozenset({"math", "numpy"}))
    codes = {finding.code for finding in findings}
    assert codes == {"IMPORT_DENIED", "DANGEROUS_CALL_DENIED", "DUNDER_REFLECTION_DENIED"}


def test_static_scanner_accepts_manifest_allow_list() -> None:
    findings = StaticPolicyScanner.scan(
        "import math\nfrom numpy import array\nvalue = math.sqrt(4)\n",
        frozenset({"math", "numpy"}),
    )
    assert findings == ()


def test_docker_command_is_fail_closed(tmp_path: Path) -> None:
    mounts = ContainerMounts(
        data=tmp_path / "data",
        artifacts=tmp_path / "artifacts",
        reports=tmp_path / "reports",
    )
    command = DockerSandbox.command(policy=ResourcePolicy(), mounts=mounts, command=("demo", "all"))
    joined = " ".join(command)
    uid = getattr(os, "getuid", lambda: 65532)()
    gid = getattr(os, "getgid", lambda: 65532)()
    expected_user = f"{uid}:{gid}" if uid != 0 else "65532:65532"
    for required in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges:true",
        "--pids-limit",
        "--memory",
        f"--user {expected_user}",
        "/psrc/data,readonly",
    ):
        assert required in joined


def test_missing_docker_is_structured_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(DockerSandbox, "available", staticmethod(lambda: False))
    with pytest.raises(ContractViolation) as raised:
        DockerSandbox.require_available(run_id="test.sandbox", strategy_id="rule.test")
    assert raised.value.error.code == ErrorCode.SANDBOX_UNAVAILABLE
    assert raised.value.error.details["fallback_used"] is False


def test_sandbox_timeout_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mounts = ContainerMounts(
        data=tmp_path / "data",
        artifacts=tmp_path / "artifacts",
        reports=tmp_path / "reports",
    )
    monkeypatch.setattr(DockerSandbox, "available", staticmethod(lambda: True))

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ContractViolation) as raised:
        DockerSandbox.execute(
            run_id="test.timeout",
            strategy_id="rule.test",
            policy=ResourcePolicy(timeout_seconds=1),
            mounts=mounts,
            command=("demo", "all"),
        )
    assert raised.value.error.code == ErrorCode.SANDBOX_TIMEOUT
    assert raised.value.error.details["fallback_used"] is False


def test_process_attestation_requires_both_markers_and_non_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSRC_STRICT_SANDBOX", "1")
    monkeypatch.setenv("PSRC_SANDBOX_ATTESTATION", "strict-container-v1")
    monkeypatch.setattr(os, "geteuid", lambda: 65532, raising=False)
    monkeypatch.setattr(DockerSandbox, "_container_marker_present", staticmethod(lambda: True))
    monkeypatch.setattr(DockerSandbox, "_runtime_controls_present", staticmethod(lambda: True))
    assert DockerSandbox.current_process_attested() is True
    monkeypatch.delenv("PSRC_SANDBOX_ATTESTATION")
    assert DockerSandbox.current_process_attested() is False


def test_process_attestation_rejects_missing_runtime_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PSRC_STRICT_SANDBOX", "1")
    monkeypatch.setenv("PSRC_SANDBOX_ATTESTATION", "strict-container-v1")
    monkeypatch.setattr(os, "geteuid", lambda: 65532, raising=False)
    monkeypatch.setattr(DockerSandbox, "_container_marker_present", staticmethod(lambda: True))
    monkeypatch.setattr(DockerSandbox, "_runtime_controls_present", staticmethod(lambda: False))
    assert DockerSandbox.current_process_attested() is False


def test_single_package_sandbox_cli_uses_production_docker_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packages = tmp_path / "packages"
    output = tmp_path / "strict-output"
    assert main(["package", "export", "--output", str(packages)]) == 0
    captured: dict[str, object] = {}

    def execute(cls: type[DockerSandbox], /, **kwargs: object) -> SandboxExecutionResult:
        del cls
        captured.update(kwargs)
        mounts = kwargs["mounts"]
        assert isinstance(mounts, ContainerMounts)
        assert mounts.artifacts.is_dir()
        return SandboxExecutionResult(returncode=0, stdout="container-ok\n", stderr="")

    monkeypatch.setattr(DockerSandbox, "available", staticmethod(lambda: True))
    monkeypatch.setattr(DockerSandbox, "execute", classmethod(execute))
    assert (
        main(
            [
                "sandbox",
                "run",
                "--strategy-dir",
                str(packages / "rule.sma_cross"),
                "--output",
                str(output),
                "--engine",
                "nautilus-trader",
            ]
        )
        == 0
    )
    assert captured["strategy_id"] == "rule.sma_cross"
    assert captured["command"] == (
        "run",
        "--strategy-dir",
        "/psrc/data",
        "--output",
        "/psrc/reports",
        "--require-strict",
        "--engine",
        "nautilus-trader",
    )
    mounts = captured["mounts"]
    assert isinstance(mounts, ContainerMounts)
    assert mounts.data == (packages / "rule.sma_cross").resolve()
    assert mounts.reports == output.resolve()
