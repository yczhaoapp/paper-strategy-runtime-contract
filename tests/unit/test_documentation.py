from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from psrc.runtime.package import discover_strategy_packages


def _contains_chinese(path: Path) -> bool:
    return re.search(r"[\u3400-\u9fff]", path.read_text(encoding="utf-8")) is not None


def test_review_documents_and_strategy_cards_are_chinese() -> None:
    repository = Path(__file__).parents[2]
    documents = (
        repository / "README.md",
        repository / "docs/adapter-guide.md",
        repository / "docs/architecture.md",
        repository / "docs/reproduction.md",
        repository / "docs/audit-remediation.md",
        repository / "docs/strategy-matrix.md",
        repository / "docs/acceptance-audit.md",
        repository / "docs/interpretation-register.md",
        repository / "spec/contract-v1.md",
        repository / "spec/lifecycle.md",
        repository / "spec/runtime-interfaces.md",
        repository / "spec/package.md",
        repository / "spec/errors.md",
        repository / "spec/compatibility.md",
        repository / "spec/security.md",
        repository / "spec/versioning.md",
        repository / "spec/authoring-agent.md",
        repository / "spec/data-provenance.md",
    )
    assert all(_contains_chinese(path) for path in documents)

    packages = discover_strategy_packages(repository / "strategies")
    assert len(packages) == 18
    assert all(_contains_chinese(package.root / "STRATEGY_CARD.md") for package in packages)


def test_generated_contract_files_are_stable_across_hash_seeds(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from psrc.cli import main
from psrc.runtime.package import export_strategy_packages
from psrc.strategies.catalog import all_examples

root = Path(sys.argv[1])
manifests = tuple(example.manifest for example in all_examples())
export_strategy_packages(root / "strategies", manifests)
raise SystemExit(main(["schema", "export", "--output", str(root / "schemas")]))
"""
    outputs = (tmp_path / "seed-1", tmp_path / "seed-2")
    for seed, output in zip(("1", "987654"), outputs, strict=True):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        subprocess.run(
            [sys.executable, "-c", script, str(output)],
            check=True,
            env=environment,
        )

    def snapshot(root: Path) -> dict[Path, bytes]:
        return {
            path.relative_to(root): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(outputs[0]) == snapshot(outputs[1])
