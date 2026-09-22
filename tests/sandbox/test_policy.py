from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from psrc.cli import main
from psrc.contract.errors import ContractViolation, ErrorCode
from psrc.contract.models import ResourcePolicy, SandboxMode
from psrc.domain.actions import Action, NoOp
from psrc.examples.sma_cross import SmaCrossStrategy
from psrc.runtime.artifacts import ArtifactStore
from psrc.runtime.package import load_strategy, load_strategy_manifest
from psrc.sandbox.container import (
    ContainerMounts,
    DockerSandbox,
    SandboxExecutionResult,
    _network_namespace_isolated,
)
from psrc.sandbox.runtime import GuardedStrategy, RuntimeResourceDenied, strategy_resource_guard
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


def test_static_scanner_resolves_aliases_and_rejects_runtime_namespace_escape() -> None:
    source = """
import numpy as np
from numpy import loadtxt as read_rows
from psrc import sandbox
np.save('/psrc/reports/overwrite.npy', [1])
read_rows('/etc/passwd')
"""
    findings = StaticPolicyScanner.scan(
        source,
        frozenset({"numpy", "psrc.strategy_api"}),
    )
    codes = [finding.code for finding in findings]
    assert codes.count("FILESYSTEM_CALL_DENIED") == 2
    assert "IMPORT_DENIED" in codes


def test_static_scanner_accepts_only_public_strategy_api_exports() -> None:
    accepted = StaticPolicyScanner.scan(
        "from psrc.strategy_api import ArtifactIO, StrategyManifest, TargetPosition\n",
        frozenset({"psrc.strategy_api"}),
    )
    denied = StaticPolicyScanner.scan(
        "from psrc.strategy_api import _BUNDLED_STRATEGIES\n",
        frozenset({"psrc.strategy_api"}),
    )
    assert accepted == ()
    assert {finding.code for finding in denied} == {
        "PRIVATE_IMPORT_DENIED",
        "STRATEGY_API_EXPORT_DENIED",
    }


def test_static_scanner_rejects_private_dependency_escape_and_strategy_api_children() -> None:
    private_escape = StaticPolicyScanner.scan(
        "import numpy as np\n"
        "import numpy.lib._npyio_impl as impl\n"
        "impl.os.remove('/psrc/reports/result.json')\n"
        "np.lib._npyio_impl.os.remove('/psrc/reports/result.json')\n"
        "from numpy.ctypeslib import ctypes as ffi\n"
        "ffi.pythonapi.Py_GetVersion()\n",
        frozenset({"numpy", "psrc.strategy_api"}),
    )
    api_child = StaticPolicyScanner.scan(
        "import psrc.strategy_api.internal\n",
        frozenset({"psrc.strategy_api"}),
    )
    private_codes = {finding.code for finding in private_escape}
    assert "PRIVATE_IMPORT_DENIED" in private_codes
    assert "PRIVATE_ATTRIBUTE_DENIED" in private_codes
    assert "RESOURCE_NAMESPACE_ESCAPE_DENIED" in private_codes
    assert {finding.code for finding in api_child} == {"IMPORT_DENIED"}


def test_runtime_audit_blocks_file_process_and_report_mount_access(tmp_path: Path) -> None:
    package = tmp_path / "package"
    reports = tmp_path / "reports"
    package.mkdir()
    reports.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    policy = ResourcePolicy()

    with strategy_resource_guard(policy=policy, package_root=package):
        with pytest.raises(RuntimeResourceDenied):
            np.save(reports / "overwrite.npy", np.asarray([1.0]))
        with pytest.raises(RuntimeResourceDenied):
            np.loadtxt(secret)
        with pytest.raises(RuntimeResourceDenied):
            subprocess.run(["true"], check=False)
        with pytest.raises(RuntimeResourceDenied):
            os.listdir(tmp_path)
        with pytest.raises(RuntimeResourceDenied):
            os.remove(secret)

    assert not (reports / "overwrite.npy").exists()
    assert secret.is_file()


