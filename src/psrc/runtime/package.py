from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast

import yaml

from psrc.contract.errors import ContractError, ContractViolation, ErrorCode, ErrorStage
from psrc.contract.hashing import sha256_model
from psrc.contract.models import SandboxMode, StrategyCodeEvidence, StrategyManifest
from psrc.runtime.source_evidence import build_strategy_code_evidence
from psrc.runtime.strategy import RuntimeStrategy
from psrc.sandbox.container import DockerSandbox
from psrc.sandbox.runtime import GuardedStrategy, RuntimeResourceDenied, strategy_resource_guard
from psrc.sandbox.static import StaticPolicyScanner

MANIFEST_NAME = "strategy.yaml"
LOCAL_ENTRYPOINT = "strategy.py:Strategy"


def _deterministic_yaml_value(value: Any) -> Any:
    """Convert Pydantic output while making semantically unordered sets reproducible."""
    if isinstance(value, dict):
        return {str(key): _deterministic_yaml_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        normalized = [_deterministic_yaml_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    if isinstance(value, (list, tuple)):
        return [_deterministic_yaml_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class StrategyPackage:
    root: Path
    manifest: StrategyManifest
    manifest_sha256: str
    code_evidence: StrategyCodeEvidence


def discover_strategy_packages(root: Path) -> tuple[StrategyPackage, ...]:
    """Discover and validate packages without importing strategy code."""
    packages = tuple(
        load_strategy_manifest(path.parent) for path in root.glob(f"*/{MANIFEST_NAME}")
    )
    return tuple(sorted(packages, key=lambda package: package.manifest.strategy_id))


def load_strategy_manifest(package_root: Path) -> StrategyPackage:
    manifest_path = package_root.resolve() / MANIFEST_NAME
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest = StrategyManifest.model_validate(raw)
    except Exception as exc:
        raise ContractViolation(
            ContractError(
                run_id="package.discovery",
                stage=ErrorStage.DISCOVERY,
                code=ErrorCode.MANIFEST_INVALID,
                message="Strategy package manifest is missing or invalid",
                strategy_id=None,
                details={"manifest_path": str(manifest_path)},
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
    try:
        code_evidence = build_strategy_code_evidence(
            package_root.resolve(), entrypoint=manifest.entrypoint
        )
    except Exception as exc:
        raise ContractViolation(
            ContractError(
                run_id="package.discovery",
                stage=ErrorStage.DISCOVERY,
                code=ErrorCode.MANIFEST_INVALID,
                message="Strategy package source evidence could not be generated",
                strategy_id=manifest.strategy_id,
                details={"package_root": str(package_root.resolve())},
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
    return StrategyPackage(
        root=package_root.resolve(),
        manifest=manifest,
        manifest_sha256=sha256_model(manifest),
        code_evidence=code_evidence,
    )


def load_strategy(
    package: StrategyPackage,
    *,
    sandbox_mode: SandboxMode,
) -> RuntimeStrategy:
    """Scan and import an entrypoint only after the package manifest is validated."""
    if (
        sandbox_mode == SandboxMode.STRICT_CONTAINER
        and not DockerSandbox.current_process_attested()
    ):
        raise ContractViolation(
            ContractError(
                run_id="package.load",
                stage=ErrorStage.SANDBOX,
                code=ErrorCode.SANDBOX_UNAVAILABLE,
                message="Strict strategy loading requires container attestation",
                strategy_id=package.manifest.strategy_id,
                details={"fallback_used": False},
            )
        )
    observed_code_evidence = build_strategy_code_evidence(
        package.root, entrypoint=package.manifest.entrypoint
    )
    if observed_code_evidence != package.code_evidence:
        raise ContractViolation(
            ContractError(
                run_id="package.load",
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.SOURCE_HASH_MISMATCH,
                message="Strategy or runtime source changed after discovery",
                strategy_id=package.manifest.strategy_id,
                details={
                    "expected": sha256_model(package.code_evidence),
                    "actual": sha256_model(observed_code_evidence),
                    "fallback_used": False,
                },
            )
        )
    try:
        module_name, class_name = package.manifest.entrypoint.split(":", maxsplit=1)
        source_path = _local_entrypoint(package, module_name)
        if source_path is not None:
            _enforce_source_policy(package)
            library_roots: list[Path] = []
            for allowed_module in sorted(
                package.manifest.resources.allowed_imports | frozenset({"psrc.strategy_api"})
            ):
                imported = importlib.import_module(allowed_module)
                module_paths = getattr(imported, "__path__", ())
                library_roots.extend(Path(path).resolve() for path in module_paths)
            module_id = f"_psrc_package_{package.manifest_sha256[:20]}"
            spec = importlib.util.spec_from_file_location(module_id, source_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot create import spec for {source_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_id] = module
            previous_dont_write_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                with strategy_resource_guard(
                        policy=package.manifest.resources,
                        package_root=package.root,
                        library_roots=tuple(library_roots),
                ):
                    spec.loader.exec_module(module)
                    strategy_type: Any = getattr(module, class_name)
                    strategy = strategy_type()
                    loaded_manifest = getattr(strategy, "manifest", None)
            finally:
                sys.dont_write_bytecode = previous_dont_write_bytecode
        else:
            if sandbox_mode == SandboxMode.STRICT_CONTAINER and not module_name.startswith("psrc."):
                raise PermissionError(
                    "strict mode only accepts scanned package-local code or trusted psrc modules"
                )
            strategy_type = getattr(importlib.import_module(module_name), class_name)
            strategy = strategy_type()
            loaded_manifest = getattr(strategy, "manifest", None)
    except ContractViolation:
        raise
    except RuntimeResourceDenied as exc:
        raise ContractViolation(
            ContractError(
                run_id="package.load",
                stage=ErrorStage.SANDBOX,
                code=ErrorCode.SANDBOX_POLICY_DENIED,
                message="Strategy import violated its resource policy",
                strategy_id=package.manifest.strategy_id,
                details={
                    "audit_event": exc.event,
                    "resource": exc.resource,
                    "fallback_used": False,
                },
            )
        ) from exc
    except Exception as exc:
        raise ContractViolation(
            ContractError(
                run_id="package.load",
                stage=ErrorStage.INITIALIZATION,
                code=ErrorCode.MANIFEST_INVALID,
                message="Strategy entrypoint could not be instantiated",
                strategy_id=package.manifest.strategy_id,
                details={"entrypoint": package.manifest.entrypoint},
                cause_chain=(f"{type(exc).__name__}: {exc}",),
            )
        ) from exc
    if loaded_manifest != package.manifest:
        raise ContractViolation(
            ContractError(
                run_id="package.load",
                stage=ErrorStage.VALIDATION,
                code=ErrorCode.MANIFEST_INVALID,
                message="Entrypoint manifest differs from the package manifest",
                strategy_id=package.manifest.strategy_id,
                details={
                    "package_manifest_sha256": package.manifest_sha256,
                    "entrypoint_manifest_sha256": (
                        sha256_model(loaded_manifest)
                        if isinstance(loaded_manifest, StrategyManifest)
                        else None
                    ),
                },
            )
        )
    if source_path is not None:
        strategy = GuardedStrategy(
            strategy,
            manifest=package.manifest,
            policy=package.manifest.resources,
            package_root=package.root,
            library_roots=tuple(library_roots),
        )
    return cast(RuntimeStrategy, strategy)


def _local_entrypoint(package: StrategyPackage, module_name: str) -> Path | None:
    if not module_name.endswith(".py"):
        return None
    relative = Path(module_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("package-local entrypoint must be a safe relative Python path")
    source_path = (package.root / relative).resolve()
    if not source_path.is_relative_to(package.root) or not source_path.is_file():
        raise FileNotFoundError(f"package-local entrypoint is absent: {relative}")
    return source_path


def _enforce_source_policy(package: StrategyPackage) -> None:
    allowed_imports = package.manifest.resources.allowed_imports | frozenset(
        {"psrc.strategy_api"}
    )
    findings: list[dict[str, object]] = []
    for source_path in sorted(package.root.rglob("*.py")):
        for finding in StaticPolicyScanner.scan(
            source_path.read_text(encoding="utf-8"), allowed_imports
        ):
            findings.append(
                {
                    "file": str(source_path.relative_to(package.root)),
                    "code": finding.code,
                    "line": finding.line,
                    "column": finding.column,
                    "message": finding.message,
                }
            )
    if findings:
        raise ContractViolation(
            ContractError(
                run_id="package.load",
                stage=ErrorStage.SANDBOX,
                code=ErrorCode.SANDBOX_POLICY_DENIED,
                message="Strategy package source violates its declared import and file policy",
                strategy_id=package.manifest.strategy_id,
                details={"findings": findings, "fallback_used": False},
            )
        )


def _local_manifest(manifest: StrategyManifest) -> StrategyManifest:
    return manifest.model_copy(update={"entrypoint": LOCAL_ENTRYPOINT})


def _entrypoint_wrapper(manifest: StrategyManifest) -> str:
    module_name, class_name = manifest.entrypoint.split(":", maxsplit=1)
    if module_name.endswith(".py"):
        raise ValueError("catalog export requires an installed Python entrypoint")
    return (
        "# Generated, reviewable package entrypoint.\n"
        f"# Bundled implementation: {class_name}\n"
        "from psrc.strategy_api import bundled_strategy_class\n\n"
        f'_BundledStrategy = bundled_strategy_class("{manifest.strategy_id}")\n\n\n'
        "class Strategy(_BundledStrategy):\n"
        "    manifest = _BundledStrategy.manifest.model_copy(\n"
        f'        update={{"entrypoint": "{LOCAL_ENTRYPOINT}"}}\n'
        "    )\n"
    )


def export_strategy_packages(root: Path, manifests: tuple[StrategyManifest, ...]) -> None:
    """Materialize reviewable package metadata from the executable catalog."""
    root.mkdir(parents=True, exist_ok=True)
    for manifest in manifests:
        package_manifest = _local_manifest(manifest)
        package = root / manifest.strategy_id
        package.mkdir(parents=True, exist_ok=True)
        with (package / MANIFEST_NAME).open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            yaml.safe_dump(
                _deterministic_yaml_value(package_manifest.model_dump(mode="python")),
                handle,
                allow_unicode=True,
                sort_keys=False,
            )
        (package / "strategy.py").write_text(
            _entrypoint_wrapper(manifest), encoding="utf-8", newline="\n"
        )
        requirements = "\n".join(
            f"- `{item.kind}` / `{item.timeframe.mode}`"
            f"{f' `{item.timeframe.interval}`' if item.timeframe.interval else ''}: "
            f"{', '.join(sorted(item.required_fields))}"
            for item in manifest.data_requirements
        )
        actions = ", ".join(sorted(str(action) for action in manifest.action_requirements.allowed))
        card = (
            f"# {manifest.strategy_id}\n\n"
            f"- 策略类别：`{manifest.kind}`\n"
            f"- 策略版本：`{manifest.strategy_version}`\n"
            f"- 执行入口：`{package_manifest.entrypoint}`\n"
            f"- 训练模式：`{manifest.lifecycle.training}`\n"
            f"- 允许动作：{actions}\n"
            f"- 确定性种子：`{manifest.seed}`\n\n"
            "## 数据契约\n\n"
            f"{requirements}\n\n"
            "本策略仅用于可复现研究演示，不构成投资建议。\n"
        )
        (package / "STRATEGY_CARD.md").write_text(
            card, encoding="utf-8", newline="\n"
        )
