from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyFinding:
    code: str
    line: int
    column: int
    message: str


class StaticPolicyScanner(ast.NodeVisitor):
    """Fail-closed admission scan for the strategy source surface.

    Runtime audit hooks and the strict container remain the enforcement boundaries.
    This scanner rejects known file/process escape surfaces before any package code
    is imported and resolves aliases so ``np.save`` and ``from numpy import save``
    receive the same decision.
    """

    dangerous_calls = frozenset(
        {
            "compile",
            "eval",
            "exec",
            "globals",
            "getattr",
            "input",
            "locals",
            "open",
            "setattr",
            "delattr",
            "vars",
            "__import__",
        }
    )

    filesystem_call_paths = frozenset(
        {
            "numpy.DataSource",
            "numpy.fromfile",
            "numpy.genfromtxt",
            "numpy.load",
            "numpy.loadtxt",
            "numpy.memmap",
            "numpy.recfromcsv",
            "numpy.recfromtxt",
            "numpy.save",
            "numpy.savetxt",
            "numpy.savez",
            "numpy.savez_compressed",
            "numpy.lib.format.open_memmap",
            "numpy.lib.npyio.DataSource",
        }
    )
    filesystem_attribute_names = frozenset({"tofile"})
    resource_namespace_segments = frozenset(
        {
            "builtins",
            "ctypes",
            "importlib",
            "os",
            "pathlib",
            "shutil",
            "socket",
            "subprocess",
            "sys",
        }
    )
    strategy_api = "psrc.strategy_api"
    strategy_api_exports = frozenset(
        {
            "Action",
            "ActionEnvelope",
            "ActionKind",
            "AccountSnapshot",
            "ArtifactIO",
            "ArtifactManifest",
            "AvellanedaStrategy",
            "BarPayload",
            "BookLevel",
            "BookSnapshotL2Payload",
            "CancelOrder",
            "DataKind",
            "ExecutionQStrategy",
            "MarketEvent",
            "MovingAverageBandStrategy",
            "NoOp",
            "Prediction",
            "QuoteL1Payload",
            "QueueLogisticStrategy",
            "ReplaceOrder",
            "StrategyManifest",
            "SubmitOrder",
            "TargetPosition",
            "TargetWeight",
            "TradePayload",
            "TrainingRequest",
            "bundled_strategy_class",
        }
    )

    def __init__(self, allowed_imports: frozenset[str]) -> None:
        self.allowed_imports = allowed_imports
        self.findings: list[PolicyFinding] = []
        self.aliases: dict[str, str] = {}

    @classmethod
    def scan(cls, source: str, allowed_imports: frozenset[str]) -> tuple[PolicyFinding, ...]:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return (
                PolicyFinding(
                    code="PYTHON_SYNTAX_INVALID",
                    line=exc.lineno or 0,
                    column=exc.offset or 0,
                    message=exc.msg,
                ),
            )
        scanner = cls(allowed_imports)
        scanner.visit(tree)
        return tuple(scanner.findings)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(alias.name, node)
            self._check_import_path(alias.name, node)
            local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            self.aliases[local_name] = alias.name if alias.asname else local_name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self._add("RELATIVE_IMPORT_DENIED", node, "relative imports are not permitted")
        elif node.module:
            self._check_import(node.module, node)
            self._check_import_path(node.module, node)
            for alias in node.names:
                if alias.name == "*":
                    self._add("STAR_IMPORT_DENIED", node, "star imports are not permitted")
                    continue
                if alias.name.startswith("_"):
                    self._add(
                        "PRIVATE_IMPORT_DENIED",
                        node,
                        f"private import {alias.name!r} is not permitted",
                    )
                if node.module == self.strategy_api and alias.name not in self.strategy_api_exports:
                    self._add(
                        "STRATEGY_API_EXPORT_DENIED",
                        node,
                        f"{alias.name!r} is not part of the public strategy API",
                    )
                self._check_resource_namespace(f"{node.module}.{alias.name}", node)
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._qualified_name(node.func)
        if name in self.dangerous_calls:
            self._add(
                "DANGEROUS_CALL_DENIED",
                node,
                f"call to {name!r} is denied by the strategy policy",
            )
        if name in self.filesystem_call_paths or (
            name is not None and name.rsplit(".", maxsplit=1)[-1] in self.filesystem_attribute_names
        ):
            self._add(
                "FILESYSTEM_CALL_DENIED",
                node,
                f"filesystem-capable call {name!r} is denied by the strategy policy",
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self._add(
                "DUNDER_REFLECTION_DENIED",
                node,
                f"dunder attribute access {node.attr!r} is denied",
            )
        qualified = self._qualified_name(node)
        if not node.attr.startswith("__") and node.attr.startswith("_"):
            self._add(
                "PRIVATE_ATTRIBUTE_DENIED",
                node,
                f"private attribute access {node.attr!r} is denied",
            )
        if qualified:
            self._check_resource_namespace(qualified, node)
        if qualified and qualified.startswith(f"{self.strategy_api}._"):
            self._add(
                "PRIVATE_RUNTIME_ATTRIBUTE_DENIED",
                node,
                f"private runtime attribute {qualified!r} is not permitted",
            )

    def _check_import_path(self, module: str, node: ast.AST) -> None:
        if any(part.startswith("_") for part in module.split(".")):
            self._add(
                "PRIVATE_IMPORT_DENIED",
                node,
                f"private module path {module!r} is not permitted",
            )
        self._check_resource_namespace(module, node)

    def _check_resource_namespace(self, qualified: str, node: ast.AST) -> None:
        parts = qualified.split(".")
        if parts[0] in {allowed.split(".", maxsplit=1)[0] for allowed in self.allowed_imports} and (
            set(parts[1:]) & self.resource_namespace_segments
        ):
            self._add(
                "RESOURCE_NAMESPACE_ESCAPE_DENIED",
                node,
                f"resource namespace escape {qualified!r} is not permitted",
            )
        self.generic_visit(node)

    def _check_import(self, module: str, node: ast.AST) -> None:
        permitted = any(
            module == allowed
            or (
                allowed != self.strategy_api
                and module.startswith(f"{allowed}.")
            )
            for allowed in self.allowed_imports
        )
        if not permitted:
            self._add(
                "IMPORT_DENIED",
                node,
                f"import {module!r} is not in the effective module allow-list",
            )

    def _qualified_name(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = self._qualified_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    def _add(self, code: str, node: ast.AST, message: str) -> None:
        self.findings.append(
            PolicyFinding(
                code=code,
                line=getattr(node, "lineno", 0),
                column=getattr(node, "col_offset", 0),
                message=message,
            )
        )
