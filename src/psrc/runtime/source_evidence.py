from __future__ import annotations

import hashlib
from pathlib import Path

from psrc.constants import CONTRACT_VERSION
from psrc.contract.hashing import sha256_model
from psrc.contract.models import SourceFileEvidence, StrategyCodeEvidence


def _source_files(root: Path) -> tuple[SourceFileEvidence, ...]:
    resolved_root = root.resolve()
    evidence: list[SourceFileEvidence] = []
    for source in sorted(resolved_root.rglob("*.py")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(resolved_root):
            raise ValueError("source evidence does not accept symbolic-link escapes")
        content = source.read_bytes()
        evidence.append(
            SourceFileEvidence(
                path=source.relative_to(resolved_root).as_posix(),
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(evidence)


def _tree_sha256(files: tuple[SourceFileEvidence, ...]) -> str:
    return sha256_model(
        {
            "files": [
                item.model_dump(mode="json")
                for item in sorted(files, key=lambda evidence: evidence.path)
            ]
        }
    )


def build_strategy_code_evidence(package_root: Path, *, entrypoint: str) -> StrategyCodeEvidence:
    package_files = _source_files(package_root)
    if not package_files:
        raise ValueError("strategy package contains no Python source files")
    runtime_root = Path(__file__).resolve().parents[1]
    runtime_files = _source_files(runtime_root)
    return StrategyCodeEvidence(
        entrypoint=entrypoint,
        package_files=package_files,
        package_sha256=_tree_sha256(package_files),
        runtime_version=CONTRACT_VERSION,
        runtime_file_count=len(runtime_files),
        runtime_source_sha256=_tree_sha256(runtime_files),
    )
