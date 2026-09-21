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
    """Defense-in-depth scanner; it is explicitly not an isolation boundary."""

    dangerous_calls = frozenset(
        {
            "compile",
            "eval",
            "exec",
            "globals",
            "input",
            "locals",
            "open",
            "vars",
            "__import__",
        }
    )

    def __init__(self, allowed_imports: frozenset[str]) -> None:
        self.allowed_imports = allowed_imports
        self.findings: list[PolicyFinding] = []

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
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self._add("RELATIVE_IMPORT_DENIED", node, "relative imports are not permitted")
        elif node.module:
            self._check_import(node.module, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name in self.dangerous_calls:
            self._add(
                "DANGEROUS_CALL_DENIED",
                node,
                f"call to {name!r} is denied by the strategy policy",
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            self._add(
                "DUNDER_REFLECTION_DENIED",
                node,
                f"dunder attribute access {node.attr!r} is denied",
            )
        self.generic_visit(node)

    def _check_import(self, module: str, node: ast.AST) -> None:
        root = module.split(".", maxsplit=1)[0]
        if root not in self.allowed_imports:
            self._add(
                "IMPORT_DENIED",
                node,
                f"import root {root!r} is not in the manifest allow-list",
            )

    def _add(self, code: str, node: ast.AST, message: str) -> None:
        self.findings.append(
            PolicyFinding(
                code=code,
                line=getattr(node, "lineno", 0),
                column=getattr(node, "col_offset", 0),
                message=message,
            )
        )
