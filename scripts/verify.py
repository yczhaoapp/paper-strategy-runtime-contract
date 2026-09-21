#!/usr/bin/env python3
"""Cross-platform verification of an already installed environment; never syncs/builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tree(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def verification_input_sha256(root: Path) -> str:
    """Bind a receipt to every reviewable verification input, excluding caches and reports."""
    inputs = (
        ".dockerignore",
        ".gitattributes",
        ".github",
        "ACCEPTANCE_MATRIX.yaml",
        "Dockerfile",
        "LICENSE",
        "Makefile",
        "NOTICE",
        "README.md",
        "CHANGELOG.md",
        "data/public",
        "docs",
        "engine_profiles",
        "papers/bindings",
        "papers/recipes",
        "papers/source-registry.json",
        "pyproject.toml",
        "schemas",
        "scripts",
        "spec",
        "src",
        "strategies",
        "tests",
        "uv.lock",
    )
    files: list[Path] = []
    for name in inputs:
        path = root / name
        files.extend(path.rglob("*") if path.is_dir() else (path,))
    digest = hashlib.sha256()
    for path in sorted(
        (
            path
            for path in files
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
            and path.name != ".DS_Store"
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fetch", action="store_true", help="online preparation, before verification"
    )
    mode.add_argument("--offline", action="store_true", help="require pre-fetched papers (default)")
    parser.add_argument("--require-strict", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/generated")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    steps: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="psrc-verify-") as temporary:
        temp = Path(temporary)
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            UV_OFFLINE="1",
            UV_NO_SYNC="1",
            COVERAGE_FILE=str(temp / ".coverage"),
            HYPOTHESIS_STORAGE_DIRECTORY=str(temp / "hypothesis"),
        )
        # Each invocation has fresh runtime evidence, preventing a failed run using prior success.
        stage = temp / "evidence"
        stage.mkdir()

        def run(name: str, argv: list[str]) -> None:
            print(f"[{name}]", flush=True)
            completed = subprocess.run(
                [sys.executable, *argv],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            (output / f"{name}.log").write_text(
                completed.stdout + completed.stderr, encoding="utf-8"
            )
            steps.append({"step": name, "returncode": completed.returncode})
            if completed.returncode:
                raise RuntimeError(f"{name} failed; see {output / (name + '.log')}")

        try:
            if args.fetch:
                run("fetch", ["scripts/fetch-papers.py"])
            else:
                registry = json.loads((ROOT / "papers/source-registry.json").read_text())
                for source in registry["sources"]:
                    pid = source["source_id"]
                    if not (ROOT / "papers/sources" / f"{pid}.pdf").is_file():
                        raise RuntimeError(
                            "offline sources absent; run scripts/fetch-papers.py first"
                        )
            if args.require_strict:
                from psrc.sandbox.container import DockerSandbox

                if not DockerSandbox.current_process_attested():
                    raise RuntimeError("SANDBOX_UNAVAILABLE: strict kernel controls not attested")
            run("ruff", ["-m", "ruff", "check", ".", "--cache-dir", str(temp / "ruff")])
            run("mypy", ["-m", "mypy", "src", "tests", "--cache-dir", str(temp / "mypy")])
            run("schema", ["-m", "psrc.cli", "schema", "export", "--output", str(temp / "schemas")])
            run(
                "packages",
                ["-m", "psrc.cli", "package", "export", "--output", str(temp / "strategies")],
            )
            for generated, committed in (
                (temp / "schemas", ROOT / "schemas/generated"),
                (temp / "strategies", ROOT / "strategies"),
            ):
                if tree(generated) != tree(committed):
                    raise RuntimeError(f"generated content drift: {committed}")
            run(
                "pytest",
                [
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={stage / 'junit.xml'}",
                    "--cov=psrc",
                    f"--cov-report=json:{stage / 'coverage.json'}",
                    "--cov-fail-under=90",
                ],
            )
            strict = ["--require-strict"] if args.require_strict else []
            for example in ("all", "failures", "adapters", "compatibility"):
                run(
                    example,
                    [
                        "-m",
                        "psrc.cli",
                        "demo",
                        example,
                        "--output",
                        str(stage / "runs" / example),
                        *(strict if example == "all" else []),
                    ],
                )
            run(
                "papers",
                [
                    "-m",
                    "psrc.cli",
                    "paper",
                    "suite",
                    "--output",
                    str(stage / "runs/papers"),
                    *strict,
                ],
            )
            run(
                "acceptance",
                [
                    "-m",
                    "psrc.cli",
                    "verify",
                    "--matrix",
                    str(ROOT / "ACCEPTANCE_MATRIX.yaml"),
                    "--evidence-root",
                    str(stage),
                    "--output",
                    str(stage / "acceptance-report.json"),
                    *strict,
                ],
            )
            status, error = "passed", None
        except Exception as exc:
            status, error = "failed", str(exc)
        # Always keep evidence and failed logs. No deletion of user-owned output directories.
        import shutil

        for item in stage.iterdir():
            target = output / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        if status == "failed" and not (stage / "acceptance-report.json").exists():
            (output / "acceptance-report.json").write_text(
                json.dumps({"status": "failed", "reason": error, "evidence_complete": False})
                + "\n",
                encoding="utf-8",
            )
        receipt = {
            "status": status,
            "error": error,
            "steps": steps,
            "strict_requested": args.require_strict,
            "python": sys.version,
            "software_version": tomllib.loads(
                (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["version"],
            "verification_input_sha256": verification_input_sha256(ROOT),
            "scope": "strict_container" if args.require_strict else "development_preflight",
        }
        (output / "verification.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(receipt, indent=2))
        return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
