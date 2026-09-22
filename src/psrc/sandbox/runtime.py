from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.models import ResourcePolicy


class RuntimeResourceDenied(PermissionError):
    def __init__(self, event: str, resource: str | None = None) -> None:
        detail = f" for {resource!r}" if resource is not None else ""
        super().__init__(f"runtime audit denied {event!r}{detail}")
        self.event = event
        self.resource = resource


@dataclass(frozen=True)
class _AuditScope:
    policy: ResourcePolicy
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]


_ACTIVE_SCOPE: ContextVar[_AuditScope | None] = ContextVar("psrc_audit_scope", default=None)
_TRUSTED_RUNTIME_IO: ContextVar[int] = ContextVar("psrc_trusted_runtime_io", default=0)
_HOOK_LOCK = Lock()
_HOOK_INSTALLED = False

_SINGLE_PATH_READ_EVENTS = frozenset(
    {
        "glob.glob",
        "os.listdir",
        "os.scandir",
        "os.walk",
    }
)
_SINGLE_PATH_WRITE_EVENTS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.mkdir",
        "os.remove",
        "os.rmdir",
        "os.truncate",
        "os.unlink",
        "os.utime",
        "shutil.rmtree",
    }
)
_TWO_PATH_WRITE_EVENTS = frozenset(
    {
        "os.link",
        "os.rename",
        "os.replace",
        "os.symlink",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
    }
)


def _canonical(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> Path:
    return Path(os.path.realpath(os.fsdecode(path)))


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _write_requested(mode: object, flags: object) -> bool:
    if isinstance(mode, str):
        return any(marker in mode for marker in ("w", "a", "x", "+"))
    if isinstance(flags, int):
        mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & mask)
    return False


def _audit(event: str, args: tuple[object, ...]) -> None:
    if _TRUSTED_RUNTIME_IO.get() > 0:
        return
    scope = _ACTIVE_SCOPE.get()
    if scope is None:
        return
    if event == "open" and args and scope.policy.filesystem != "unrestricted":
        raw_path = args[0]
        if isinstance(raw_path, int):
            raise RuntimeResourceDenied(event, f"file-descriptor:{raw_path}")
        if isinstance(raw_path, (str, bytes, os.PathLike)):
            path = _canonical(raw_path)
            write = _write_requested(
                args[1] if len(args) > 1 else None,
                args[2] if len(args) > 2 else None,
            )
            roots = scope.write_roots if write else scope.read_roots
            if not _is_within(path, roots):
                raise RuntimeResourceDenied(event, str(path))
    if event in _SINGLE_PATH_READ_EVENTS and args and scope.policy.filesystem != "unrestricted":
        raw_path = args[0]
        if isinstance(raw_path, (str, bytes, os.PathLike)):
            path = _canonical(raw_path)
            if not _is_within(path, scope.read_roots):
                raise RuntimeResourceDenied(event, str(path))
    if event in _SINGLE_PATH_WRITE_EVENTS and args and scope.policy.filesystem != "unrestricted":
        raw_path = args[0]
        if isinstance(raw_path, (str, bytes, os.PathLike)):
            path = _canonical(raw_path)
            if not _is_within(path, scope.write_roots):
                raise RuntimeResourceDenied(event, str(path))
    if (
        event in _TWO_PATH_WRITE_EVENTS
        and len(args) >= 2
        and scope.policy.filesystem != "unrestricted"
    ):
        for raw_path in args[:2]:
            if isinstance(raw_path, (str, bytes, os.PathLike)):
                path = _canonical(raw_path)
                if not _is_within(path, scope.write_roots):
                    raise RuntimeResourceDenied(event, str(path))
    if event.startswith("socket.") and scope.policy.network == "deny":
        raise RuntimeResourceDenied(event)
    if event in {
        "ctypes.dlopen",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "pty.spawn",
        "subprocess.Popen",
    }:
        raise RuntimeResourceDenied(event)
    if event in {"sys.addaudithook", "sys.setprofile", "sys.settrace"}:
        raise RuntimeResourceDenied(event)