def test_artifact_store_is_the_only_writable_strategy_channel(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    store = ArtifactStore(tmp_path / "artifacts")
    policy = ResourcePolicy()
    with strategy_resource_guard(policy=policy, package_root=package):
        artifact = store.save_bytes(
            run_id="test.runtime-audit",
            artifact_id="sha256-test",
            strategy_id="rule.test",
            strategy_version="1.0.0",
            artifact_kind="state",
            framework="test",
            logical_name="state.txt",
            media_type="text/plain",
            payload=b"ok",
            training_dataset_id="test.dataset",
            seed=0,
        )
        with pytest.raises(RuntimeResourceDenied):
            (store.root / artifact.artifact_id / "tamper.txt").write_text("bad")

    assert store.load_bytes(
        run_id="test.runtime-audit",
        strategy_id="rule.test",
        manifest=artifact,
    ) == {"state.txt": b"ok"}


def test_manifest_descriptor_executes_inside_resource_guard(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    source_package = Path("strategies/supervised.logistic_direction")
    shutil.copy2(source_package / "strategy.yaml", package / "strategy.yaml")
    marker = tmp_path / "outside-marker.txt"
    package.joinpath("strategy.py").write_text(
        "from psrc.strategy_api import bundled_strategy_class\n"
        "writer = open\n"
        '_Bundled = bundled_strategy_class("supervised.logistic_direction")\n'
        "class Strategy(_Bundled):\n"
        "    @property\n"
        "    def manifest(self):\n"
        f"        writer({str(marker)!r}, 'w').write('forbidden')\n"
        "        return super().manifest\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractViolation) as raised:
        load_strategy(
            load_strategy_manifest(package), sandbox_mode=SandboxMode.DEVELOPMENT
        )
    assert raised.value.error.code == ErrorCode.SANDBOX_POLICY_DENIED
    assert not marker.exists()


def test_guarded_action_return_is_canonicalized_before_leaving_policy_scope(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    marker = tmp_path / "outside-marker.txt"

    class DeferredActions(list[Action]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            marker.write_text("forbidden", encoding="utf-8")
            return super().__iter__()

    class DeferredActionStrategy(SmaCrossStrategy):
        def on_event(self, event: object, account: object) -> tuple[Action, ...]:
            del event, account
            return DeferredActions(
                [NoOp(reason_code="test.deferred", explanation="deferred container")]
            )  # type: ignore[return-value]

    raw_strategy = DeferredActionStrategy()
    strategy = GuardedStrategy(
        raw_strategy,
        manifest=raw_strategy.manifest,
        policy=raw_strategy.manifest.resources,
        package_root=package,
    )

    actions = strategy.on_event(object(), object())

    assert type(actions) is tuple
    assert len(actions) == 1
    assert type(actions[0]) is NoOp
    assert not marker.exists()


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
    assert raised.value.error.details["container_cleanup_attempted"] is True
    assert raised.value.error.details["container_cleanup_succeeded"] is False
    assert raised.value.error.details["fallback_used"] is False


def test_sandbox_timeout_force_removes_the_named_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounts = ContainerMounts(
        data=tmp_path / "data",
        artifacts=tmp_path / "artifacts",
        reports=tmp_path / "reports",
    )
    monkeypatch.setattr(DockerSandbox, "available", staticmethod(lambda: True))
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(tuple(command))
        if "run" in command:
            raise subprocess.TimeoutExpired(cmd=command, timeout=1)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ContractViolation) as raised:
        DockerSandbox.execute(
            run_id="test.timeout-cleanup",
            strategy_id="rule.test",
            policy=ResourcePolicy(timeout_seconds=1),
            mounts=mounts,
            command=("demo", "all"),
        )
    container_name = calls[0][calls[0].index("--name") + 1]
    assert calls[1][-3:] == ("rm", "--force", container_name)
    assert raised.value.error.details["container_cleanup_succeeded"] is True


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