def _ensure_audit_hook() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    with _HOOK_LOCK:
        if not _HOOK_INSTALLED:
            sys.addaudithook(_audit)
            _HOOK_INSTALLED = True


@contextmanager
def strategy_resource_guard(
    *,
    policy: ResourcePolicy,
    package_root: Path,
    artifact_roots: tuple[Path, ...] = (),
    library_roots: tuple[Path, ...] = (),
) -> Iterator[None]:
    """Apply process-local runtime controls while package code is executing.

    Package source is readable so Python can import it. Artifact roots are the
    only strategy-visible read/write paths under ``artifact_store_only``.
    """
    _ensure_audit_hook()
    package = package_root.resolve()
    artifacts = tuple(root.resolve() for root in artifact_roots)
    libraries = tuple(root.resolve() for root in library_roots)
    read_roots = (package, *artifacts, *libraries)
    write_roots = artifacts
    token = _ACTIVE_SCOPE.set(
        _AuditScope(policy=policy, read_roots=read_roots, write_roots=write_roots)
    )
    try:
        yield
    finally:
        _ACTIVE_SCOPE.reset(token)


def trusted_runtime_io[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Permit filesystem work performed by a validated runtime service."""

    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        token = _TRUSTED_RUNTIME_IO.set(_TRUSTED_RUNTIME_IO.get() + 1)
        try:
            return function(*args, **kwargs)
        finally:
            _TRUSTED_RUNTIME_IO.reset(token)

    return wrapped


class GuardedStrategy:
    """Proxy that applies the manifest resource policy to every strategy callback."""

    def __init__(
        self,
        strategy: Any,
        *,
        policy: ResourcePolicy,
        package_root: Path,
        library_roots: tuple[Path, ...] = (),
    ) -> None:
        self._strategy = strategy
        self._policy = policy
        self._package_root = package_root.resolve()
        self._library_roots = tuple(root.resolve() for root in library_roots)
        self._run_id = "package.runtime"

    @property
    def manifest(self) -> Any:
        return self._strategy.manifest

    def bind_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    def _invoke(
        self,
        method: str,
        *args: object,
        artifact_roots: tuple[Path, ...] = (),
        **kwargs: object,
    ) -> Any:
        target = getattr(self._strategy, method)
        try:
            with strategy_resource_guard(
                policy=self._policy,
                package_root=self._package_root,
                artifact_roots=artifact_roots,
                library_roots=self._library_roots,
            ):
                return target(*args, **kwargs)
        except RuntimeResourceDenied as exc:
            raise ContractViolation(
                ContractError(
                    run_id=self._run_id,
                    stage=ErrorStage.SANDBOX,
                    code=ErrorCode.SANDBOX_POLICY_DENIED,
                    message="Strategy runtime operation violated its resource policy",
                    strategy_id=self.manifest.strategy_id,
                    details={
                        "audit_event": exc.event,
                        "resource": exc.resource,
                        "filesystem_policy": self._policy.filesystem,
                        "network_policy": self._policy.network,
                        "fallback_used": False,
                    },
                )
            ) from exc

    def on_start(self) -> None:
        self._invoke("on_start")

    def on_event(self, event: object, account: object) -> Any:
        return self._invoke("on_event", event, account)

    def on_finish(self) -> None:
        self._invoke("on_finish")

    def train(self, request: object, store: Any) -> Any:
        return self._invoke("train", request, store)

    def load(self, manifest: object, store: Any, *, run_id: str) -> None:
        self._invoke("load", manifest, store, run_id=run_id)


def bind_strategy_run_id(strategy: object, run_id: str) -> None:
    if isinstance(strategy, GuardedStrategy):
        strategy.bind_run_id(run_id)
